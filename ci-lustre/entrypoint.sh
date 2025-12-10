#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
#
# Entrypoint script for CI container
# Starts nginx and the Gerrit CI daemon

set -e

# Determine hosting mode (default: nginx)
HOSTING_MODE="${HOSTING_MODE:-nginx}"

echo "Starting CI container with hosting mode: $HOSTING_MODE"

# Cleanup handler
cleanup() {
    echo "Shutting down services..."
    if [ -n "$NGINX_PID" ] && kill -0 $NGINX_PID 2>/dev/null; then
        kill $NGINX_PID
    fi
    if [ -n "$CI_PID" ] && kill -0 $CI_PID 2>/dev/null; then
        kill $CI_PID
    fi
    exit 0
}

trap cleanup SIGTERM SIGINT

# Fetch Linux and Lustre repositories as ktest user
echo "Fetching repositories..."

su - ktest -c '
# Create git directory
mkdir -p /home/ktest/git

# Clone Linux kernel v6.14
echo "Cloning Linux kernel v6.14..."
git clone --depth 1 --branch v6.14 https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git /home/ktest/git/linux

# Clone Lustre latest master
echo "Cloning Lustre master branch..."
git clone git://git.whamcloud.com/fs/lustre-release.git /home/ktest/git/lustre-release
'

echo "Repository fetch complete"

# Clone GitHub Pages repository if in github-pages mode
if [ "$HOSTING_MODE" = "github-pages" ]; then
    echo "Cloning GitHub Pages repository..."

    # Remove existing directory if it exists
    if [ -d "$OUTPUT_DIR" ]; then
        echo "Removing existing OUTPUT_DIR..."
        rm -rf "$OUTPUT_DIR"
    fi

    # Clone the repository as ktest user
    su - ktest -c "
        # Configure git if credentials are provided
        if [ -n \"\$GITHUB_TOKEN\" ]; then
            git config --global credential.helper store
            echo \"https://\$GITHUB_TOKEN@github.com\" > ~/.git-credentials
        fi

        # Clone the repository
        git clone https://tim-day-387:$GITHUB_TOKEN@github.com/tim-day-387/upstream-patch-review.git $OUTPUT_DIR

        # Configure git user for commits
        cd $OUTPUT_DIR
        git config user.name \"John Ktest\"
        git config user.email \"john@ktest.com\"

	# Clean old reviews
	rm -f *.html *.json *.log *.css *.js
    "

    echo "GitHub Pages repository cloned successfully"
fi

# Start nginx if in nginx mode
if [ "$HOSTING_MODE" = "nginx" ]; then
    echo "Starting nginx..."
    nginx -g "daemon off;" &
    NGINX_PID=$!
    echo "Nginx started with PID $NGINX_PID"
fi

# Wait a moment for nginx to start
sleep 2

# Fix podman socket permissions for ktest user
if [ -S "/run/podman/podman.sock" ]; then
    echo "Setting permissions on /run/podman/podman.sock..."
    chmod 666 /run/podman/podman.sock
else
    echo "Warning: /run/podman/podman.sock not found"
fi

# Function to start the CI daemon
start_ci_daemon() {
    echo "Starting Gerrit CI daemon..."
    su - ktest -c "cd /home/ktest/ktest/ci-lustre/ && GERRIT_USERNAME='$GERRIT_USERNAME' GERRIT_PASSWORD='$GERRIT_PASSWORD' OUTPUT_DIR='$OUTPUT_DIR' PODMAN_SOCKET='$PODMAN_SOCKET' HOSTING_MODE='$HOSTING_MODE' python3 gerrit_build-and-test-new.py" &
    CI_PID=$!
    echo "CI daemon started with PID $CI_PID"
}

# Start the CI daemon as a background service
start_ci_daemon

# Monitor and restart CI daemon if it dies
while true; do
    if ! kill -0 $CI_PID 2>/dev/null; then
        echo "CI daemon died, restarting in 5 seconds..."
        sleep 5
        start_ci_daemon
    fi
    sleep 10
done
