#!/bin/bash

set -e

# Script to run the ktest CI container with Podman

IMAGE_NAME="ktest-ci"
IMAGE_TAG="latest"
FULL_IMAGE_NAME="${IMAGE_NAME}:${IMAGE_TAG}"
CONTAINER_NAME="ktest-ci"
WORKER_CONTAINER_NAME="ktest-worker-$(echo "$WORKER_HOSTNAME" | tr '.' '-')"
POD_NAME="ktest-ci-pod"

# Configuration
HOST_PORT="${HOST_PORT:-8080}"
JOBSERVER="127.0.0.1"
JOBSERVER_HOME="/home/ktest/"
WORKER_HOSTNAME="${WORKER_HOSTNAME:-$(hostname)-worker}"

echo "Starting ktest CI container..."
echo "Image: ${FULL_IMAGE_NAME}"
echo "Container: ${CONTAINER_NAME}"
echo "Host port: ${HOST_PORT}"

# Stop and remove existing pod if running
podman pod rm -f "${POD_NAME}" 2>/dev/null || true

# Create pod with port forwarding
podman pod create \
    --name "${POD_NAME}" \
    --publish "${HOST_PORT}:80"

# Run the container in the pod
podman run \
    --name "${CONTAINER_NAME}" \
    --rm \
    --interactive \
    --tty \
    --detach \
    --pod "${POD_NAME}" \
    --device "/dev/kvm:/dev/kvm" \
    "${FULL_IMAGE_NAME}" \
    /usr/local/bin/start-services.sh

# Run the worker container in the pod
podman run \
    --name "${WORKER_CONTAINER_NAME}" \
    --workdir /home/ktest/linux/ \
    --user ktest \
    --rm \
    --interactive \
    --tty \
    --pod "${POD_NAME}" \
    --device "/dev/kvm:/dev/kvm" \
    --env "WORKER_HOSTNAME=${WORKER_HOSTNAME}" \
    --env "JOBSERVER_HOME=${JOBSERVER_HOME}" \
    "${FULL_IMAGE_NAME}" \
    /usr/local/bin/worker.sh $VERBOSE_ARG $RUN_ONCE_ARG "$JOBSERVER"

echo "Container started successfully!"
echo "Access the CI interface at: http://localhost:${HOST_PORT}"
echo "Health check: http://localhost:${HOST_PORT}/health"
