#!/bin/bash
#
# List things that aren't wired up:
#
#   1. job files (*.json) that aren't referenced by any *.group file.
#   2. ktest files under tests/fs/lustre/ that aren't referenced by any
#      job (*.json) file.
#
# Group files list jobs by path relative to jobs/, without the .json
# extension, one per line (blank lines and #-comments are ignored).
#
# Jobs reference a ktest by path relative to the repo root (with the
# .ktest extension) in their "build" and "run" fields.

set -euo pipefail

jobs_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$jobs_dir/.." && pwd)"
tests_subdir="tests/fs/lustre"

# Jobs not named in any group file.
grouped="$(cat "$jobs_dir"/*.group 2>/dev/null |
	sed 's/#.*//' |
	grep -v '^[[:space:]]*$' |
	sed 's/^[[:space:]]*//;s/[[:space:]]*$//' |
	sort -u)"

echo "# Jobs not in any group:"
find "$jobs_dir" -name '*.json' -printf '%P\n' |
	sed 's/\.json$//' |
	sort -u |
	comm -23 - <(echo "$grouped")

# ktest files not referenced by any job file.
referenced="$(grep -rhoE "$tests_subdir/[^\"]+\.ktest" "$jobs_dir" |
	sort -u)"

echo "# Lustre ktests not in any job:"
find "$repo_dir/$tests_subdir" -name '*.ktest' -printf "$tests_subdir/%P\n" |
	sort -u |
	comm -23 - <(echo "$referenced")
