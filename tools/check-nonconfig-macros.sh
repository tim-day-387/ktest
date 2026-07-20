#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# check-nonconfig-macros.sh
#
# Break down the macros that kernel code tests in preprocessor
# conditionals but that are NOT CONFIG_* macros.
#
# The convention is that build/kernel configuration should be gated on
# CONFIG_* macros.  Anything else tested in a #if / #ifdef -- a HAVE_*
# autoconf probe, a WITH_*/ENABLE_* switch, LINUX_VERSION_CODE, a bare
# name -- is "non-compliant" from that point of view.  Many HAVE_* uses
# are legitimate kernel-API detection, but this report surfaces every
# non-CONFIG_ gate so the ones that really are configuration (e.g.
# HAVE_SERVER_SUPPORT -> CONFIG_LUSTRE_FS_SERVER) can be found and
# migrated.
#
# By default output is one row per macro, ranked by how often it is
# tested, split into references from .c files and from headers (.h).
# With -s the same references are instead grouped by subdirectory.
#
# Usage:
#   contrib/scripts/check-nonconfig-macros.sh [-s] [-l] [-p PREFIX] [PATH ...]
#
#   -s         group by subdirectory instead of by macro name
#   -l         list the detail under each row (sites, or macro names
#              for -s)
#   -p PREFIX  only report macros whose name starts with PREFIX
#              (e.g. -p HAVE_ , -p WITH_)
#   PATH       trees to scan (default: lustre lnet libcfs ldiskfs)
#
# Exit status is the number of distinct non-CONFIG_ macros found,
# capped at 255.

set -u

# Preprocessor conditional lines only.
COND='^[[:space:]]*#[[:space:]]*(if|ifdef|ifndef|elif)\b'

# Tokens that are operators/keywords in a conditional, not macros.
NOTMACRO='^(if|ifdef|ifndef|elif|defined|IS_ENABLED|IS_BUILTIN|IS_MODULE)$'

list=0
prefix=""
bysub=0

while getopts "slp:h" opt; do
	case "$opt" in
	s) bysub=1 ;;
	l) list=1 ;;
	p) prefix="$OPTARG" ;;
	h)
		sed -n '2,34p' "$0" | sed 's/^#\?//'
		exit 0
		;;
	*)
		echo "usage: $0 [-s] [-l] [-p PREFIX] [PATH ...]" >&2
		exit 2
		;;
	esac
done
shift $((OPTIND - 1))

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

# Every macro reference as: <macro>\t<kind C|H>\t<path>:<line>
#
# For each conditional line we strip C/C++ comments, pull out identifier
# tokens, drop the directive/operator keywords and any CONFIG_* token.
matches=$(grep -rnP --include='*.c' --include='*.h' "$COND" "${present[@]}" 2>/dev/null |
	while IFS=: read -r file line rest; do
		kind=C; [ "${file##*.}" = h ] && kind=H
		# drop /* ... */ and // ... comments, then extract idents
		printf '%s\n' "$rest" |
			sed -E 's:/\*.*\*/::g; s://.*$::' |
			grep -oP '[A-Za-z_][A-Za-z0-9_]*' |
			grep -vE "$NOTMACRO" |
			grep -vP '^CONFIG_' |
			while read -r macro; do
				printf '%s\t%s\t%s:%s\n' "$macro" "$kind" "$file" "$line"
			done
	done)

# Optional name-prefix filter.
if [ -n "$prefix" ]; then
	matches=$(printf '%s\n' "$matches" | awk -F'\t' -v p="$prefix" 'index($1,p)==1')
fi

if [ -z "$matches" ]; then
	echo "No non-CONFIG_ macros found."
	exit 0
fi

