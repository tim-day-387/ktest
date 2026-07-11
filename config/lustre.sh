#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0

#
# Global Lustre/LNet kernel config requirements.
#
# These require-lustre-*-kernel-config helpers live here (top-level config/)
# so any test can pull in the Lustre kernel config, not just the tests under
# tests/fs/lustre/. Source this after test-libs.sh (for require-kernel-config)
# and set FSTYPE to select the OSD; it defaults to a non-ZFS build.
#

function require-lustre-base-kernel-config()
{
    # Minimal config required for Lustre to build
    #
    # TRANSPARENT_HUGEPAGE is "depends on !PREEMPT_RT" in the kernel, so it
    # can never be enabled on an RT build -- olddefconfig would drop it and
    # the config check would then fail. Only require it when we're not
    # building a PREEMPT_RT kernel.
    if [[ ! " ${ktest_kernel_config_require[*]} " = *" PREEMPT_RT "* ]]; then
	require-kernel-config TRANSPARENT_HUGEPAGE
    fi
    require-kernel-config QUOTA
    require-kernel-config KEYS
    require-kernel-config NETWORK_FILESYSTEMS
    require-kernel-config MULTIUSER
    require-kernel-config NFS_FS
    require-kernel-config BITREVERSE
    require-kernel-config CRYPTO
    require-kernel-config CRYPTO_DEFLATE
    require-kernel-config ZLIB_DEFLATE

    # More tracing support!
    require-kernel-config BPF_SYSCALL
    require-kernel-config DEBUG_INFO_BTF
    # require-kernel-config DEBUG_INFO_BTF_MODULES
    require-kernel-config BPF_JIT
    require-kernel-config FUNCTION_GRAPH_RETVAL

    # Expose kernel config via /proc/config.gz
    require-kernel-config IKCONFIG
    require-kernel-config IKCONFIG_PROC

    # Enable zswap by default
    require-kernel-config ZSWAP
    require-kernel-config ZSWAP_DEFAULT_ON

    # Profiling
    # TODO: Fix me!
    require-kernel-config MEM_ALLOC_PROFILING
    require-kernel-config ERRNO_UNWIND
}

function require-lustre-modules-kernel-config()
{
    require-kernel-config MODULES,MODULE_UNLOAD
}

function require-lustre-kernel-config()
{
    require-lustre-base-kernel-config
    require-lustre-modules-kernel-config

    # ZFS uses a ramdisk for backing storage
    if [[ "${FSTYPE:-}" =~ "zfs" ]]; then
	require-kernel-config BLK_DEV_RAM=y
	require-kernel-config BLK_DEV_RAM_SIZE=524288
    fi

    # On the native platform Lustre is built in-tree, so pull in the in-kernel
    # modules (LNET/Lustre/OSD as =m) - they land in /lib/modules for
    # load_lustre_modules to modprobe. On mainline Lustre comes from an
    # out-of-tree lustre-release build, so this is skipped.
    if [[ "${ktest_lustre_intree:-0}" == "1" ]]; then
	require-lustre-inkernel-kernel-config
    fi
}

function require-lustre-builtin-kernel-config()
{
    # Built-in Lustre: LNET/Lustre/ZFS compiled into the kernel.
    require-kernel-config LIBCFS=y
    require-kernel-config LNET=y
    require-kernel-config LUSTRE_FS=y
    require-kernel-config LUSTRE_FS_SERVER=y

    if [[ "${FSTYPE:-}" =~ "zfs" ]]; then
	require-kernel-config ZFS=y
	require-kernel-config LUSTRE_FS_ZFS=y
    else
	require-kernel-config LUSTRE_FS_WBCFS=y
    fi
}

function require-lustre-inkernel-kernel-config()
{
    # In-kernel Lustre: modules built from the kernel tree, with
    # server support and a ZFS OSD.
    require-kernel-config LIBCFS=m
    require-kernel-config LNET=m
    require-kernel-config LUSTRE_FS=m
    require-kernel-config LUSTRE_FS_SERVER=m

    if [[ "${FSTYPE:-}" =~ "zfs" ]]; then
	require-kernel-config ZFS=m
	require-kernel-config LUSTRE_FS_ZFS=m
    else
	require-kernel-config LUSTRE_FS_WBCFS=m
    fi
}

function require-lustre-ebpf-config()
{
    # More tracing support!
    require-kernel-config BPF_SYSCALL=y
    require-kernel-config DEBUG_INFO_BTF=y
    require-kernel-config DEBUG_INFO_BTF_MODULES=y
    require-kernel-config BPF_JIT=y
}

function require-lustre-debug-kernel-config()
{
    # Basic debugging stuff
    require-kernel-config KASAN
    require-kernel-config KASAN_VMALLOC

    # ZFS doesn't support some options
    if [[ "${FSTYPE:-}" =~ "zfs" ]]; then
	return
    fi

    # Extra debug (probably expensive)
    require-kernel-config DEBUG_INFO
    require-kernel-config DEBUG_FS
    require-kernel-config DEBUG_KERNEL
    require-kernel-config DEBUG_MEMORY_INIT
    require-kernel-config DEBUG_RT_MUTEXES
    require-kernel-config DEBUG_SPINLOCK
    require-kernel-config DEBUG_WW_MUTEX_SLOWPATH
    require-kernel-config DEBUG_IRQFLAGS

    # DEBUG_MUTEXES/DEBUG_RWSEMS are "depends on !PREEMPT_RT" -- on an RT
    # build the mutex/rwsem implementations are replaced by rtmutexes, so
    # these debug options can't be enabled. Skip them for PREEMPT_RT.
    if [[ ! " ${ktest_kernel_config_require[*]} " = *" PREEMPT_RT "* ]]; then
	require-kernel-config DEBUG_MUTEXES
	require-kernel-config DEBUG_RWSEMS
    fi
    require-kernel-config DEBUG_BUGVERBOSE
    require-kernel-config DEBUG_PI_LIST
}

#
# Baseline kernel config shared by every LGTM Kconfig-coverage run
# (tests/fs/lustre/config/*.ktest): the in-kernel Lustre baseline plus debug
# options. Each config/*.ktest layers its single option on top with
# require-kernel-config.
#
function require-lgtm-kernel-config()
{
    require-lustre-kernel-config
    require-lustre-debug-kernel-config
}

function require-lustre-efa-kernel-config()
{
    require-kernel-config NETDEVICES
    require-kernel-config PCI_MSI
    require-kernel-config NET_VENDOR_AMAZON
    require-kernel-config ETHERNET
    require-kernel-config PCI
    require-kernel-config AMAZON_DRIVER_UPDATES
    require-kernel-config AMAZON_ENA_ETHERNET
    require-kernel-config INFINIBAND
    require-kernel-config INFINIBAND_USER_ACCESS
    require-kernel-config AMAZON_EFA_INFINIBAND
}
