# ktest

Kernel test harness adapted for Lustre.

## Setup

```
systemctl --user enable --now podman.socket
pk setup          # writes ~/.ktestrc with kernel and lustre source paths
pk build          # builds the ktest-runner container (--all for distro images too)
```

## Usage

Run jobs:

```
pk job mainline/ml                      # one job
pk job distro/u24 mainline/ml-llmount   # several
pk job lustre-ci                        # a .group file
```

Results go to `/tmp/ktest-results`. Packages go to `/tmp/ktest-packages`.

Run a command in the container directly:

```
pk run ls -l
pk run whoami
```
