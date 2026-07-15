// Uses are counted across translation units: a function defined in one TU
// and only called from another is not reported.
//
// RUN: rm -rf %t && split-file %s %t/lustre-release
// RUN: %gen_db %t/lustre-release lustre/a.c lustre/b.c
// RUN: %xunused -lint unused-functions %t/lustre-release/compile_commands.json 2>&1 \
// RUN:   | FileCheck %s --implicit-check-not=good_

//--- lustre/a.c
void bad_orphan(void)
{
}

void good_shared(void)
{
}

//--- lustre/b.c
void good_shared(void);

int main(void)
{
	good_shared();
	return 0;
}

// CHECK: bad_orphan lustre/a.c BODY
