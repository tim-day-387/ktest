#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# check-config-macros.sh
#
# List usage of kernel CONFIG_* macros across the Lustre source tree,
# broken down by subdirectory and by file type.
#
# Kernel code should only reference CONFIG_* macros from .c files.  A
# CONFIG_* test in a header is a hazard: the header can be included by a
# translation unit that never pulled in the kernel autoconf, silently
# taking the "disabled" branch and, worse, changing a struct layout so
# that objects built with and without the option no longer agree on ABI.
#
# This script reports, per subdirectory, how many CONFIG_* references
# live in .c versus .h files.  Header references are the actionable ones
# and are listed individually so they can be moved into .c files.
#
# Only genuine kernel-style CONFIG_* references are counted, i.e. those
# used in preprocessor conditionals (#if / #ifdef / #ifndef / #elif) or
# in IS_ENABLED() / IS_BUILTIN() / IS_MODULE().  Lustre's own value-like
# identifiers that happen to start with CONFIG_ (CONFIG_SUB_CLIENT,
# CONFIG_READ, CONFIG_CMD, ...) are not preprocessor tests and are
# therefore ignored.
#
# Usage:
#   contrib/scripts/check-config-macros.sh [-v] [-H] [PATH ...]
#
#   -v   verbose: list every matching reference (file:line: macro)
#   -H   only report header (.h) references (the violations)
#   PATH one or more trees to scan (default: lustre lnet libcfs ldiskfs)
#
# Exit status is the number of header references found (0 == clean),
# capped at 255, so it can gate a build or a commit hook.

set -u

# A CONFIG_ token with an identifier boundary in front so we do not pick
# up tails of longer names such as _LUSTRE_CONFIG_H.
TOKEN='(?<![A-Za-z0-9_])CONFIG_[A-Z0-9_]+'

# A line that actually tests a CONFIG_ macro: a preprocessor conditional
# or an IS_ENABLED()/IS_BUILTIN()/IS_MODULE() invocation.
COND="(^[[:space:]]*#[[:space:]]*(if|ifdef|ifndef|elif)\b.*${TOKEN})|(IS_ENABLED|IS_BUILTIN|IS_MODULE)[[:space:]]*\([[:space:]]*${TOKEN}"

verbose=0
headers_only=0

while getopts "vHh" opt; do
	case "$opt" in
	v) verbose=1 ;;
	H) headers_only=1 ;;
	h)
		sed -n '2,40p' "$0" | sed 's/^#\?//'
		exit 0
		;;
	*)
		echo "usage: $0 [-v] [-H] [PATH ...]" >&2
		exit 2
		;;
	esac
done
shift $((OPTIND - 1))

# Run from the top of the tree so reported paths are repo-relative.
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)" || exit 2

