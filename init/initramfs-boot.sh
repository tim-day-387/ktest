#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# boot - hand control onward from the initramfs shell.
#
# Installed as /usr/local/sbin/boot in the initramfs.  With no arguments it
# mounts the root named on the kernel cmdline and starts real init: it flags
# PID 1 (/init) and exits the shell; /init then execs /sbin/ktest-init, which
# loads the boot-path modules, parses root=/lustreroot=, mounts the root on
# /newroot and switch_roots into it.
#
# Given a UKI path it kexecs into that image instead; the UKI carries its
# own initrd.  The initramfs image ships kexec-tools >= 2.0.30, which has
# the UKI loader (see Documentation/kexec-uki.md).  The kexec path also
# works from a booted system (sudo init/initramfs-boot.sh <image>); there
# the jump goes through systemctl kexec so filesystems unmount cleanly.
#
# Both paths use the default cmdline below rather than the running kernel's:
# -d/-f swap the lustreroot device and fsname, -m overrides or appends
# module options (module_blacklist=, drm.panic_disabled=, mod.param=...).
# kexec passes the result as the new kernel's cmdline; the no-argument
# handoff stages it in /run/ktest-cmdline, which ktest-init prefers over
# /proc/cmdline.  Note that on the handoff path the kernel has already
# booted, so -m overrides are seen by ktest-init but cannot change what the
# running kernel did with its real cmdline.
#
# The modules are pre-loaded here with modprobe(8) first: modprobe resolves
# aliases and '-'/'_' spelling that ktest-init's own modules.dep loader has
# tripped on (e.g. osd_zfs), and ktest-init treats already-loaded modules as
# no-ops.  Parameters come from /etc/modprobe.d as usual.

# The cmdline `boot` assumes; -d/-f/-m rewrite pieces of it and the
# BOOT_IMAGE= token tracks the kernel actually being kexec'd.
default_cmdline='root=/dev/lustre rw lustreroot=rootfs,device=/dev/nvme1n1p1,fsname=dd961847 module_blacklist=nouveau audit=0 drm.panic_disabled=1'

