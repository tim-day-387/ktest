#!/bin/bash

set -e

# Script to build the ktest CI container with Podman

IMAGE_NAME="ktest-ci"
IMAGE_TAG="latest"
FULL_IMAGE_NAME="${IMAGE_NAME}:${IMAGE_TAG}"

echo "Building ktest CI container..."
echo "Image: ${FULL_IMAGE_NAME}"

# Build the container
podman build \
    --file "${SCRIPT_DIR}/Dockerfile" \
    --tag "${FULL_IMAGE_NAME}" \
    "${PROJECT_ROOT}"

echo "Build completed successfully!"
echo "Image: ${FULL_IMAGE_NAME}"
