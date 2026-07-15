#!/bin/sh
# SPDX-License-Identifier: Apache-2.0

#
# gen_db.sh TREE [SRC...]
#
# Write TREE/compile_commands.json with one entry compiling each SRC
# (a path relative to TREE). Used by the lit tests to fabricate the
# compilation database xunused runs on.
#

tree=$1
shift

{
    echo '['
    first=1
    for src in "$@"; do
        [ "$first" = 1 ] || printf ',\n'
        first=0
        printf '  {"directory": "%s", "command": "cc -c %s", "file": "%s"}' \
            "$tree" "$src" "$src"
    done
    [ "$first" = 1 ] || printf '\n'
    echo ']'
} > "$tree/compile_commands.json"
