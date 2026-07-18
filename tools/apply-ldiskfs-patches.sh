#!/bin/bash
# SPDX-License-Identifier: GPL-2.0

set -e

if [ $# -ne 3 ]; then
    echo "Usage: $0 <lustre-dir> <linux-dir> <series-file>"
    echo "Example: $0 /path/to/lustre /path/to/linux ldiskfs-6.12-ml.series"
    exit 1
fi

LUSTRE_DIR="$1"
LINUX_DIR="$2"
SERIES_FILE="$3"

SERIES_PATH="$LUSTRE_DIR/ldiskfs/kernel_patches/series/$SERIES_FILE"
PATCHES_DIR="$LUSTRE_DIR/ldiskfs/kernel_patches/patches"

if [ ! -f "$SERIES_PATH" ]; then
    echo "Error: Series file not found: $SERIES_PATH"
    exit 1
fi

if [ ! -d "$LINUX_DIR" ]; then
    echo "Error: Linux directory not found: $LINUX_DIR"
    exit 1
fi

cd "$LINUX_DIR"

while IFS= read -r patch || [ -n "$patch" ]; do
    [ -z "$patch" ] && continue
    [[ "$patch" =~ ^[[:space:]]*# ]] && continue

    patch_file="$PATCHES_DIR/$patch"

    if [ ! -f "$patch_file" ]; then
        echo "Error: Patch file not found: $patch_file"
        exit 1
    fi

    echo "Applying: $patch"
    if ! patch -p1 < "$patch_file"; then
        echo "Error: Failed to apply $patch"
        exit 1
    fi
    git add -A
    git reset -- '*.orig'
    git commit -m "Apply $patch"
done < "$SERIES_PATH"

echo "All patches applied successfully"
