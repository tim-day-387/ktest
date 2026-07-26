#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Bash completion for pk. Source this file from your shell rc:
#
#     source /path/to/ktest/tools/pk-completion.bash
#
# Completes subcommands, per-subcommand options, and job names
# (basenames of *.json and *.group files under jobs/, matching
# the recursive lookup in podman_ktest/jobs.py).

_pk_ktest_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

_pk_job_names() {
    find "$_pk_ktest_dir/jobs" \( -name '*.json' -o -name '*.group' \) \
	 -type f 2>/dev/null |
	sed -e 's|.*/||' -e 's|\.json$||' -e 's|\.group$||'
}

_pk() {
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    COMPREPLY=()

    # Options that take a value
    case "$prev" in
	--podman-socket|--git-hash|--change-id|--subject|--plugin-args|\
	--github-token|--ci-container-socket)
	    return 0
	    ;;
	--shared-filesystem|--output)
	    COMPREPLY=($(compgen -d -- "$cur"))
	    return 0
	    ;;
	--gerrit-auth)
	    COMPREPLY=($(compgen -f -- "$cur"))
	    return 0
	    ;;
	--hosting)
	    COMPREPLY=($(compgen -W "nginx github-pages" -- "$cur"))
	    return 0
	    ;;
    esac

    # Find the subcommand: first non-option word, skipping values
    # of global options
    local i w cmd=""
    for ((i = 1; i < COMP_CWORD; i++)); do
	w="${COMP_WORDS[i]}"
	case "$w" in
	    --podman-socket|--shared-filesystem)
		((i++))
		;;
	    -*)
		;;
	    *)
		cmd="$w"
		break
		;;
	esac
    done

    if [[ -z "$cmd" ]]; then
	if [[ "$cur" == -* ]]; then
	    COMPREPLY=($(compgen -W \
		"--podman-socket --shared-filesystem --tarball-input" \
		-- "$cur"))
	else
	    COMPREPLY=($(compgen -W \
		"info setup build stop run job deploy" -- "$cur"))
	fi
	return 0
    fi

    case "$cmd" in
	build)
	    COMPREPLY=($(compgen -W "--ci-only --local-only --all" -- "$cur"))
	    ;;
	stop)
	    COMPREPLY=($(compgen -W "--all" -- "$cur"))
	    ;;
	job)
	    if [[ "$cur" == -* ]]; then
		COMPREPLY=($(compgen -W \
		    "--stdout --output --git-hash --change-id --subject \
		     --no-cleanup --plugin-args --custom-llvm" -- "$cur"))
	    else
		COMPREPLY=($(compgen -W "$(_pk_job_names)" -- "$cur"))
	    fi
	    ;;
	deploy)
	    COMPREPLY=($(compgen -W \
		"--hosting --gerrit-auth --github-token \
		 --ci-container-socket" -- "$cur"))
	    ;;
    esac

    return 0
}

complete -F _pk pk ./pk
