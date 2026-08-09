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
# Given a UKI path it kexecs into that image instead, reusing the current
# kernel cmdline; the UKI carries its own initrd.  The initramfs image
# ships kexec-tools >= 2.0.30, which has the UKI loader (see
# Documentation/kexec-uki.md).  The kexec path also works from a booted
# system (sudo init/initramfs-boot.sh <image>); there the jump goes through
# systemctl kexec so filesystems unmount cleanly.
#
# The modules are pre-loaded here with modprobe(8) first: modprobe resolves
# aliases and '-'/'_' spelling that ktest-init's own modules.dep loader has
# tripped on (e.g. osd_zfs), and ktest-init treats already-loaded modules as
# no-ops.  Parameters come from /etc/modprobe.d as usual.

usage() {
    cat <<EOF
Usage: boot [OPTION]... [KERNEL-IMAGE]

With no arguments, mount the root named on the kernel cmdline and hand off
to real init (/sbin/ktest-init).

With KERNEL-IMAGE, kexec into that UKI instead, reusing the current
kernel cmdline; the UKI carries its own initrd.  A bare name is also
looked up under /boot (mount it here first), e.g. \`boot UkImage-v16-7.1\`.
This works from a booted system too, where the jump goes through
systemctl kexec for a clean shutdown.

Options:
  -n, --no-exec       stage the image (kexec -l) but don't boot into it
  -h, --help          show this help
EOF
}

kernel=
no_exec=false

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

if [[ -n $kernel ]]; then
    # A bare name resolves under /boot, mirroring where qlkbuild installs
    # the UkImage-* UKIs.
    [[ -e $kernel || ! -e /boot/$kernel ]] || kernel=/boot/$kernel
    [[ -e $kernel ]] || { echo "boot: kernel image not found: $kernel" >&2; exit 1; }

    echo "Staging: $kernel"
    kexec -l "$kernel" --reuse-cmdline || {
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

touch /run/ktest-boot
echo "boot armed: handing off to /sbin/ktest-init"
kill -HUP $PPID
