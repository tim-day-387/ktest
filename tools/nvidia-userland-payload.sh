#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# nvidia-userland-payload.sh - pack the NVIDIA driver userland for the UKI
#
# Usage: nvidia-userland-payload.sh <driver-version> <output-dir>
#
# The NVIDIA kernel modules refuse any userland that is not the exact same
# driver release (RmPerformVersionCheck in open-gpu-kernel-modules compares
# the full version string), so ktest ships the userland alongside the
# modules it builds instead of baking a copy into the desktop root image.
# This script produces that payload:
#
#   <output-dir>/nvidia-userland.squashfs
#       Every file NVIDIA's .run installer creates (libraries, the X driver,
#       nvidia-smi, the setuid nvidia-modprobe, the glvnd/Vulkan/OpenCL
#       vendor JSONs, ...) laid out as it would appear under /, plus a
#       .ktest-overlay manifest at the root naming the directories
#       ktest-init overlays onto the booted root filesystem (see
#       overlay_nvidia_userland in init/init.c).
#   <output-dir>/firmware/
#       The GSP firmware blobs the modules load.  Delivered separately:
#       mk_initramfs puts them under /lib/firmware/nvidia/<version>, which
#       ktest-init bind-mounts over the root along with /lib/modules.
#
# The payload is built by really installing the driver into this system
# (--no-kernel-modules) and diffing the file list before and after.  That
# modifies whatever it runs in, so only run it inside a throwaway container:
# see the nvidia-userland stage in containers/Containerfile.ktest.u26.
#
# NVIDIA_RUN_FILE=<path> skips the download and uses that .run instead.

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <driver-version> <output-dir>" >&2
    exit 1
fi

ver=$1
out=$(realpath -m "$2")
run=NVIDIA-Linux-x86_64-$ver.run

for tool in curl mksquashfs; do
    command -v "$tool" >/dev/null || { echo "$tool not found" >&2; exit 1; }
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"

if [[ -n ${NVIDIA_RUN_FILE:-} ]]; then
    cp "$NVIDIA_RUN_FILE" "$run"
else
    curl -fsSL -O "https://us.download.nvidia.com/XFree86/Linux-x86_64/$ver/$run"
fi
sh "$run" --extract-only --target nvidia-run

# Snapshot the regular files and symlinks on the root filesystem; the
# installer only ever adds these two kinds.
snapshot()
{
    find / -xdev \( -type f -o -type l \) -not -path "$work/*" 2>/dev/null \
	| LC_ALL=C sort
}

snapshot > before

# --no-install-libglvnd: the desktop image already carries the distro's
#   libglvnd (libGL/libEGL dispatch), which is what we want the NVIDIA
#   vendor libraries to plug into.
# --no-systemd: the suspend/hibernate units are useless in the VM and would
#   drag the systemd unit directories into the overlay set.
# --no-x-check / --no-nouveau-check: no X server or GPU in a build container.
(cd nvidia-run && ./nvidia-installer --silent \
    --no-kernel-modules --no-nouveau-check --no-x-check \
    --no-install-libglvnd --no-systemd --no-wine-files \
    --no-check-for-alternate-installs --no-backup \
    --no-rebuild-initramfs --no-distro-scripts)

snapshot > after

# Everything the installer added, minus:
#   /var, /tmp, ld.so.cache   installer bookkeeping and side effects
#   /usr/share/doc            the HTML README
#   /usr/lib/nvidia           install-time glvnd sanity-check helpers
#   /usr/lib/firmware         handed over via firmware/ instead (see above)
#   /usr/lib/libGL.so.1       legacy non-multiarch glvnd dispatch symlink; the
#                             desktop's libglvnd already provides libGL
LC_ALL=C comm -13 before after \
    | grep -vE '^/(tmp/|var/|etc/ld\.so\.cache$|usr/share/doc/|usr/lib/nvidia/|usr/lib/firmware/|usr/lib/libGL\.so\.1$)' \
    > files

[[ -s files ]] || { echo "nvidia-installer installed nothing?" >&2; exit 1; }

mkdir -p payload "$out/firmware"

# tar preserves symlinks, modes (the setuid bit on nvidia-modprobe) and
# strips the leading / for us.
tar --no-recursion --files-from=files -cf - 2>/dev/null | tar -C payload -xpf -

cp nvidia-run/firmware/*.bin "$out/firmware/"

# Overlay manifest: the directories holding installed files, pruned to the
# outermost ones (a kept directory's subtree rides along in the overlay).
# Files are sorted, so an ancestor always precedes its descendants.
sed 's#/[^/]*$##' files | LC_ALL=C sort -u | sed 's#^/##' \
    | awk '{
	for (i = 1; i <= n; i++)
	    if (index($0 "/", kept[i]) == 1)
		next;
	kept[++n] = $0 "/";
	print;
    }' > payload/.ktest-overlay

# A file dropped straight into one of these would make init overlay the
# whole hierarchy; refuse rather than ship that.
if bad=$(grep -xE 'etc|lib|usr|usr/lib|usr/share|usr/local' payload/.ktest-overlay); then
    echo "overlay manifest names a top-level directory ($bad); files:" >&2
    grep -E "^/($(paste -sd'|' <<< "$bad"))/[^/]+\$" files >&2
    exit 1
fi

echo "Overlay directories:"
sed 's/^/  /' payload/.ktest-overlay

mksquashfs payload "$out/nvidia-userland.squashfs" \
    -comp zstd -Xcompression-level 19 -all-root -no-xattrs -noappend -quiet

echo "Wrote $out/nvidia-userland.squashfs"
