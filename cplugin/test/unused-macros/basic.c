// Macros defined but never expanded or tested are reported; expansion,
// #ifdef-style tests and #undef all count as uses, and header guards are
// ignored.
//
// RUN: rm -rf %t && split-file %s %t/lustre-release
// RUN: %gen_db %t/lustre-release lustre/test.c
// RUN: %xunused -lint unused-macros %t/lustre-release/compile_commands.json 2>&1 \
// RUN:   | FileCheck %s --implicit-check-not=GOOD_

//--- lustre/hdr.h
#ifndef GOOD_GUARD_H
#define GOOD_GUARD_H
#define BAD_IN_HEADER 1
#define GOOD_FROM_HEADER 2
#endif

//--- lustre/test.c
#include "hdr.h"

#define BAD_UNUSED 1
#define GOOD_EXPANDED 2
#define GOOD_TESTED 3
#define GOOD_UNDEFED 4

int use_site(void)
{
	int v = GOOD_EXPANDED + GOOD_FROM_HEADER;

#ifdef GOOD_TESTED
	v++;
#endif
	return v;
}

#undef GOOD_UNDEFED

// CHECK-DAG: BAD_UNUSED lustre/test.c:3 MACRO BODY
// CHECK-DAG: BAD_IN_HEADER lustre/hdr.h:3 MACRO HEADER
