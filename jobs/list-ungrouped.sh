#!/bin/bash
#
# List job files (*.json) that aren't referenced by any *.group file.
#
# Group files list jobs by path relative to jobs/, without the .json
# extension, one per line (blank lines and #-comments are ignored).

set -euo pipefail

jobs_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Collect every job named in a group file.
grouped="$(cat "$jobs_dir"/*.group 2>/dev/null |
	sed 's/#.*//' |
	grep -v '^[[:space:]]*$' |
	sed 's/^[[:space:]]*//;s/[[:space:]]*$//' |
	sort -u)"

# Compare against every job file present, listing the ones not grouped.
find "$jobs_dir" -name '*.json' -printf '%P\n' |
	sed 's/\.json$//' |
	sort -u |
	comm -23 - <(echo "$grouped")
