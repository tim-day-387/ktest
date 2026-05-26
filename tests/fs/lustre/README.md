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

The freshly built Lustre kernel modules are bundled into the initramfs by
`qlkbuild run`, which re-runs mk-initramfs and ukify against the build. The
lustre-release and zfs checkouts are built in place on the host and read in the
VM over the /host 9p mount, so the test framework (llmount.sh, auster, etc.)
runs straight out of them.

If you require openZFS support, build it beforehand (using similar
instructions); its tree is reached the same way.

Now, run one of the Lustre tests:
```
build-test-kernel run -k $KERNEL_PATH -o $KERNEL_PATH/ktest-out/ tests/fs/lustre/llmount.ktest
```

Run interactively will keep the filesystem mounted after the
test completes:
```
build-test-kernel run -I -k $KERNEL_PATH -o $KERNEL_PATH/ktest-out/ tests/fs/lustre/llmount.ktest
```
