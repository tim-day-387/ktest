LUSTRE TESTING TOOLS:
=====================

This subdirectory contains tests for the Lustre filesystem. Each test will
build a Lustre-compatible kernel and run regression tests using the Lustre
test framework. Currently, these tests only support a co-located client
and server (i.e. all Lustre services running on the same kernel).

GETTING STARTED:
================

Follow the setup guide in the top-level README. Then, build a test kernel
that will support Lustre:
```
build-test-kernel run -k $KERNEL_PATH -o $KERNEL_PATH/ktest-out/ tests/fs/lustre/boot.ktest
```

Next, build Lustre against the kernel you are testing with:
```
./autogen.sh
./configure --disable-ldiskfs \
	    --disable-shared \
	    --with-o2ib=no \
	    --disable-gss \
	    --with-linux=$KERNEL_PATH \
	    --with-linux-obj=$KERNEL_PATH/ktest-out/kernel_build.x86_64/
make -j$(nproc)
```
The configuration can be customized. However, disabling shared libraries
and specifying the kernel path are required.

The Lustre modules and utils are bundled into the initramfs (under
/ktools/lustre-release) by `qlkbuild run`, which re-runs mk-initramfs
and ukify with the freshly built trees.

If you require openZFS support, build it beforehand (using similar
instructions); its tree is bundled the same way at /ktools/zfs.

Now, run one of the Lustre tests:
```
build-test-kernel run -k $KERNEL_PATH -o $KERNEL_PATH/ktest-out/ tests/fs/lustre/llmount.ktest
```

Run interactively will keep the filesystem mounted after the
test completes:
```
build-test-kernel run -I -k $KERNEL_PATH -o $KERNEL_PATH/ktest-out/ tests/fs/lustre/llmount.ktest
```
