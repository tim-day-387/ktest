// Functions defined but never used are reported; called functions, main
// and __attribute__((constructor)) functions are not. Naming convention
// used by all tests in this suite: bad_*/BAD_* must be reported,
// good_*/GOOD_* must not (enforced by --implicit-check-not).
//
// RUN: rm -rf %t && split-file %s %t/lustre-release
// RUN: %gen_db %t/lustre-release lustre/test.c
// RUN: %xunused -lint unused-functions %t/lustre-release/compile_commands.json 2>&1 \
// RUN:   | FileCheck %s --implicit-check-not=good_

//--- lustre/test.c
static void bad_static(void)
{
}

void bad_external(void)
{
}

__attribute__((constructor)) static void good_ctor(void)
{
}

static int good_called(void)
{
	return 42;
}

int main(void)
{
	return good_called();
}

// CHECK-DAG: bad_static lustre/test.c BODY
// CHECK-DAG: bad_external lustre/test.c BODY
