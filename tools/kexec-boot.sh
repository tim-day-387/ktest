#!/bin/bash
# kexec into one of the systemd-stub UKIs in /boot.
#
# Usage: sudo ./kexec-boot.sh [image]
#   image   Path or basename of a /boot/UkImage-* (default: newest one).
#
# Needs kexec-tools >= 2.0.30 for the UKI loader (see Documentation/kexec-uki.md).
# The UKIs carry no embedded cmdline, so the running cmdline is reused.
#
# Env:
#   WIFI_PCI=0000:00:14.3   Unload iwlwifi before the jump (it won't survive
#                           kexec without a hardware reset) and FLR-reset the
#                           device. Leave unset to skip wifi handling.
#   NO_EXEC=1               Stage the image (kexec -l) but don't boot into it.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Must be run as root; re-run with: sudo $0 $*" >&2
    exit 1
fi

KEXEC="${KEXEC:-kexec}"
WIFI_PCI="${WIFI_PCI:-0000:00:14.3}"

# Resolve the image: explicit arg, else the newest UkImage in /boot.
img="${1:-}"
if [ -z "$img" ]; then
    img=$(ls -1t /boot/UkImage-* 2>/dev/null | head -1)
    [ -n "$img" ] || { echo "No /boot/UkImage-* found" >&2; exit 1; }
elif [ ! -e "$img" ]; then
    img="/boot/$img"
fi
[ -e "$img" ] || { echo "Image not found: $img" >&2; exit 1; }

echo "Staging: $img"
"$KEXEC" -l "$img" --reuse-cmdline

if [ "${NO_EXEC:-0}" = 1 ]; then
    echo "Staged (NO_EXEC set). Boot it with: $KEXEC -e   (or $KEXEC -u to cancel)"
    exit 0
fi

# CNVi wifi won't re-init across kexec (no firmware/PCI reset) — drop it cleanly.
if [ -n "${WIFI_PCI:-}" ]; then
    echo "Unloading iwlwifi and resetting $WIFI_PCI before kexec"
    modprobe -r iwlmvm iwlwifi 2>/dev/null || true
    [ -e "/sys/bus/pci/devices/$WIFI_PCI/reset" ] && \
        echo 1 > "/sys/bus/pci/devices/$WIFI_PCI/reset" || true
fi

echo "Booting into $img now..."
exec "$KEXEC" -e
