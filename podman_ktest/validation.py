# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import os
from pathlib import Path
from typing import Optional, List

import podman

from .models import ValidationError
from .utils import get_podman_socket, get_ccache_dir, is_in_home


def _run_validation_container(client, command, devices=None, mounts=None):
    """Run a validation command in a container and return (success, output)."""
    try:
        container = client.containers.run(
            image="ktest-runner:latest",
            command=["bash", "-c", command],
            devices=devices or [],
            mounts=mounts or [],
            remove=True,
        )
        output = container.decode("utf-8") if container else ""
        return True, output
    except Exception as e:
        return False, str(e)


def _validate_kvm_exists(client) -> Optional[ValidationError]:
    """Check that /dev/kvm exists."""
    command = "test -e /dev/kvm && echo 'exists' || echo 'missing'"
    success, output = _run_validation_container(client, command, devices=["/dev/kvm"])

    if not success or "missing" in output:
        return ValidationError(
            check_name="kvm_exists",
            message="/dev/kvm does not exist",
            remediation="KVM support is required. Ensure your system supports virtualization and the kvm module is loaded.",
        )
    return None


def _validate_kvm_permissions(client) -> Optional[ValidationError]:
    """Check that /dev/kvm is readable and writable."""
    command = "test -r /dev/kvm && test -w /dev/kvm && echo 'ok' || echo 'denied'"
    success, output = _run_validation_container(client, command, devices=["/dev/kvm"])

    if not success or "denied" in output:
        return ValidationError(
            check_name="kvm_permissions",
            message="/dev/kvm is not readable/writable",
            remediation="Run: sudo chmod 666 /dev/kvm",
        )
    return None


def _validate_ccache_directory(client, ccache_dir) -> Optional[ValidationError]:
    """Create and validate the ccache directory."""
    ccache_path = Path(ccache_dir)
    parent_dir = str(ccache_path.parent.parent)

    # Create ccache directory locally if in home directory
    if is_in_home(ccache_dir):
        try:
            ccache_path.mkdir(parents=True, exist_ok=True)
            os.chmod(ccache_path, 0o777)
            os.chmod(ccache_path.parent, 0o777)
        except Exception as e:
            return ValidationError(
                check_name="ccache_directory",
                message=f"Failed to create ccache directory: {ccache_dir}",
                remediation=f"Ensure the parent directory exists and is writable: {parent_dir}",
            )
    else:
        command = f"mkdir -p {ccache_dir} && chmod 777 {ccache_dir} && echo 'ok'"
        mounts = [
            {
                "type": "bind",
                "source": parent_dir,
                "target": parent_dir,
                "read_only": False,
            }
        ]

        success, output = _run_validation_container(client, command, mounts=mounts)

        if not success or "ok" not in output:
            return ValidationError(
                check_name="ccache_directory",
                message=f"Failed to create ccache directory: {ccache_dir}",
                remediation=f"Ensure the parent directory exists and is writable: {parent_dir}",
            )

    return None


def _validate_ccache_writable(client, ccache_dir) -> Optional[ValidationError]:
    """Validate that the ccache directory is writable from inside the container."""
    mounts = [
        {
            "type": "bind",
            "source": ccache_dir,
            "target": "/tmp/ccache",
            "read_only": False,
        }
    ]

    success, output = _run_validation_container(
        client,
        "touch /tmp/ccache/.write_test && rm -f /tmp/ccache/.write_test && echo 'ok'",
        mounts=mounts,
    )

    if not success or "ok" not in output:
        return ValidationError(
            check_name="ccache_writable",
            message=f"ccache directory is not writable: {ccache_dir}",
            remediation=f"Fix permissions: chmod 777 {ccache_dir} (or rm -rf {ccache_dir}/* to clear stale cache)",
        )
    return None


def valid_env(podman_socket=None, shared_filesystem=None):
    """Validate the environment before running jobs.

    Each validation step runs in its own container.
    Returns True if environment is valid, False otherwise.
    """
    ccache_dir = get_ccache_dir(shared_filesystem)
    socket_url = get_podman_socket(podman_socket)
    errors: List[ValidationError] = []

    try:
        with podman.PodmanClient(base_url=socket_url) as client:
            # Run each validation in its own container
            validations = [
                ("Checking /dev/kvm exists", lambda: _validate_kvm_exists(client)),
                (
                    "Checking /dev/kvm permissions",
                    lambda: _validate_kvm_permissions(client),
                ),
                (
                    "Creating ccache directory",
                    lambda: _validate_ccache_directory(client, ccache_dir),
                ),
                (
                    "Checking ccache directory writable",
                    lambda: _validate_ccache_writable(client, ccache_dir),
                ),
            ]

            for description, validate_func in validations:
                print(f"  {description}...", end=" ", flush=True)
                error = validate_func()
                if error:
                    print("FAILED")
                    errors.append(error)
                else:
                    print("OK")

    except Exception as e:
        errors.append(
            ValidationError(
                check_name="podman_connection",
                message=f"Failed to connect to podman: {e}",
                remediation="Ensure podman is running and the socket is accessible.",
            )
        )

    # Report all errors
    if errors:
        print()
        print(f"Environment validation failed with {len(errors)} error(s):")
        print()
        for error in errors:
            print(f"  [{error.check_name}] {error.message}")
            if error.remediation:
                print(f"    Remediation: {error.remediation}")
            print()
        return False

    print()
    print("Environment validated successfully")
    return True
