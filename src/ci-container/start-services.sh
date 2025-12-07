#!/bin/bash

set -e

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# Create required directories
log "Setting up directories..."
mkdir -p /var/log/nginx
mkdir -p /run/nginx
mkdir -p /workspace/ktest-out

if [ ! -f "/etc/ktest-ci.toml" ]; then
    log "No default ktest configuration..."
    exit 1
fi

# Ensure correct permissions
chown -R ktest:ktest /workspace/ktest-out
chown www-data:www-data /var/www/cgi-bin/cgi

# Test nginx configuration
log "Testing nginx configuration..."
nginx -t

# Start fcgiwrap
log "Starting fcgiwrap..."
fcgiwrap -f -s unix:/var/run/fcgiwrap.socket &
FCGIWRAP_PID=$!

# Wait for fcgiwrap socket to be available
while [ ! -S /var/run/fcgiwrap.socket ]; do
    sleep 0.1
done

# Set correct permissions for fcgiwrap socket
chown www-data:www-data /var/run/fcgiwrap.socket

# Start SSH daemon
log "Starting SSH daemon..."
/usr/sbin/sshd -D &
SSHD_PID=$!

# Start nginx
log "Starting nginx..."
nginx -g "daemon off;" &
NGINX_PID=$!

# Start gen-job-list background service as user ktest
log "Starting gen-job-list background service..."
su ktest -c 'while true; do gen-job-list; sleep 10; done' &
GEN_JOB_LIST_PID=$!

# Function to handle shutdown
cleanup() {
    log "Shutting down services..."
    kill $NGINX_PID 2>/dev/null || true
    kill $FCGIWRAP_PID 2>/dev/null || true
    kill $SSHD_PID 2>/dev/null || true
    kill $GEN_JOB_LIST_PID 2>/dev/null || true
    wait
    log "Services stopped"
}

# Trap signals for graceful shutdown
trap cleanup SIGTERM SIGINT

log "Services started successfully"
log "Nginx PID: $NGINX_PID"
log "FCGIWrap PID: $FCGIWRAP_PID"
log "SSH PID: $SSHD_PID"
log "Gen-job-list PID: $GEN_JOB_LIST_PID"
log "CI service available at http://localhost:80"
log "SSH service available at localhost:22"

# Wait for processes
wait