if [ "$bysub" -eq 1 ]; then
	# subsystem = first two path components, e.g. lustre/llite.
	printf '%s\n' "$matches" |
		awk -F'\t' '{
			split($3, p, ":"); n = split(p[1], d, "/")
			ss = (n >= 2) ? d[1] "/" d[2] : d[1]
			print ss "\t" $1 "\t" $2
		}' |
		sort |
		awk -F'\t' -v list="$list" '
	{
		s = $1; m = $2; kind = $3
		seen[s] = 1
		tot[s]++
		if (kind == "H") h[s]++; else c[s]++
		if (!((s SUBSEP m) in mk)) { mk[s SUBSEP m] = 1; nmac[s]++ }
		mlist[s] = mlist[s] m "\n"
	}
	END {
		n = 0
		for (s in seen) name[n++] = s
		# rank by total desc, then name asc
		for (i = 1; i < n; i++) {
			k = name[i]; j = i - 1
			while (j >= 0 && ((tot[name[j]] < tot[k]) ||
			       (tot[name[j]] == tot[k] && name[j] > k))) {
				name[j+1] = name[j]; j--
			}
			name[j+1] = k
		}
		printf "%-24s %6s %6s %6s %7s\n",
			"SUBSYSTEM", "TOTAL", "C", "H", "MACROS"
		printf "%-24s %6s %6s %6s %7s\n",
			"------------------------",
			"------", "------", "------", "-------"
		tt = 0; tc = 0; th = 0
		for (i = 0; i < n; i++) {
			s = name[i]
			printf "%-24s %6d %6d %6d %7d\n",
				s, tot[s], c[s]+0, h[s]+0, nmac[s]
			tt += tot[s]; tc += c[s]; th += h[s]
			if (list) {
				# unique macro names for this subsystem, sorted
				ns = split(mlist[s], arr, "\n")
				delete uq; nu = 0
				for (x = 1; x <= ns; x++)
					if (arr[x] != "" && !(arr[x] in uq)) {
						uq[arr[x]] = 1; us[nu++] = arr[x]
					}
				for (a = 1; a < nu; a++) {
					key = us[a]; b = a - 1
					while (b >= 0 && us[b] > key) {
						us[b+1] = us[b]; b--
					}
					us[b+1] = key
				}
				for (a = 0; a < nu; a++)
					printf "        %s\n", us[a]
			}
		}
		printf "%-24s %6s %6s %6s %7s\n",
			"------------------------",
			"------", "------", "------", "-------"
		printf "%-24s %6d %6d %6d\n", "TOTAL", tt, tc, th
	}'

	nd=$(printf '%s\n' "$matches" | cut -f1 | sort -u | grep -c .)
	[ "$nd" -gt 255 ] && nd=255
	exit "$nd"
fi

printf '%s\n' "$matches" |
	sort |
	awk -F'\t' -v list="$list" '
{
	m = $1; kind = $2; site = $3
	seen[m] = 1
	tot[m]++
	if (kind == "H") { h[m]++; hs[m] = hs[m] site "\n" }
	else             { c[m]++; cs[m] = cs[m] site "\n" }
}
END {
	n = 0
	for (m in seen) name[n++] = m
	# rank by total desc, then name asc
	for (i = 1; i < n; i++) {
		k = name[i]; j = i - 1
		while (j >= 0 && ( (tot[name[j]] < tot[k]) ||
		       (tot[name[j]] == tot[k] && name[j] > k) )) {
			name[j+1] = name[j]; j--
		}
		name[j+1] = k
	}

	printf "%-44s %6s %6s %6s\n", "MACRO", "TOTAL", "C", "H"
	printf "%-44s %6s %6s %6s\n",
		"--------------------------------------------",
		"------", "------", "------"
	tt = 0; tc = 0; th = 0
	for (i = 0; i < n; i++) {
		m = name[i]
		printf "%-44s %6d %6d %6d\n", m, tot[m], c[m]+0, h[m]+0
		tt += tot[m]; tc += c[m]; th += h[m]
		if (list) {
			ns = split(cs[m] hs[m], arr, "\n")
			for (s = 1; s <= ns; s++)
				if (arr[s] != "") printf "        %s\n", arr[s]
		}
	}
	printf "%-44s %6s %6s %6s\n",
		"--------------------------------------------",
		"------", "------", "------"
	printf "%-44s %6d %6d %6d\n", "TOTAL (" n " macros)", tt, tc, th
}'

# Exit status = number of distinct non-CONFIG_ macros.
nd=$(printf '%s\n' "$matches" | cut -f1 | sort -u | grep -c .)
[ "$nd" -gt 255 ] && nd=255
exit "$nd"
