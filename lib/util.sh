
. "$ktest_dir/lib/common.sh"

check_root_image_exists()
{
    local candidates=()

    if [[ -n $ktest_root_image ]]; then
	return
    fi

    if [[ $ktest_lustre_root == 1 ]]; then
	candidates+=("$HOME/.ktest/lustre_root.$DEBIAN_ARCH")
	candidates+=("/var/lib/ktest/lustre_root.$DEBIAN_ARCH")
    else
	candidates+=("$HOME/.ktest/root.$DEBIAN_ARCH")
	candidates+=("/var/lib/ktest/root.$DEBIAN_ARCH")
    fi

    for c in "${candidates[@]}"; do
	if [[ -f $c ]]; then
	    ktest_root_image=$c
	    return
	fi
    done

    echo "Root image not found in any of:"
    for c in "${candidates[@]}"; do
	echo "    $c"
    done
    echo "Use $ktest_dir/root_image create"
    exit 1
}
