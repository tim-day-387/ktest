#!/bin/bash

VER=20
BINDIR=/usr/bin
LLVMDIR=/usr/lib/llvm-${VER}/bin

updated=0
created=0
skipped=0

# Update existing unversioned symlinks that point to llvm-18
for link in "$BINDIR"/clang* "$BINDIR"/llvm-*; do
    name=$(basename "$link")
    # Skip versioned links (ending in -digits)
    [[ "$name" =~ -[0-9]+$ ]] && continue
    # Skip non-symlinks
    [ -L "$link" ] || continue

    target=$(readlink "$link")
    [[ "$target" == *"llvm-18"* ]] || continue

    binname=$(basename "$target")
    new_target="../lib/llvm-${VER}/bin/${binname}"

    if [ -f "$LLVMDIR/$binname" ]; then
        ln -sf "$new_target" "$link"
        echo "Updated: $link -> $new_target"
        ((updated++))
    else
        echo "Warning: $LLVMDIR/$binname not found, skipping $link"
        ((skipped++))
    fi
done

# Create unversioned symlinks for -20 tools that have no unversioned entry yet
for versioned in "$BINDIR"/*-${VER}; do
    [ -L "$versioned" ] || continue
    versioned_name=$(basename "$versioned")
    base_name="${versioned_name%-${VER}}"
    unversioned="$BINDIR/$base_name"

    [ -e "$unversioned" ] && continue

    binname=$(basename "$(readlink "$versioned")")
    new_target="../lib/llvm-${VER}/bin/${binname}"

    if [ -f "$LLVMDIR/$binname" ]; then
        ln -sf "$new_target" "$unversioned"
        echo "Created:  $unversioned -> $new_target"
        ((created++))
    fi
done

echo ""
echo "Done: $updated updated, $created created, $skipped skipped."
