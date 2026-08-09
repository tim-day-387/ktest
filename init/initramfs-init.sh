#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# ktest initramfs /init - always boots to an interactive bash shell.
#
# The initramfs userspace is a stripped-down Ubuntu container image (see
# containers/Containerfile.initramfs); mk_initramfs overlays the freshly
# built kernel's modules in /lib/modules and installs the compiled boot
# binaries (/sbin/ktest-init + /sbin/mount.lustreroot).  This script mounts
# the kernel filesystems and hands the console to bash.  Modules are loaded
# by hand with modprobe(8); parameters in /etc/modprobe.d apply as usual.
#
# Running `boot` in the shell mounts the root named on the kernel cmdline
# and starts real init: it flags /run/ktest-boot and exits the shell, and
# the loop below then execs /sbin/ktest-init as PID 1.  A plain `exit` just
# respawns the shell.

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
# /init starts with no TERM; the kernel console is linux-compatible, and
# without this clear(1) and friends fall back to a dumb terminal.
export TERM=linux

mount -t proc     -o nosuid,nodev,noexec proc     /proc
mount -t sysfs    -o nosuid,nodev,noexec sysfs    /sys
mount -t devtmpfs -o nosuid              devtmpfs /dev
mkdir -p /dev/pts /dev/shm
mount -t devpts   -o nosuid,noexec,gid=5,mode=620 devpts /dev/pts
mount -t tmpfs    -o nosuid,nodev                 tmpfs  /dev/shm
mount -t tmpfs    -o nosuid,nodev                 tmpfs  /run
mount -t tmpfs                                    tmpfs  /tmp

# Quiet the console: only err and worse may print, so kernel chatter (late
# module loads, firmware probes) stops scribbling over the shell.  The full
# log stays available via dmesg.  Messages printed before /init runs are
# controlled from the kernel cmdline (quiet / loglevel=) instead.
dmesg -n 4

# Load the input stack: the initramfs has no udev, so on a physical console
# the USB host-controller and HID keyboard drivers (modules in ktest
# kernels) must be loaded by hand before the shell can read the keyboard.
# Serial consoles work without any of these; missing modules are ignored.
modprobe -a -q xhci_pci ehci_pci ohci_pci uhci_hcd usbhid hid_generic atkbd

# PID 1 must never exit or the kernel panics; respawn the shell forever.
# setsid --ctty gives the shell a controlling terminal so job control works.
# Clear the boot scrollback before each shell so it starts on a clean
# screen; dmesg still has the full log.
while true; do
    clear 2>/dev/null || printf '\033[H\033[2J'
    echo "ktest initramfs: bash shell ('boot' mounts the root and starts init)"
    setsid --ctty --wait bash -l
    if [[ -e /run/ktest-boot && -x /sbin/ktest-init ]]; then
	exec /sbin/ktest-init
    fi
done
