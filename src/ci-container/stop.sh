#!/bin/bash

set -e

POD_NAME="ktest-ci-pod"

# Stop and remove existing pod if running
podman pod rm -f "${POD_NAME}"
