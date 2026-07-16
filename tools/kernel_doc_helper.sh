#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only

#
# Count kernel-doc warnings across the Lustre tree.
#
# Folders which did not have any warnings to begin with:
#
# conf/ contrib/ ec/ include/ kernel_patches/ kunit/ scripts/ tests/
#
# Author: Arshad Hussain <arshad.hussain@aeoncomputing.com>
#

folder_lst0="fid/ fld/ ldlm/ llite/ lfsck/ lmv/ lod/ lov/ mdc/ mdd/ mdt/   \
             mgc/ mgs/ obdclass/ obdecho/ ofd/ osc/ osd-ldiskfs/ osd-zfs/  \
             osp/ ptlrpc/ quota/ target/ utils/"
folder_lst1="lnet/lnet"

KERNEL_DOC=${KERNEL_DOC:-"/home/ktest/git/linux/scripts/kernel-doc"}
LUSTRE_ROOT=${LUSTRE_ROOT:-"/home/ktest/git/lustre-release"}

BIN="$0"
TOTAL=0

show_usage()
{
	echo "Usage:"
	echo "$BIN [file.c]"
	echo "Eg: $BIN ./lustre/llite/file.c # for single file (complete path)"
	echo "Eg: $BIN # for all pre-defined folders"
	echo
	echo "Pre-defined folders:"
	echo "$folder_lst0 $folder_lst1"
	exit 1
}

# Count warnings from both the old perl ("file:line: warning: ...")
# and the new python ("Warning: file:line ...") kernel-doc output.
count_warnings()
{
	$KERNEL_DOC -none "$@" 2>&1 | grep -Eic '(^|: )warning:'
}

check_file()
{
	[[ -f "$1" ]] || show_usage

	warn=$(count_warnings "$1")
	echo "$warn:$1"
	TOTAL=$((TOTAL + warn))
}

check_folders()
{
	for f in $1; do
		warn=$(count_warnings "$2"/$f/*.c)
		echo "$warn:$f"
		TOTAL=$((TOTAL + warn))
	done
}

#
# main
#
(( $# < 2 )) || show_usage

if (( $# == 1 )); then
	check_file "$1"
else
	check_folders "$folder_lst0" "$LUSTRE_ROOT/lustre"
	check_folders "$folder_lst1" "$LUSTRE_ROOT"
fi

echo "$TOTAL : Total"
