// Symbols declared in a shared header but used from only one directory
// are reported. A function's out-of-header definition anchors it to the
// defining directory like a use does. Headers already local to their only
// user and uapi/ headers are ignored.
//
// RUN: rm -rf %t && split-file %s %t/lustre-release
// RUN: %gen_db %t/lustre-release lustre/a.c ldlm/b.c
// RUN: %xunused -lint global-headers %t/lustre-release/compile_commands.json 2>&1 \
// RUN:   | FileCheck %s --implicit-check-not=good_ --implicit-check-not=GOOD_

//--- include/shared.h
#ifndef SHARED_H
#define SHARED_H

#define BAD_ONLY_MACRO 1
#define GOOD_BOTH_MACRO 2

static inline int bad_only_inline(void)
{
	return 1;
}

static inline int good_both_inline(void)
{
	return 2;
}

int bad_anchored_fn(int v);
int good_split_fn(int v);

#endif

//--- include/uapi/u.h
#ifndef U_H
#define U_H
#define GOOD_UAPI_MACRO 3
#endif

//--- lustre/local.h
#ifndef LOCAL_H
#define LOCAL_H
static inline int good_already_local(void)
{
	return 4;
}
#endif

//--- lustre/a.c
#include "../include/shared.h"
#include "../include/uapi/u.h"
#include "local.h"

int bad_anchored_fn(int v)
{
	return v + BAD_ONLY_MACRO;
}

int good_split_fn(int v)
{
	return v + GOOD_BOTH_MACRO;
}

int a_entry(void)
{
	return bad_anchored_fn(bad_only_inline() + good_already_local() +
			       GOOD_UAPI_MACRO) + good_both_inline();
}

//--- ldlm/b.c
#include "../include/shared.h"

int b_entry(void)
{
	return good_split_fn(good_both_inline()) + GOOD_BOTH_MACRO;
}

// CHECK-DAG: bad_only_inline include/shared.h:7 FUNCTION only-used-by lustre
// CHECK-DAG: bad_anchored_fn include/shared.h:17 FUNCTION only-used-by lustre
// CHECK-DAG: BAD_ONLY_MACRO include/shared.h:4 MACRO only-used-by lustre
