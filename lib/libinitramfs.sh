#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# libinitramfs.sh - Build the ktest initramfs from a container image
#
# The initramfs userspace derives from a container image (a stripped-down
# Ubuntu, see containers/Containerfile.initramfs), the same way root_image
# derives the VM root filesystems.  /init is a bash script baked into the
# image that mounts the kernel filesystems and always drops into an
# interactive bash shell; running `boot` there hands off to /sbin/ktest-init
# (compiled from init/init.c), which mounts the root named on the kernel
# cmdline (root= or lustreroot=) and switch_roots into it.  The host-side
# additions are kernel build artifacts (modules, optionally firmware) and
# the static init/ binaries.

INITRAMFS_IMAGE_TAG=${INITRAMFS_IMAGE_TAG:-localhost/ktest-initramfs:latest}
INITRAMFS_IMAGE_BASE=${INITRAMFS_IMAGE_BASE:-docker.io/library/ubuntu:26.04}

# mk_initramfs
#
# Bundles the modules from $ktest_kernel_binary/lib/modules, includes firmware
# when ktest_uki_firmware is set, and writes $ktest_kernel_binary/initramfs.
#
# Runs in a subshell so set -euo pipefail and the cleanup trap stay scoped to
# this build and don't leak into the caller.

function mk_initramfs() (
    set -euo pipefail

    local FIRMWARE=false
    [[ $ktest_uki_firmware == 1 ]] && FIRMWARE=true

    local MODULES_DIR OUTPUT
    MODULES_DIR="$(readlink -f "$ktest_kernel_binary/lib/modules")"
    OUTPUT="$(readlink -f "$ktest_kernel_binary")/initramfs"

    command -v podman &>/dev/null || { echo "podman not found - install podman"; exit 1; }

    local FIRMWARE_DIR="$ktest_dir/../linux-firmware"
    # Fall back to the distro firmware tree (e.g. the linux-firmware apt package
    # inside the ktest container) if no upstream linux-firmware git checkout is
    # present alongside ktest.
    [[ -d $FIRMWARE_DIR ]] || FIRMWARE_DIR=/lib/firmware

    local TMPDIR
    TMPDIR="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR"' EXIT

    local INITRAMFS="$TMPDIR/initramfs"
    mkdir -p "$INITRAMFS"

    # Build the initramfs container image.  Idempotent - re-runs use podman's
    # layer cache, so a no-op rebuild is fast.  Build from a staged context
    # holding only the files the Containerfile COPYs: the whole context is
    # tarred up per build, and when podman runs remote (CONTAINER_HOST inside
    # the pk job containers, see podman_ktest/models.py) it would otherwise
    # stream all of $ktest_dir (.git, target/, ...) over the socket.
    echo "Building initramfs container image ($INITRAMFS_IMAGE_TAG)..."
    local CTX="$TMPDIR/context"
    mkdir -p "$CTX/conf" "$CTX/containers" "$CTX/init"
    cp "$ktest_dir/conf/modparams.conf" \
       "$ktest_dir/conf/setparams.conf" "$CTX/conf/"
    cp "$ktest_dir/containers/Containerfile.initramfs" "$CTX/containers/"
    cp "$ktest_dir/init/initramfs-init.sh" \
       "$ktest_dir/init/initramfs-boot.sh" "$CTX/init/"
    podman build \
	--build-arg "BASE=$INITRAMFS_IMAGE_BASE" \
	-f "$CTX/containers/Containerfile.initramfs" \
	-t "$INITRAMFS_IMAGE_TAG" \
	"$CTX"

    # Extract the image into the staging tree.  No xattrs: rootless tar can't
    # set system.* xattrs, and nothing in the initramfs needs file caps - the
    # shell runs as root anyway.
    echo "Extracting initramfs container image..."
    local cid
    cid=$(podman create "$INITRAMFS_IMAGE_TAG" /bin/true)
    podman export "$cid" | tar -C "$INITRAMFS" -xf -
    podman rm "$cid" >/dev/null

    # Build and install the boot binaries.  /init execs /sbin/ktest-init
    # when the shell user runs `boot`; ktest-init hands lustreroot= mounts
    # off to /sbin/mount.lustreroot (the path is hardcoded in init.c).
    echo "Building init binaries..."
    make -C "$ktest_dir/init"
    echo "Installing ktest-init + mount.lustreroot + zimport..."
    cp "$ktest_dir/init/init" "$INITRAMFS/sbin/ktest-init"
    cp "$ktest_dir/init/mount.lustreroot" "$INITRAMFS/sbin/mount.lustreroot"
    cp "$ktest_dir/init/zimport" "$INITRAMFS/sbin/zimport"

    echo "Copying modules from $MODULES_DIR..."
    mkdir -p "$INITRAMFS/lib/modules"
    cp -a "$MODULES_DIR/." "$INITRAMFS/lib/modules/"

    # Populate firmware. Copy /lib/firmware first as a base so distro-packaged
    # firmware fills any gaps (e.g. GPU firmware not yet in linux-firmware upstream),
    # then overlay the linux-firmware checkout on top so it takes precedence for
    # chips it does have.
    if $FIRMWARE; then
	mkdir -p "$INITRAMFS/lib/firmware"
	local fw_srcs=(/lib/firmware)
	[[ "$FIRMWARE_DIR" != /lib/firmware ]] && fw_srcs+=("$FIRMWARE_DIR")
	local src
	for src in "${fw_srcs[@]}"; do
	    [[ -d "$src" ]] || continue
	    echo "Copying firmware from $src..."
	    cp -a "$src/i915" "$INITRAMFS/lib/firmware/" 2>/dev/null || true
	    cp -a "$src/nvidia" "$INITRAMFS/lib/firmware/" 2>/dev/null || true
	    find "$src" \( -name 'iwlwifi-*.ucode' -o -name 'iwlwifi-*.pnvm' \) | xargs -r cp -t "$INITRAMFS/lib/firmware/" 2>/dev/null || true
	done

	# Copy every firmware blob the packaged modules declare (modinfo -F
	# firmware), preserving the path the driver requests it by.  This covers
	# whatever hardware the kernel supports without per-vendor globs.  The
	# wholesale copies above still matter: i915/nvidia/iwlwifi pick some blob
	# names at runtime (GuC/GSP images, ucode API fallback) that
	# MODULE_FIRMWARE doesn't declare.
	echo "Copying firmware declared by packaged modules..."
	{ find "$INITRAMFS/lib/modules" -name '*.ko*' -print0 \
	      | xargs -0 -r modinfo -F firmware 2>/dev/null || true; \
	  cat "$INITRAMFS"/lib/modules/*/modules.builtin.modinfo 2>/dev/null \
	      | tr '\0' '\n' | sed -n 's/^[^=]*\.firmware=//p' || true; } \
	    | sort -u \
	    | while read -r fw; do
		for src in "${fw_srcs[@]}"; do
		    for f in "$src/$fw" "$src/$fw.zst" "$src/$fw.xz"; do
			[[ -e "$f" ]] || continue
			mkdir -p "$INITRAMFS/lib/firmware/$(dirname "$fw")"
			cp "$f" "$INITRAMFS/lib/firmware/$(dirname "$fw")/"
		    done
		done
	    done || true
	cp "$FIRMWARE_DIR/regulatory.db" "$FIRMWARE_DIR/regulatory.db.p7s" "$INITRAMFS/lib/firmware/" 2>/dev/null || \
	    cp /lib/firmware/regulatory.db /lib/firmware/regulatory.db.p7s "$INITRAMFS/lib/firmware/" 2>/dev/null || \
	    echo "Warning: regulatory.db not found, WiFi regulatory domain will be unavailable"

	# Drop the uncompressed firmware blob whenever a .zst/.xz counterpart is
	# also present.  Distro firmware trees ship some blobs (notably the multi-
	# tens-of-MB NVIDIA GSP images) as both raw and compressed; the loader needs
	# only one, and the compressed form is smaller.  ktest kernels build with
	# CONFIG_FW_LOADER_COMPRESS_{XZ,ZSTD}=y, so the compressed blob loads fine.
	find "$INITRAMFS/lib/firmware" -type f \( -name '*.zst' -o -name '*.xz' \) \
	    -print0 | while IFS= read -r -d '' comp; do
		rm -f "${comp%.*}"
	done
    fi

    # Pack into a compressed cpio archive.  zstd -T0 -10 ratios near gzip -9 but
    # at an order of magnitude higher throughput; kernel decompresses with
    # CONFIG_RD_ZSTD (already on in all ktest configs).
    echo "Packing initramfs..."
    (cd "$INITRAMFS" && find . | cpio -H newc -o --quiet) | zstd -T0 -10 -q -f -o "$OUTPUT"

    echo "Initramfs written to: $OUTPUT"
)
