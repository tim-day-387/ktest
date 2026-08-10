#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# format-efi-disk.sh - format a block device as a single EFI system
# partition.
#
# Wipes the device, writes a GPT holding one ESP-typed partition spanning
# the whole disk, and formats it FAT32.  On success, prints instructions
# for installing a UKI (the UkImage-* files qlkbuild packages) onto the
# new partition.

set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 /dev/DEVICE

DESTROYS everything on /dev/DEVICE and repartitions it as a single EFI
system partition (GPT + FAT32).
EOF
}

if [[ $# -ne 1 || $1 == -h || $1 == --help ]]; then
    usage
    exit 1
fi

dev=$1

if [[ $(id -u) -ne 0 ]]; then
    echo "$0: must be root - re-run under sudo" >&2
    exit 1
fi

[[ -b $dev ]] || { echo "$0: not a block device: $dev" >&2; exit 1; }

case $(lsblk -no TYPE "$dev" | head -1) in
    disk|loop) ;;
    *)
	echo "$0: $dev is not a whole disk (give the disk, not a partition)" >&2
	exit 1
	;;
esac

if lsblk -no MOUNTPOINTS "$dev" | grep -q .; then
    echo "$0: $dev has mounted filesystems - unmount them first:" >&2
    lsblk "$dev" >&2
    exit 1
fi

echo "About to repartition and format:"
echo
lsblk -o NAME,SIZE,MODEL,FSTYPE,MOUNTPOINTS "$dev"
echo
read -rp "This DESTROYS all data on $dev. Type the device path to continue: " confirm
if [[ $confirm != "$dev" ]]; then
    echo "$0: aborted" >&2
    exit 1
fi

wipefs -a "$dev" >/dev/null

# One ESP-typed partition (type=uefi) filling the disk.
sfdisk --quiet "$dev" <<EOF
label: gpt
type=uefi, name="ESP"
EOF

# Wait for the kernel/udev to surface the new partition node (nvme-style
# devices get a 'p1' suffix, sd-style a bare '1' - ask lsblk rather than
# guessing).
udevadm settle 2>/dev/null || true
blockdev --rereadpt "$dev" 2>/dev/null || true
part=$(lsblk -nrpo NAME,TYPE "$dev" | awk '$2 == "part" { print $1; exit }')
[[ -n $part ]] || { echo "$0: new partition did not appear on $dev" >&2; exit 1; }

mkfs.vfat -F 32 -n ESP "$part" >/dev/null

cat <<EOF

EFI system partition ready: $part

To install a UKI (qlkbuild packages them as /tmp/ktest-output/UkImage-*):

  sudo mount $part /mnt
  sudo mkdir -p /mnt/EFI/BOOT
  sudo cp UkImage-XXX /mnt/EFI/BOOT/BOOTX64.EFI
  sudo cp UkImage-XXX /mnt/
  sudo umount /mnt

BOOTX64.EFI is the firmware's removable-media fallback path, so the disk
boots with no NVRAM entry needed.  The named copy at the partition root
is for the initramfs shell: with the ESP mounted at /boot, \`boot
UkImage-XXX\` kexecs it by name.

To register a proper boot entry instead of relying on the fallback path:

  sudo efibootmgr --create --disk $dev --part 1 --label ktest \\
      --loader '\\EFI\\BOOT\\BOOTX64.EFI'
EOF
