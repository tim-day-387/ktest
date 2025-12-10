# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2026, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

from pathlib import Path


def cmd_setup(args, dirs=None):
    """Interactive setup to create ~/.ktestrc configuration file."""
    ktestrc_path = Path.home() / ".ktestrc"

    print("podman-ktest setup")
    print("=" * 60)
    print()

    if ktestrc_path.exists():
        print(f"Warning: {ktestrc_path} already exists")
        response = input("Do you want to overwrite it? [y/N]: ").strip().lower()
        if response != "y":
            print("Setup cancelled")
            return 0
        print()

    # Prompt for kernel source directory
    print("Kernel source directory:")
    print("  This is the directory containing your Linux kernel source code")
    default_kernel = str(Path.home() / "git" / "linux")
    kernel_source = input(f"  Path [{default_kernel}]: ").strip()
    if not kernel_source:
        kernel_source = default_kernel

    kernel_path = Path(kernel_source).expanduser().resolve()
    if not kernel_path.exists():
        print(f"  Warning: {kernel_path} does not exist")
        response = input("  Continue anyway? [y/N]: ").strip().lower()
        if response != "y":
            print("Setup cancelled")
            return 1
    print()

    # Prompt for lustre source directory
    print("Lustre source directory:")
    print("  This is the directory containing your Lustre source code")
    default_lustre = str(kernel_path.parent / "lustre-release")
    lustre_source = input(f"  Path [{default_lustre}]: ").strip()
    if not lustre_source:
        lustre_source = default_lustre

    lustre_path = Path(lustre_source).expanduser().resolve()
    if not lustre_path.exists():
        print(f"  Warning: {lustre_path} does not exist")
        response = input("  Continue anyway? [y/N]: ").strip().lower()
        if response != "y":
            print("Setup cancelled")
            return 1
    print()

    # Create the ktestrc file
    print(f"Creating {ktestrc_path}...")
    ktestrc_content = f"""#!/bin/bash
# ktest configuration file
# This file is sourced as a shell script

# Kernel source directory
export ktest_kernel_source="{kernel_path}"

# Lustre source directory
export ktest_lustre_source="{lustre_path}"
"""

    try:
        with open(ktestrc_path, "w") as f:
            f.write(ktestrc_content)
        ktestrc_path.chmod(0o644)
        print(f"Successfully created {ktestrc_path}")
        print()
        print("Configuration:")
        print(f"  ktest_kernel_source={kernel_path}")
        print(f"  ktest_lustre_source={lustre_path}")
        print()
        print("Setup complete!")
        return 0
    except Exception as e:
        print(f"Error: Failed to create {ktestrc_path}: {e}")
        return 1
