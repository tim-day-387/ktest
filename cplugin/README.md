# ccplugin

A clang-based tool that runs source-level lints across all source files in
the whole project, in parallel. Derived from
[xunused](https://github.com/mgehre/xunused), modified for Lustre.

## Lints

Each lint lives in its own `.cpp` file:

| Lint | File | What it finds |
|------|------|---------------|
| `unused-functions` | `unused_functions.cpp` | Functions and methods with a definition but no use. Templates, virtual functions, constructors and `static` linkage are all taken into account. |
| `unused-macros` | `unused_macros.cpp` | Macros that are defined but never expanded or tested (`#ifdef` etc.). Header guards are ignored. |
| `global-headers` | `global_headers.cpp` | Functions and macros declared in a shared header but only used from a single directory; they belong in a header local to that directory (e.g. a symbol in `lnet/include/` only used by `lnet/lnet/`). UAPI headers are ignored. |
| `feature-macros` | `feature_macros.cpp` | Preprocessor feature checks (`#ifdef` etc.) that test something other than a kernel-style `CONFIG_*` macro: `HAVE_*` names, macros defined by the autoconf-generated `config.h`, or configure-time features that are never defined. Macros defined in real source files or on the command line (header guards, `MODULE`, `__KERNEL__`) are allowed. |

All lints run by default. Select lints on the command line:

```
xunused -list-lints                         # list available lints
xunused -lint unused-functions ...          # run only the named lints
xunused -no-lint unused-macros ...          # run all but the named lints
```

Both `-lint` and `-no-lint` are repeatable and accept comma-separated lists.

The `global-headers` lint can additionally print a per-header usage view
with `-header-usage`: for every Lustre header, the number of declarations
it holds (function prototypes, static inlines and macros), what percentage
of them is used by multiple directories, and a breakdown of the
declarations used by only a single directory, e.g.

```
HEADER lnet/include/lnet/lib-lnet.h declarations=142
  multiple-dirs 64% (92/142)
    lnet/klnds/socklnd
    lnet/lnet
  only lnet/lnet 22% (32/142)
  only lnet/klnds/socklnd 7% (10/142)
  unused 5% (8/142)
```

The `multiple-dirs` line lists the directories sharing those declarations
and is omitted when no declaration is used by more than one directory.

## Adding a lint

Implement a `Lint` subclass (cross-TU state plus `report()`) and a
`TUHandler` subclass (per-TU AST matchers and/or preprocessor callbacks) in
a new `.cpp` file, then register the factory in `lint.h`/`lint.cpp` and add
the file to `CMakeLists.txt`. See `lint.h` for the interface.

## Testing

The `test/` directory holds an LLVM-style [lit](https://llvm.org/docs/CommandGuide/lit.html)
test suite: each test builds a tiny fake `lustre-release` tree with
`split-file`, generates a compilation database for it, runs the built
`xunused` on it and checks the findings with `FileCheck`. Run it with

```
make check
```

`lit` is taken from `PATH`, from the copy the `llvm-NN-tools` package
ships under `/usr/lib/llvm-*/build/utils/lit`, or passed explicitly, e.g.
`make check LIT="python3 ~/src/llvm-project/llvm/utils/lit/lit.py"`.
`FileCheck`, `split-file` and `not` are found on `PATH` or in the newest
`/usr/lib/llvm-*/bin`. Set `XUNUSED=/path/to/xunused` to test a binary
other than `build/xunused`.

Test conventions: symbols named `bad_*`/`BAD_*` must appear in the
output, `good_*`/`GOOD_*` must not (enforced with
`--implicit-check-not`), and findings use `CHECK-DAG` since TUs are
processed in parallel.

## Building and Installation

First download or build the necessary versions of LLVM and Clang with
development headers. On Debian and Ubuntu, this can easily be done via
[http://apt.llvm.org](http://apt.llvm.org) and `apt install llvm-18-dev libclang-18-dev`.
Then build via

```
make
```

## Run it

To run the tool, provide a [compilation database](https://clang.llvm.org/docs/JSONCompilationDatabase.html).
By default, it will analyze all files that are mentioned in it.

```
./build/xunused /path/to/your/project/compile_commands.json
```

You can specify the option `-filter` together with a regular expression.
Only files whose path matches the regular expression will be analyzed. You
might want to exclude your test's source code to find functions that are
only used by tests but not any other code.

If `xunused` complains about missing include files such as `stddef.h`, try
adding `-extra-arg=-I/usr/include/clang/17/include` (or similar) to the
arguments.
