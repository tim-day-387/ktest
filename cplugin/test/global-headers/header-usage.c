// -header-usage prints, per header, the share of declarations used by
// multiple directories (with the directory list) and a breakdown of the
// single-directory declarations, after the regular findings.
//
// RUN: rm -rf %t && split-file %s %t/lustre-release
// RUN: %gen_db %t/lustre-release lustre/a.c ldlm/b.c
// RUN: %xunused -lint global-headers -header-usage %t/lustre-release/compile_commands.json 2>&1 \
// RUN:   | FileCheck %s

//--- include/shared.h
#ifndef SHARED_H
#define SHARED_H
static inline int multi_fn(void)
{
	return 1;
}

static inline int single_fn(void)
{
	return 2;
}
#endif

//--- lustre/a.c
#include "../include/shared.h"

int a_entry(void)
{
	return multi_fn() + single_fn();
}

//--- ldlm/b.c
#include "../include/shared.h"

int b_entry(void)
{
	return multi_fn();
}

// CHECK: single_fn include/shared.h:8 FUNCTION only-used-by lustre
// CHECK: HEADER include/shared.h declarations=2
// CHECK-NEXT: multiple-dirs 50% (1/2)
// CHECK-NEXT: ldlm
// CHECK-NEXT: lustre
// CHECK-NEXT: only lustre 50% (1/2)