trees=("$@")
[ ${#trees[@]} -eq 0 ] && trees=(lustre lnet libcfs ldiskfs)

present=()
for t in "${trees[@]}"; do
	[ -d "$t" ] && present+=("$t")
done
if [ ${#present[@]} -eq 0 ]; then
	echo "no source trees found (looked for: ${trees[*]})" >&2
	exit 2
fi

# Every matching reference as: <path>\t<line>\t<macro>
matches=$(grep -rnP --include='*.c' --include='*.h' "$COND" "${present[@]}" 2>/dev/null |
	while IFS=: read -r file line rest; do
		echo "$rest" | grep -oP "$TOKEN" | while read -r macro; do
			printf '%s\t%s\t%s\n' "$file" "$line" "$macro"
		done
	done)

if [ -z "$matches" ]; then
	echo "No CONFIG_* macro usage found."
	exit 0
fi

# subsystem = first two path components, e.g. lustre/llite, lnet/klnds.
subsys() { echo "$1" | awk -F/ '{ if (NF>=2) print $1"/"$2; else print $1 }'; }

if [ "$headers_only" -eq 1 ]; then
	echo "Header CONFIG_* references (should be moved to .c files):"
	echo
	printf '%s\n' "$matches" | awk -F'\t' '$1 ~ /\.h$/' |
		sort -t/ -k1,2 | while IFS=$'\t' read -r file line macro; do
			printf '  %s:%s: %s\n' "$file" "$line" "$macro"
		done
	nh=$(printf '%s\n' "$matches" | awk -F'\t' '$1 ~ /\.h$/' | wc -l)
	echo
	echo "Total header references: $nh"
	[ "$nh" -gt 255 ] && nh=255
	exit "$nh"
fi

if [ "$verbose" -eq 1 ]; then
	echo "All CONFIG_* references (C = ok, H = should move to .c):"
	echo
	printf '%s\n' "$matches" | sort -t/ -k1 |
		while IFS=$'\t' read -r file line macro; do
			kind=C; [ "${file##*.}" = h ] && kind=H
			printf '  [%s] %s:%s: %s\n' "$kind" "$file" "$line" "$macro"
		done
	echo
fi

# Per-subsystem summary table.
printf '%-24s %7s %7s %7s %7s   %s\n' \
	SUBSYSTEM C-refs C-files H-refs H-files MACROS
printf '%-24s %7s %7s %7s %7s   %s\n' \
	------------------------ ------ ------- ------ ------- ------

total_c=0
total_h=0
printf '%s\n' "$matches" | while IFS=$'\t' read -r file line macro; do
	printf '%s\t%s\t%s\t%s\n' "$(subsys "$file")" "$file" "$macro" \
		"$([ "${file##*.}" = h ] && echo H || echo C)"
done | sort | awk -F'\t' '
{
	ss = $1; f = $2; m = $3; kind = $4
	if (kind == "H") { href[ss]++; hfile[ss","f]=1 }
	else             { cref[ss]++; cfile[ss","f]=1 }
	macros[ss","m] = 1
	seen[ss] = 1
}
END {
	n = 0
	for (s in seen) subs[n++] = s
	# simple insertion sort for stable ordering
	for (i = 1; i < n; i++) {
		key = subs[i]; j = i - 1
		while (j >= 0 && subs[j] > key) { subs[j+1] = subs[j]; j-- }
		subs[j+1] = key
	}
	for (i = 0; i < n; i++) {
		s = subs[i]
		cf = 0; hf = 0
		for (k in cfile) { split(k, a, ","); if (a[1] == s) cf++ }
		for (k in hfile) { split(k, a, ","); if (a[1] == s) hf++ }
		# collect macro names for this subsystem, sorted unique
		delete ms
		mc = 0
		for (k in macros) {
			idx = index(k, ",")
			ks = substr(k, 1, idx-1)
			km = substr(k, idx+1)
			if (ks == s) ms[mc++] = km
		}
		for (p = 1; p < mc; p++) {
			key = ms[p]; q = p - 1
			while (q >= 0 && ms[q] > key) { ms[q+1] = ms[q]; q-- }
			ms[q+1] = key
		}
		ml = ""
		for (p = 0; p < mc; p++) ml = ml (p ? ", " : "") ms[p]
		printf "%-24s %7d %7d %7d %7d   %s\n", \
			s, cref[s]+0, cf, href[s]+0, hf, ml
		TC += cref[s]; TH += href[s]
	}
	printf "%-24s %7s %7s %7s %7s\n", "", "------", "", "------", ""
	printf "%-24s %7d %7s %7d %7s\n", "TOTAL", TC, "", TH, ""
	if (TH > 0) {
		printf "\n%d CONFIG_* reference(s) in headers -- rerun with -H to list them.\n", TH
	}
}'

# Exit status = number of header references (for hooks/CI).
nh=$(printf '%s\n' "$matches" | awk -F'\t' '$1 ~ /\.h$/' | wc -l)
[ "$nh" -gt 255 ] && nh=255
exit "$nh"
