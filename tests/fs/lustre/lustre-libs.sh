#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2024, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Library for writing Lustre tests in the ktest format.
#
# Author: Timothy Day <timday@amazon.com>
#

. "$(dirname "$(dirname "$(dirname "$(readlink -e "${BASH_SOURCE[0]}")")")")/test-libs.sh"

# The lustre-release and zfs trees are built in place on the host and reached
# in the VM over the /host 9p mount (the build runs in these checkouts, so the
# compiled modules/binaries live there).  The Lustre test framework (llmount.sh,
# auster, etc.) runs straight out of them.  racer.ktest overrides these for its
# own layout.
export workspace_path="/host/home/ktest/git"
export lustre_pkg_path="$workspace_path/lustre-release"
export zfs_pkg_path="$workspace_path/zfs"

# Set Lustre test-framework.sh environment. Prefer the normally-installed
# zfs/zpool (resolved from PATH before the build-tree dirs are prepended
# below); fall back to the in-tree build layout only if not installed.
export ZFS="$(command -v zfs 2>/dev/null || echo "$zfs_pkg_path/cmd/zfs/zfs")"
export ZPOOL="$(command -v zpool 2>/dev/null || echo "$zfs_pkg_path/cmd/zpool/zpool")"

export LUSTRE="$lustre_pkg_path/lustre"
export LCTL="$(command -v lctl 2>/dev/null || echo "$LUSTRE/utils/lctl")"
export LNETCTL="$(command -v lnetctl 2>/dev/null || echo "$LUSTRE/../lnet/utils/lnetctl")"
export LNETDUMP="$(command -v lnetdump 2>/dev/null || echo "$LUSTRE/utils/lnetdump/lnetdump")"
export LUSTRE_RMMOD="$(command -v lustre_rmmod 2>/dev/null || echo "$LUSTRE/scripts/lustre_rmmod")"
export RUNAS_ID="1000"

# Update paths
set +u
export lustre_tests_bin="/usr/lib/lustre/tests"
export PATH="$zfs_pkg_path:$zfs_pkg_path/cmd/zpool:$zfs_pkg_path/cmd/zfs:$PATH:$lustre_tests_bin"
export LD_LIBRARY_PATH="$zfs_pkg_path/lib/libzfs/.libs:$zfs_pkg_path/lib/libzfs_core/.libs:$LD_LIBRARY_PATH"
export LD_LIBRARY_PATH="$zfs_pkg_path/lib/libuutil/.libs:$zfs_pkg_path/lib/libnvpair/.libs:$LD_LIBRARY_PATH"
export LD_LIBRARY_PATH="$zfs_pkg_path/lib/libzpool/.libs:$LD_LIBRARY_PATH"
set -u

# Dump out all of the special Lustre variables
function print_lustre_env() {
    echo "FSTYPE=$FSTYPE"
    echo "FSNAME=$FSNAME"
    echo "MGSNID=$MGSNID"
    echo "TESTSUITE=$TESTSUITE"
    echo "ONLY=$ONLY"
}

# Run a command as if it were part of test-framework.sh
function run_tf() {
	    cat << EOF | bash
. "$LUSTRE/tests/test-framework.sh" > /dev/null
init_test_env > /dev/null
$@
EOF
}

# Run llog_test.ko unit tests
function run_llog() {
    export MGS="$($LCTL dl | awk '/mgs/ { print $4 }')"

    cat << EOF | bash
. "$LUSTRE/tests/test-framework.sh" > /dev/null
init_test_env > /dev/null

# Load module
load_module kunit/llog_test || error "load_module failed"

# Using ignore_errors will allow lctl to cleanup even if the test fails.
$LCTL mark "Attempt llog unit tests"
eval "$LCTL <<-EOF || RC=2
	attach llog_test llt_name llt_uuid
	ignore_errors
	setup $MGS
	--device llt_name cleanup
	--device llt_name detach
EOF"
$LCTL mark "Finish llog units tests"
EOF
}

# Grab special Lustre environment variables
# TODO: There's probably a better way to do this...
set +u
if [[ -n "$FSTYPE" || -n "$FSNAME" || -n "$MGSNID" || -n "$TESTSUITE" || -n "$ONLY" ]]; then
    rm -f /tmp/ktest-lustre.env
    print_lustre_env > /tmp/ktest-lustre.env
else
    # If the filesystem doesn't exist, use defaults
    if [[ -f /host/tmp/ktest-lustre.env ]]; then
	eval $(cat /host/tmp/ktest-lustre.env)
    else
	FSTYPE="wbcfs"
	FSNAME=""
	MGSNID=""
	TESTSUITE=""
	ONLY=""
    fi
fi
set -u

function configure_interface()
{
    local interface="$1"

    echo >> /etc/network/interfaces
    echo "auto $interface" >> /etc/network/interfaces
    echo "iface $interface inet dhcp" >> /etc/network/interfaces

    ip route del default
    ifup "$interface"
    dhclient "$interface"
    ip route show
}

