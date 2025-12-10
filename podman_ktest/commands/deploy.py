# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import json
import os
from pathlib import Path

import podman

from ..config import DEPLOY_CONTAINER_NAME, DEPLOY_NGINX_PORT
from ..utils import get_podman_socket


def cmd_deploy(args, podman_socket=None):
    """Deploy the Lustre CI container."""
    socket_url = get_podman_socket(podman_socket)

    # Determine the socket path to mount from the host
    # If --ci-container-socket is provided, use it as the source
    # Otherwise, use the same path as the host socket
    if args.ci_container_socket:
        host_socket_to_mount = args.ci_container_socket.replace("unix://", "")
    else:
        host_socket_to_mount = socket_url.replace("unix://", "")

    # Check if gerrit auth file exists and parse it
    gerrit_auth_path = Path(args.gerrit_auth).resolve()
    if not gerrit_auth_path.exists():
        print(f"Error: Gerrit auth file not found: {gerrit_auth_path}")
        return 1

    # Parse gerrit auth file to extract credentials
    try:
        with open(gerrit_auth_path, "r") as f:
            auth_data = json.load(f)

        # Default to review.whamcloud.com, but allow override via GERRIT_HOST env var
        gerrit_host = os.environ.get("GERRIT_HOST", "review.whamcloud.com")

        if gerrit_host not in auth_data:
            print(f"Error: Gerrit host '{gerrit_host}' not found in auth file")
            print(f"Available hosts: {', '.join(auth_data.keys())}")
            return 1

        if "gerrit/http" not in auth_data[gerrit_host]:
            print(
                f"Error: 'gerrit/http' credentials not found for host '{gerrit_host}'"
            )
            return 1

        gerrit_username = auth_data[gerrit_host]["gerrit/http"]["username"]
        gerrit_password = auth_data[gerrit_host]["gerrit/http"]["password"]

        print(
            f"Loaded Gerrit credentials for {gerrit_host} (username: {gerrit_username})"
        )
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in auth file: {e}")
        return 1
    except KeyError as e:
        print(f"Error: Missing key in auth file: {e}")
        return 1

    # Validate github-pages requirements
    if args.hosting == "github-pages" and not args.github_token:
        print("Error: --github-token is required for github-pages hosting mode")
        return 1

    with podman.PodmanClient(base_url=socket_url) as client:
        try:
            client.images.get("ci-lustre:latest")
            print("Found ci-lustre:latest image")
        except:
            print("Error finding ci-lustre image!")
            return 1

        # Stop and remove existing container if it exists
        try:
            existing = client.containers.get(DEPLOY_CONTAINER_NAME)
            print(f"Found existing container {DEPLOY_CONTAINER_NAME}")
            try:
                print(f"Stopping existing container {DEPLOY_CONTAINER_NAME}...")
                existing.stop(timeout=10)
            except Exception as e:
                print(f"Warning: Failed to stop container: {e}")
                # Continue anyway to try removal

            try:
                print(f"Removing existing container {DEPLOY_CONTAINER_NAME}...")
                existing.remove()
                print(f"Removed existing container {DEPLOY_CONTAINER_NAME}")
            except Exception as e:
                print(f"Error: Failed to remove existing container: {e}")
                print(
                    f"You may need to manually remove it with: podman rm -f {DEPLOY_CONTAINER_NAME}"
                )
                return 1
        except podman.errors.exceptions.NotFound:
            # Container doesn't exist, that's fine
            pass
        except Exception as e:
            print(f"Warning: Error checking for existing container: {e}")
            # Try to force remove by name in case it's in a bad state
            try:
                print(f"Attempting to force remove container by name...")
                client.containers.get(DEPLOY_CONTAINER_NAME).remove(force=True)
                print(f"Successfully force removed container {DEPLOY_CONTAINER_NAME}")
            except Exception as e2:
                print(f"Error: Could not remove existing container: {e2}")
                print(
                    f"You may need to manually remove it with: podman rm -f {DEPLOY_CONTAINER_NAME}"
                )
                return 1

        # Prepare environment variables
        env = {
            "HOSTING_MODE": args.hosting,
            "GERRIT_USERNAME": gerrit_username,
            "GERRIT_PASSWORD": gerrit_password,
            "OUTPUT_DIR": "/var/www/ci-lustre/upstream-patch-review",
            "PODMAN_SOCKET": "/run/podman/podman.sock",
        }

        # Add GitHub token if provided
        if args.github_token:
            env["GITHUB_TOKEN"] = args.github_token

        # Prepare mounts - only mount podman socket
        mounts = [
            {
                "type": "bind",
                "source": host_socket_to_mount,
                "target": "/run/podman/podman.sock",
                "read_only": False,
            },
        ]

        # Prepare port bindings
        ports = {}
        if args.hosting == "nginx":
            ports = {"80/tcp": DEPLOY_NGINX_PORT}

        # Create and start the container
        print(f"Creating CI container '{DEPLOY_CONTAINER_NAME}'...")
        print(f"  Hosting mode: {args.hosting}")
        if args.hosting == "nginx":
            print(f"  Nginx port: {DEPLOY_NGINX_PORT}")
            print(f"  Output dir: {env['OUTPUT_DIR']}")

        container = client.containers.create(
            image="ci-lustre:latest",
            name=DEPLOY_CONTAINER_NAME,
            environment=env,
            mounts=mounts,
            ports=ports,
            detach=True,
            remove=False,
        )

        container.start()

        print()
        print(f"CI container '{DEPLOY_CONTAINER_NAME}' started successfully!")
        print()
        print("To view logs:")
        print(f"  podman logs -f {DEPLOY_CONTAINER_NAME}")
        print()
        print("To stop:")
        print(f"  podman stop {DEPLOY_CONTAINER_NAME}")
        print()
        if args.hosting == "nginx":
            print(
                f"Web interface will be available at: http://localhost:{DEPLOY_NGINX_PORT}/upstream-patch-review/"
            )
        elif args.hosting == "github-pages":
            print(
                f"Results will be pushed to: https://tim-day-387.github.io/upstream-patch-review/"
            )

        return 0
