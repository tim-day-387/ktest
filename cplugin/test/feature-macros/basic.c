// Feature checks are reported when they test a HAVE_* macro (wherever it
// is defined), a macro defined only by the generated config.h, or a macro
// never defined at all. CONFIG_* names, reserved names and macros defined
// in real source files are allowed.
//
// RUN: rm -rf %t && split-file %s %t/lustre-release
// RUN: %gen_db %t/lustre-release lustre/test.c
// RUN: %xunused -lint feature-macros %t/lustre-release/compile_commands.json 2>&1 \
// RUN:   | FileCheck %s --implicit-check-not=GOOD_ --implicit-check-not=KERNEL

//--- config.h
#define BAD_CONFIG_ONLY 1

//--- lustre/compat.h
#ifndef GOOD_COMPAT_H
#define GOOD_COMPAT_H
#define GOOD_COMPAT_FLAG 1
#define HAVE_BAD_FEATURE 1
#endif

//--- lustre/test.c
#include "../config.h"
#include "compat.h"

#ifdef CONFIG_GOOD_OPTION
int x1;
#endif

#ifdef HAVE_BAD_FEATURE
int x2;
#endif

#ifndef BAD_NEVER_DEFINED
int x3;
#endif

#ifdef BAD_CONFIG_ONLY
int x4;
#endif

#ifdef GOOD_COMPAT_FLAG
int x5;
#endif

#if defined(HAVE_BAD_EXPR)
int x6;
#endif

#ifdef __KERNEL__
int x7;
#endif

// CHECK-DAG: HAVE_BAD_FEATURE lustre/test.c:8 FEATURE BODY
// CHECK-DAG: BAD_NEVER_DEFINED lustre/test.c:12 FEATURE BODY
// CHECK-DAG: BAD_CONFIG_ONLY lustre/test.c:16 FEATURE BODY
// CHECK-DAG: HAVE_BAD_EXPR lustre/test.c:24 FEATURE BODY