usage() {
    cat <<EOF
Usage: boot [OPTION]... [KERNEL-IMAGE]

With no arguments, mount the root named on the default cmdline and hand off
to real init (/sbin/ktest-init).

With KERNEL-IMAGE, kexec into that UKI instead with the default cmdline;
the UKI carries its own initrd.  A bare name is also looked up under /boot
(mount it here first), e.g. \`boot UkImage-v16-7.1\`.  This works from a
booted system too, where the jump goes through systemctl kexec for a clean
shutdown.

The default cmdline is:
  $default_cmdline

Options:
  -d, --device DEV       lustreroot boot device (default /dev/nvme1n1p1)
  -f, --fsname NAME      lustre fsname (default dd961847)
  -m, --module-opt K=V   override a module option on the cmdline, e.g.
                         -m module_blacklist=nouveau,amdgpu; replaces the
                         token with the same key, appends if none matches
                         (repeatable)
  -n, --no-exec          stage the image (kexec -l) but don't boot into it
  -h, --help             show this help
EOF
}

kernel=
no_exec=false
device=
fsname=
module_opts=()

need_value() {
    [[ $# -ge 2 ]] || { echo "boot: $1 needs a value" >&2; exit 1; }
}

while [[ $# -gt 0 ]]; do
    case $1 in
	-h|--help)
	    usage
	    exit 0
	    ;;
	-n|--no-exec)
	    no_exec=true
	    shift
	    ;;
	-d|--device)
	    need_value "$@"
	    device=$2
	    shift 2
	    ;;
	--device=*)
	    device=${1#*=}
	    shift
	    ;;
	-f|--fsname)
	    need_value "$@"
	    fsname=$2
	    shift 2
	    ;;
	--fsname=*)
	    fsname=${1#*=}
	    shift
	    ;;
	-m|--module-opt)
	    need_value "$@"
	    module_opts+=("$2")
	    shift 2
	    ;;
	--module-opt=*)
	    module_opts+=("${1#*=}")
	    shift
	    ;;
	-*)
	    echo "boot: unknown option: $1" >&2
	    usage >&2
	    exit 1
	    ;;
	*)
	    [[ -z $kernel ]] || { echo "boot: only one kernel image may be given" >&2; exit 1; }
	    kernel=$1
	    shift
	    ;;
    esac
done

if [[ $(id -u) -ne 0 ]]; then
    echo "boot: must be root - re-run under sudo" >&2
    exit 1
fi

# Assemble the cmdline from default_cmdline: swap the device=/fsname=
# fields inside the lustreroot= token, point BOOT_IMAGE= at the kernel
# being kexec'd, then apply -m overrides - each replaces the token sharing
# its key (module_blacklist=..., drm.panic_disabled=..., mod.param=...) or
# is appended when no token matches.
build_cmdline() {
    local -a toks parts
    local i p opt key hit

    read -ra toks <<<"$default_cmdline"

    for i in "${!toks[@]}"; do
	case ${toks[i]} in
	    lustreroot=*)
		IFS=, read -ra parts <<<"${toks[i]#lustreroot=}"
		for p in "${!parts[@]}"; do
		    case ${parts[p]} in
			device=*) [[ -z $device ]] || parts[p]=device=$device ;;
			fsname=*) [[ -z $fsname ]] || parts[p]=fsname=$fsname ;;
		    esac
		done
		toks[i]=lustreroot=$(IFS=,; printf '%s' "${parts[*]}")
		;;
	    BOOT_IMAGE=*)
		[[ -z $kernel ]] || toks[i]=BOOT_IMAGE=$kernel
		;;
	esac
    done

    for opt in "${module_opts[@]}"; do
	key=${opt%%=*}
	hit=false
	for i in "${!toks[@]}"; do
	    if [[ ${toks[i]} == "$key" || ${toks[i]} == "$key="* ]]; then
		toks[i]=$opt
		hit=true
	    fi
	done
	$hit || toks+=("$opt")
    done

    printf '%s' "${toks[*]}"
}

if [[ -n $kernel ]]; then
    # A bare name resolves under /boot, mirroring where qlkbuild installs
    # the UkImage-* UKIs.
    [[ -e $kernel || ! -e /boot/$kernel ]] || kernel=/boot/$kernel
    [[ -e $kernel ]] || { echo "boot: kernel image not found: $kernel" >&2; exit 1; }

    cmdline=$(build_cmdline)
    echo "Staging: $kernel"
    echo "cmdline: $cmdline"
    kexec -l "$kernel" --command-line "$cmdline" || {
	echo "boot: kexec failed to load $kernel" >&2
	exit 1
    }

    if $no_exec; then
	echo "Staged. Boot it with: kexec -e   (or kexec -u to cancel;" \
	     "systemctl kexec on a booted system)"
	exit 0
    fi

    # Wifi won't re-init across kexec without a hardware reset - unload the
    # driver behind every wireless netdev and FLR-reset the device.  The
    # initramfs never loads wifi drivers itself, so this is a no-op unless
    # the shell user brought wifi up by hand.
    for dev in /sys/class/net/*; do
	[[ -e $dev/phy80211 && -e $dev/device/driver/module ]] || continue
	pci=$(basename "$(readlink -f "$dev/device")")
	mod=$(basename "$(readlink -f "$dev/device/driver/module")")
	echo "Unloading $mod (${dev##*/}) and resetting $pci before kexec"
	for holder in "/sys/module/$mod/holders"/*; do
	    [[ -e $holder ]] && modprobe -r "${holder##*/}" 2>/dev/null
	done
	modprobe -r "$mod" 2>/dev/null || true
	[[ -e /sys/bus/pci/devices/$pci/reset ]] && \
	    echo 1 > "/sys/bus/pci/devices/$pci/reset" || true
    done

    echo "Booting into $kernel now..."
    # A booted system has mounted filesystems and running services - let
    # systemd shut down cleanly before the jump.  The initramfs has no
    # systemd; kexec directly.
    [[ -d /run/systemd/system ]] && exec systemctl kexec
    sync
    exec kexec -e
elif $no_exec; then
    echo "boot: --no-exec needs a kernel image to kexec" >&2
    exit 1
fi

# The no-argument handoff is only meaningful as the initramfs shell's
# `boot`; anywhere else it would HUP the caller's login shell.
if [[ ! -e /init || ! -x /sbin/ktest-init ]]; then
    echo "boot: not in the ktest initramfs - pass a kernel image to kexec" >&2
    exit 1
fi

modprobe -a -q nvme zfs lnet ksocklnd lustre osd_zfs || true

# Stage the assembled cmdline where ktest-init looks before falling back
# to /proc/cmdline; ktest-init consumes the file.
cmdline=$(build_cmdline)
echo "$cmdline" > /run/ktest-cmdline
echo "cmdline: $cmdline"

touch /run/ktest-boot
echo "boot armed: handing off to /sbin/ktest-init"
kill -HUP $PPID