function set_hostname_interface()
{
    local interface="$1"
    local local_ip="$(ip address show $interface | awk -F' ' '$1 == "inet" { print $2 }' | awk -F'/' '{ print $1 }')"
    local host_name="$(hostname)"

    sed -i '/$host_name/d' /etc/hosts
    echo "$local_ip" "$host_name" >> /etc/hosts
}

function require-lustre-base-kernel-config()
{
    # Minimal config required for Lustre to build
    require-kernel-config TRANSPARENT_HUGEPAGE
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
    if [[ "$FSTYPE" =~ "zfs" ]]; then
	require-kernel-config BLK_DEV_RAM=y
	require-kernel-config BLK_DEV_RAM_SIZE=524288
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
    if [[ "$FSTYPE" =~ "zfs" ]]; then
	return
    fi

    # Extra debug (probably expensive)
    require-kernel-config DEBUG_INFO
    require-kernel-config DEBUG_FS
    require-kernel-config DEBUG_KERNEL
    require-kernel-config DEBUG_MEMORY_INIT
    require-kernel-config DEBUG_RT_MUTEXES
    require-kernel-config DEBUG_SPINLOCK
    require-kernel-config DEBUG_MUTEXES
    require-kernel-config DEBUG_WW_MUTEX_SLOWPATH
    require-kernel-config DEBUG_RWSEMS
    require-kernel-config DEBUG_IRQFLAGS
    require-kernel-config DEBUG_BUGVERBOSE
    require-kernel-config DEBUG_PI_LIST
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

function load_lustre_modules()
{
    if [[ "$FSTYPE" =~ "zfs" ]]; then
	modprobe zfs
    fi

    FSTYPE="$FSTYPE" "$lustre_pkg_path/lustre/tests/llmount.sh" --load-modules
}

function setup_lustre_mgs()
{
    if [[ "$FSTYPE" == "zfs" ]]; then
	"$ktest_dir/target/release/lustre-ktest" mgs --osd zfs
    else
	"$ktest_dir/target/release/lustre-ktest" mgs
    fi
}

function cleanup_lustre_mgs()
{
    "$ktest_dir/target/release/lustre-ktest" umount
}

function configure_lnet()
{
    local efa_modpath="$LUSTRE/../lnet/klnds/efalnd/kefalnd.ko"
    local efa_interface="$(ls -1 /sys/class/infiniband | head -n1)"
    local eth_interface="$1"

    # Install EFA if available
    if [[ -f "$efa_modpath" ]]; then
	insmod "$efa_modpath" ipif_name="$eth_interface"
    fi

    # Reset configuration
    "$LNETCTL" set discovery 1
    "$LNETCTL" lnet configure
    "$LNETCTL" net del --net tcp || true

    # Add our TCP interfaces
    "$LNETCTL" net add --net tcp --if "$eth_interface" || true

    # Add our EFA interfaces
    if [[ -f "$efa_modpath" ]]; then
	"$LNETCTL" net add --net efa \
		   --if "$efa_interface" \
		   --peer-credits 128 || true
	"$LNETCTL" udsp add --src efa --priority 0
    fi

    # Dump config
    "$LNETCTL" net show -v
}

function lustre_performance_tuning()
{
    "$LCTL" set_param debug=0
}

function lustre_client_performance_tuning()
{
    lustre_performance_tuning

    "$LCTL" set_param llite.*.checksum_pages=0
    "$LCTL" set_param osc.*OST*.max_rpcs_in_flight=128
}

function __setup_lustrefs()
{
    print_lustre_env
    load_lustre_modules

    if [[ "$FSTYPE" == "zfs" ]]; then
	"$ktest_dir/target/release/lustre-ktest" mount --osd zfs
    else
	"$ktest_dir/target/release/lustre-ktest" mount
    fi

    # TODO: Make this tunable...
    # valgrind --leak-check=full "$LNETDUMP" &
    # "$LNETDUMP" &

    # Disable identity upcall (required for OSD wbcfs; harmless for ZFS)
    "$LCTL" set_param mdt.*.identity_upcall=NONE

    mount -t lustre
}

function setup_lustrefs()
{
    run_quiet_with_status "SETUP LUSTRE" "DONE" __setup_lustrefs
}

function __cleanup_lustrefs()
{
    if [[ "$ktest_interactive" != "true" ]]; then
	"$ktest_dir/target/release/lustre-ktest" umount
    fi
}

function cleanup_lustrefs()
{
    run_quiet_with_status "CLEANUP LUSTRE" "DONE" __cleanup_lustrefs
}

function split_array() {
    local input="$1"
    local input_array=($input)
    local chunk_size=$2
    local target_chunk=$3
    local result=()

    # Calculate the starting and ending indices for the target chunk
    local start_index=$(( (target_chunk - 1) * chunk_size ))
    local end_index=$(( start_index + chunk_size - 1 ))

    # Populate the result array with the specified chunk
    for i in $(seq $start_index $end_index); do
	[[ $i -lt ${#input_array[@]} ]] && result+=("${input_array[i]}")
    done

    # Print the result array
    echo "${result[@]}"
}

function join () {
    local IFS="$1"
    shift
    echo "$*"
}

# Lustre/ZFS will always taint kernel
allow_taint
