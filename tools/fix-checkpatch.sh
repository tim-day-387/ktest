#!/bin/bash
# Run checkpatch.pl --fix-inplace over Lustre kernel source files in parallel.
#
# Usage: ./fix-checkpatch.sh [path ...]
#   Defaults to the Lustre kernel module trees if no path is given.
#   Userspace tests/ and utils/ directories are always excluded.

set -euo pipefail

CHECKPATCH="${CHECKPATCH:-$HOME/Programming/linux/scripts/checkpatch.pl}"
JOBS="${JOBS:-10}"

# Lustre kernel code lives in these trees.
KERNEL_PATHS=(libcfs lnet lustre include)

if [[ ! -x "$CHECKPATCH" ]]; then
	echo "checkpatch.pl not found or not executable: $CHECKPATCH" >&2
	exit 1
fi

paths=("$@")
if [[ ${#paths[@]} -eq 0 ]]; then
	paths=("${KERNEL_PATHS[@]}")
fi

git ls-files -- "${paths[@]}" |
	grep -Ev '(^|/)(tests|utils)/' |
	xargs -P"$JOBS" -n1 "$CHECKPATCH" --fix-inplace
