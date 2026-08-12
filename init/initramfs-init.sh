#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# ktest initramfs /init - boots to an interactive bash shell, unless
# ktest.bootnow on the kernel cmdline says to boot straight through.
#

# Setup environment
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
export TERM=linux

# Mount important virtual filesystems
mountpoint -q /proc    || mount -t proc     -o nosuid,nodev,noexec proc     /proc
mountpoint -q /sys     || mount -t sysfs    -o nosuid,nodev,noexec sysfs    /sys
mountpoint -q /dev     || mount -t devtmpfs -o nosuid              devtmpfs /dev
mkdir -p /dev/pts /dev/shm
mountpoint -q /dev/pts || mount -t devpts   -o nosuid,noexec,gid=5,mode=620 devpts /dev/pts
mountpoint -q /dev/shm || mount -t tmpfs    -o nosuid,nodev                 tmpfs  /dev/shm
mountpoint -q /run     || mount -t tmpfs    -o nosuid,nodev                 tmpfs  /run
mountpoint -q /tmp     || mount -t tmpfs                                    tmpfs  /tmp

# Silence boot spam
dmesg -n 4

# Load critical modules
MODULES=(
    xhci_pci ehci_pci ohci_pci uhci_hcd
    usbhid hid_generic atkbd nvme zfs
    lnet ksocklnd lustre osd_zfs iwlwifi
    iwlmvm mt7921e
)
modprobe -a -q "${MODULES[@]}"

# Instant boot: hand off without ever spawning a shell
if [[ ${1-} != ktest-boot-failed ]] && grep -qw 'ktest\.bootnow' /proc/cmdline; then
    if [[ -x /sbin/ktest-init ]]; then
	exec /sbin/ktest-init
    fi
    echo "ktest.bootnow: handoff failed, dropping to the initramfs shell"
fi

# PID 1 must never exit or the kernel panics
while true; do
    clear 2>/dev/null || printf '\033[H\033[2J'
    echo "ktest initramfs: bash shell ('boot' mounts the root and starts init)"
    if [[ ${1-} == ktest-boot-failed ]]; then
	echo "boot failed - back in the initramfs shell, dmesg has the log"
	shift
    fi
    setsid --ctty --wait bash -l
    if [[ -e /run/ktest-boot && -x /sbin/ktest-init ]]; then
	# Consume the flag: if ktest-init fails and execs back into this
	# script, a stale flag would re-run it instead of holding the shell.
	rm -f /run/ktest-boot
	exec /sbin/ktest-init
    fi
done
