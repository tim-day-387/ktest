# SPDX-License-Identifier: GPL-2.0-only

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

import os
import platform
from pathlib import Path
from typing import Optional, List

from .models import ValidationError
from .utils import (
    get_podman_client,
    get_podman_socket,
    get_ccache_dir,
    get_package_dir,
    is_in_home,
)

KTEST_IMAGE = "ktest-runner:latest"

# Debian architecture suffix used by root_image (see lib/common.sh parse_arch)
DEBIAN_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())


def _run_validation_container(client, command, devices=None, mounts=None):
    """Run a validation command in a container and return (success, output)."""
    try:
        container = client.containers.run(
            image=KTEST_IMAGE,
            command=["bash", "-c", command],
            devices=devices or [],
            mounts=mounts or [],
            remove=True,
        )
        output = container.decode("utf-8") if container else ""
        return True, output
    except Exception as e:
        return False, str(e)


def _validate_podman_socket(client, podman_socket=None) -> Optional[ValidationError]:
    """Check that the podman socket is up and responding."""
    socket_url = get_podman_socket(podman_socket)

    try:
        if client.ping():
            return None
    except Exception:
        pass

    return ValidationError(
        check_name="podman_socket",
        message=f"podman socket is not responding: {socket_url}",
        remediation="Start the podman socket: systemctl --user enable --now podman.socket",
    )


def _validate_ktest_image(client) -> Optional[ValidationError]:
    """Check that the ktest container image exists."""
    try:
        if client.images.exists(KTEST_IMAGE):
            return None
    except Exception:
        pass

    return ValidationError(
        check_name="ktest_image",
        message=f"container image not found: {KTEST_IMAGE}",
        remediation="Build it with: podman-ktest build",
    )


def _validate_root_image(client) -> Optional[ValidationError]:
    """Check that a VM root image exists in /var/lib/ktest.

    The check runs in a container with /var/lib/ktest bind-mounted, so it
    tests the host path even when podman-ktest runs inside the ci-lustre
    container (bind-mount sources are resolved on the host, same as job
    containers -- see models.py).
    """
    if DEBIAN_ARCH is None:
        return ValidationError(
            check_name="root_image",
            message=f"unsupported architecture: {platform.machine()}",
            remediation="Only x86_64 and aarch64 are supported.",
        )

    root_image = f"/var/lib/ktest/root.{DEBIAN_ARCH}"
    command = f"test -f {root_image} && echo 'ok' || echo 'missing'"
    mounts = [
        {
            "type": "bind",
            "source": "/var/lib/ktest",
            "target": "/var/lib/ktest",
            "read_only": True,
        }
    ]

    success, output = _run_validation_container(client, command, mounts=mounts)

    if not success or "ok" not in output:
        return ValidationError(
            check_name="root_image",
            message=f"root image not found: {root_image}",
            remediation="Build it with: ./root_image create (or podman-ktest deploy)",
        )
    return None


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


def _validate_host_directory(
    client, dir_path, check_name, label, mount_source=None
) -> Optional[ValidationError]:
    """Create and validate a directory on the host.

    The directory is used as a bind-mount source for job containers, which
    podman resolves on the host. When podman-ktest runs inside the ci-lustre
    container, a local mkdir would land in the container's private filesystem,
    not the host -- so for non-home paths we create the directory via a
    container that bind-mounts an existing host ancestor (mount_source) and
    runs mkdir -p underneath it.

    mount_source defaults to the grandparent (the layout for <root>/ktest/<x>);
    callers whose path sits directly under the shared root pass it explicitly.
    """
    path = Path(dir_path)
    parent_dir = mount_source if mount_source else str(path.parent.parent)

    # Create directory locally if in home directory (host runs only)
    if is_in_home(dir_path):
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o777)
            os.chmod(path.parent, 0o777)
        except Exception:
            return ValidationError(
                check_name=check_name,
                message=f"Failed to create {label} directory: {dir_path}",
                remediation=f"Ensure the parent directory exists and is writable: {parent_dir}",
            )
    else:
        command = f"mkdir -p {dir_path} && chmod 777 {dir_path} && echo 'ok'"
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
                check_name=check_name,
                message=f"Failed to create {label} directory: {dir_path}",
                remediation=f"Ensure the parent directory exists and is writable: {parent_dir}",
            )

    return None


def _validate_ccache_directory(client, ccache_dir) -> Optional[ValidationError]:
    """Create and validate the ccache directory."""
    return _validate_host_directory(client, ccache_dir, "ccache_directory", "ccache")


def _validate_package_directory(
    client, package_dir, shared_filesystem=None
) -> Optional[ValidationError]:
    """Create and validate the package output directory.

    The default (/tmp/ktest-packages) sits directly under /tmp, and the shared
    variant under the shared-filesystem root, so bind-mount that root rather
    than the path's grandparent (which would be / for the default).
    """
    mount_source = shared_filesystem if shared_filesystem else "/tmp"
    return _validate_host_directory(
        client, package_dir, "package_directory", "package output", mount_source
    )


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
    package_dir = get_package_dir(shared_filesystem)
    errors: List[ValidationError] = []
    check_lines: List[str] = []

    try:
        with get_podman_client(podman_socket) as client:
            # The socket and image checks are prerequisites for every other
            # check, which all run inside a ktest-runner container -- if
            # either fails, the rest are skipped rather than reported as a
            # cascade of misleading failures.
            prerequisites = [
                (
                    "Checking podman socket",
                    lambda: _validate_podman_socket(client, podman_socket),
                ),
                (
                    "Checking ktest container image",
                    lambda: _validate_ktest_image(client),
                ),
            ]

            # Run each validation in its own container
            validations = [
                ("Checking /dev/kvm exists", lambda: _validate_kvm_exists(client)),
                (
                    "Checking /dev/kvm permissions",
                    lambda: _validate_kvm_permissions(client),
                ),
                ("Checking root image", lambda: _validate_root_image(client)),
                (
                    "Creating ccache directory",
                    lambda: _validate_ccache_directory(client, ccache_dir),
                ),
                (
                    "Checking ccache directory writable",
                    lambda: _validate_ccache_writable(client, ccache_dir),
                ),
            ]

            # The package output dir is only created up-front for a shared
            # filesystem, where podman-ktest runs inside the ci-lustre container
            # and the bind-mount source must be created on the host. A plain
            # host run creates it lazily at job time instead.
            if shared_filesystem:
                validations.append(
                    (
                        "Creating package output directory",
                        lambda: _validate_package_directory(
                            client, package_dir, shared_filesystem
                        ),
                    )
                )

            # Buffer the per-check results; they are only printed if a check
            # fails, to keep successful runs (e.g. `job --stdout`) uncluttered.
            # The prerequisites are ordered dependencies (no socket means the
            # image check can't run either), so stop at the first failure.
            for description, validate_func in prerequisites:
                error = validate_func()
                if error:
                    check_lines.append(f"  {description}... FAILED")
                    errors.append(error)
                    break
                check_lines.append(f"  {description}... OK")

            if not errors:
                for description, validate_func in validations:
                    error = validate_func()
                    if error:
                        check_lines.append(f"  {description}... FAILED")
                        errors.append(error)
                    else:
                        check_lines.append(f"  {description}... OK")

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
        for line in check_lines:
            print(line)
        print()
        print(f"Environment validation failed with {len(errors)} error(s):")
        print()
        for error in errors:
            print(f"  [{error.check_name}] {error.message}")
            if error.remediation:
                print(f"    Remediation: {error.remediation}")
            print()
        return False

    print("Environment validated successfully")
    return True
