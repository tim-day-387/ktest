#!/bin/bash

set -e

# CI Worker script for ktest
# This script runs inside the CI container and uses ci/test-git-branch.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KTEST_DIR="/workspace"

# Default configuration
export JOBSERVER_OUTPUT_DIR="/workspace/ktest-out/"
JOBSERVER="${JOBSERVER:-}"
WORKER_HOSTNAME="${WORKER_HOSTNAME:-$(hostname)}"
VERBOSE="${VERBOSE:-false}"

# Logging function
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

usage() {
    echo "CI Worker for ktest"
    echo "Usage: $0 [options] JOBSERVER"
    echo "Arguments:"
    echo "  JOBSERVER   Jobserver hostname/address"
    echo "Options:"
    echo "  -v          Verbose output"
    echo "  -o          Run one test only"
    echo "  -h          Show this help"
    echo ""
    echo "Environment variables:"
    echo "  WORKER_HOSTNAME  Worker hostname identifier (default: $(hostname))"
}

# Parse command line arguments
RUN_ONCE_ARG=""
VERBOSE_ARG=""
while getopts "voh" opt; do
    case $opt in
        v) VERBOSE=true; VERBOSE_ARG="-v" ;;
        o) RUN_ONCE_ARG="-o" ;;
        h) usage; exit 0 ;;
        *) usage; exit 1 ;;
    esac
done
shift $((OPTIND-1))

# Get jobserver from argument
if [ $# -eq 0 ]; then
    if [ -z "$JOBSERVER" ]; then
        echo "ERROR: JOBSERVER must be specified as argument or environment variable" >&2
        usage
        exit 1
    fi
else
    JOBSERVER="$1"
fi

# Validate required environment
if [ ! -d "$KTEST_DIR" ]; then
    log "ERROR: KTEST_DIR ($KTEST_DIR) not found"
    exit 1
fi

if [ ! -f "$KTEST_DIR/ci/test-git-branch.sh" ]; then
    log "ERROR: ci/test-git-branch.sh not found in $KTEST_DIR"
    exit 1
fi

# Signal handlers for graceful shutdown
cleanup() {
    log "Received shutdown signal, cleaning up..."
    # Kill any background processes
    jobs -p | xargs -r kill 2>/dev/null || true
    log "Worker shutdown complete"
    exit 0
}

trap cleanup SIGTERM SIGINT

# Main execution
main() {
    log "Starting ktest CI worker"
    log "Configuration:"
    log "  Jobserver: $JOBSERVER"
    log "  Worker hostname: $WORKER_HOSTNAME"
    log "  Ktest dir: $KTEST_DIR"
    log "  Verbose: $VERBOSE"
    
    # Export hostname for test-git-branch.sh
    export HOSTNAME="$WORKER_HOSTNAME"
    
    # Run the existing test-git-branch.sh script
    log "Starting ci/test-git-branch.sh..."
    exec "$KTEST_DIR/ci/test-git-branch.sh" $VERBOSE_ARG $RUN_ONCE_ARG "$JOBSERVER"
}

# Only run main if this script is executed directly
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
