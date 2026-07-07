#!/bin/bash
#
# detect-zfs-nvidia-refs.sh
#
# Detect ZFS or NVIDIA references that have leaked into the Lustre codebase
# outside of the directories where they legitimately belong:
#
#   ZFS backend code       -> lustre/osd-zfs/
#   Compatibility shims    -> lustre_compat/ and include/lustre_compat/
#
# Any ZFS or NVIDIA reference found *elsewhere* is reported, since such code
# ought to live behind those abstraction boundaries.
#
# Usage:
#   contrib/detect-zfs-nvidia-refs.sh [options] [lustre-tree]
#
# Options:
#   -a, --all       Search all tracked files (default: *.c and *.h only)
#   -z, --zfs       Only search for ZFS references
#   -n, --nvidia    Only search for NVIDIA references
#   -c, --count     Print only a per-file count summary
#   -q, --quiet     Suppress the match listing (still sets exit status)
#   -h, --help      Show this help
#
# Arguments:
#   lustre-tree     Path to the lustre-release checkout
#                   (default: the git tree this script lives in)
#
# Exit status:
#   0  no stray references found
#   1  stray references found
#   2  usage / environment error
#

set -u

# --- pattern definitions ----------------------------------------------------

# ZFS: the "zfs" token plus internal ZFS/SPL API prefixes that never contain
# the string "zfs" but are unmistakably ZFS-only.
ZFS_PATTERN='zfs|\bdmu_|\bdsl_|\bspa_|\bdnode|\bobjset|\bzap_|\bzpl_|\bdbuf_|\bsa_handle|\btxg_'

# NVIDIA: GPUDirect Storage / nvfs peer-memory integration.
NVIDIA_PATTERN='nvidia|\bnvfs|gpu_?direct|gpudirect|\bcufile|\bgds_|peer_memory'

# Directories where ZFS/NVIDIA references are expected and therefore ignored.
EXCLUDES=(
	':(exclude)lustre/osd-zfs/**'
	':(exclude)lustre_compat/**'
	':(exclude)include/lustre_compat/**'
)

# --- option parsing ---------------------------------------------------------

want_zfs=1
want_nvidia=1
count_only=0
quiet=0
globs=('*.c' '*.h')
tree=""

usage() {
	sed -n '2,40p' "$0" | sed 's/^#//; s/^ //'
	exit "${1:-0}"
}

while [ $# -gt 0 ]; do
	case "$1" in
	-a|--all)    globs=() ;;
	-z|--zfs)    want_nvidia=0 ;;
	-n|--nvidia) want_zfs=0 ;;
	-c|--count)  count_only=1 ;;
	-q|--quiet)  quiet=1 ;;
	-h|--help)   usage 0 ;;
	-*)          echo "unknown option: $1" >&2; usage 2 ;;
	*)           tree="$1" ;;
	esac
	shift
done

# --- locate the lustre tree -------------------------------------------------

if [ -z "$tree" ]; then
	# Default to the git tree containing this script.
	tree="$(cd "$(dirname "$0")" && git rev-parse --show-toplevel 2>/dev/null)"
fi

if [ -z "$tree" ] || [ ! -d "$tree" ]; then
	echo "error: could not locate a lustre tree (pass one as an argument)" >&2
	exit 2
fi

if ! git -C "$tree" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	echo "error: '$tree' is not a git work tree" >&2
	exit 2
fi

if [ ! -d "$tree/lustre/osd-zfs" ]; then
	echo "warning: '$tree' does not look like a lustre-release checkout" >&2
fi

# --- colors (only when writing to a terminal) -------------------------------

if [ -t 1 ]; then
	BOLD=$'\e[1m'; RED=$'\e[31m'; CYAN=$'\e[36m'; DIM=$'\e[2m'; RST=$'\e[0m'
	grep_color=always
else
	BOLD=""; RED=""; CYAN=""; DIM=""; RST=""
	grep_color=never
fi

# --- search helper ----------------------------------------------------------

# search <label> <pattern>
# Returns 0 if any matches were found.
search() {
	local label="$1" pattern="$2"
	local matches

	matches="$(git -C "$tree" grep -nI --color="$grep_color" -iE "$pattern" \
		-- "${globs[@]}" "${EXCLUDES[@]}" 2>/dev/null)"

	if [ -z "$matches" ]; then
		printf '%s%s%s: none\n' "$BOLD" "$label" "$RST"
		return 1
	fi

	local nfiles nlines
	nlines="$(printf '%s\n' "$matches" | wc -l)"
	nfiles="$(printf '%s\n' "$matches" | cut -d: -f1 | sort -u | wc -l)"

	printf '%s%s%s: %s%d%s matches in %s%d%s files\n' \
		"$BOLD" "$label" "$RST" "$RED" "$nlines" "$RST" "$RED" "$nfiles" "$RST"

	if [ "$quiet" -eq 1 ]; then
		return 0
	fi

	if [ "$count_only" -eq 1 ]; then
		printf '%s\n' "$matches" | cut -d: -f1 | sort | uniq -c | sort -rn |
			while read -r n file; do
				printf '  %s%4d%s  %s\n' "$RED" "$n" "$RST" "$file"
			done
	else
		printf '%s\n' "$matches" | sed 's/^/  /'
	fi
	printf '\n'
	return 0
}

# --- run --------------------------------------------------------------------

printf '%sScanning %s%s\n' "$DIM" "$tree" "$RST"
printf '%s(excluding lustre/osd-zfs and lustre_compat)%s\n\n' "$DIM" "$RST"

found=0

if [ "$want_zfs" -eq 1 ]; then
	search "ZFS references" "$ZFS_PATTERN" && found=1
fi

if [ "$want_nvidia" -eq 1 ]; then
	search "NVIDIA references" "$NVIDIA_PATTERN" && found=1
fi

exit "$found"
