#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only

#
# Host side of tcp-client.ktest: wait for the VM to announce its Lustre
# server, run tools/lustre_tcp_client.py against it and leave the exit
# status in $ktest_helper_dir/result.
#
# The LNet acceptor wants a privileged source port, so this needs root
# or CAP_NET_BIND_SERVICE (which pk gives its job containers).
#

set -u

client="$ktest_dir/tools/lustre_tcp_client.py"
dir="$ktest_helper_dir"
out="$dir/copies"
rc=0

function run_client()
{
    echo "== lustre_tcp_client.py $*"
    python3 "$client" --server "$server" --timeout 20 "$@"
}

function check()
{
    if "$@"; then
	echo "== PASS: $*"
    else
	echo "== FAIL: $*"
	rc=1
    fi
}

function copy_and_verify()
{
    local path

    run_client --copy-file tcptest/MANIFEST "$out/MANIFEST" || return 1

    # carry on after a failure, to show everything that is broken
    while read -r _ path; do
	mkdir -p "$(dirname "$out/$path")"
	run_client --copy-file "tcptest/$path" "$out/$path" ||
	    echo "== copy failed: $path"
    done < "$out/MANIFEST"

    (cd "$out" && md5sum -c MANIFEST)
}

# Run the client and look for a pattern in what it prints
function client_prints()
{
    local pattern="$1"
    local output
    shift

    output="$(run_client "$@" 2>&1)" || { echo "$output"; return 1; }
    echo "$output"
    grep -q -- "$pattern" <<< "$output"
}

# DNE (a ZFS backed server only): readdir of a striped directory has to
# list what the kernel client's ls did
function striped_readdir()
{
    local output

    [[ -f "$out/stripedir.ls" ]] || { echo "no DNE part in this run"; return 0; }

    # the listing is all there is on stdout, the log goes to stderr
    output="$(run_client --show-path tcptest/stripedir)" || return 1
    echo "$output"

    # "type fid name" lines, less the comment lines, "." and ".."
    awk '$1 != "#" && $1 != "==" && $3 != "." && $3 != ".." { print $3 }' \
	<<< "$output" | sort > "$out/stripedir.readdir"
    diff -u "$out/stripedir.ls" "$out/stripedir.readdir"
}

# The test has no names a striped directory keeps out of their stripe
# (migration, temporary files), so finding one there is a wrong hash
function names_hash_right()
{
    ! grep "not in the stripe it hashes to" "$dir/client.log"
}

function missing_file_fails()
{
    ! run_client --copy-file tcptest/does-not-exist "$out/missing"
}

function run_checks()
{
    mkdir -p "$out"

    # the mount flow alone, with the fsname discovered from the MGS
    check run_client
    check client_prints "-MDT0000" --list-logs
    check run_client --config-log
    check run_client --nodemap
    check client_prints "\btcptest\b" --show-path /
    check run_client --show-path tcptest/pfl
    check copy_and_verify
    check striped_readdir
    check names_hash_right
    check missing_file_fails
}

until [[ -f "$dir/server" ]]; do
    sleep 1
done
server="$(cat "$dir/server")"

run_checks > "$dir/client.log" 2>&1

echo "$rc" > "$dir/result.tmp"
mv "$dir/result.tmp" "$dir/result"
