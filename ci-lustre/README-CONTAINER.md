# Containerized Lustre CI

This is a containerized version of the Lustre Gerrit CI that runs entirely in a container and spawns job containers via the podman API on the host.

## Features

- **Containerized**: The CI daemon runs in a container with no dependencies on host directories
- **Flexible Hosting**: Choose between nginx (serve from container) or GitHub Pages (push to git repo)
- **Podman API**: Spawns job containers via the host's podman socket
- **No Host Paths**: Only requires the podman socket to be mounted
- **Easy Deployment**: Deploy with a single `podman-ktest deploy` command

## Prerequisites

1. **Podman**: Install podman on your host
2. **Gerrit Auth**: Create a JSON file with your Gerrit credentials:
   ```json
   {
       "review.whamcloud.com": {
           "gerrit/http": {
               "username": "your-username",
               "password": "your-password"
           }
       }
   }
   ```
3. **GitHub Repo** (optional, for GitHub Pages mode): A git repository for hosting the static site

## Deployment

### Option 1: Nginx Mode (Default)

Serve the CI site directly from the container using nginx:

```bash
./podman-ktest deploy \
  --hosting nginx \
  --gerrit-auth /path/to/gerrit-auth.json \
  --port 8080 \
  --name lustre-ci
```

The web interface will be available at `http://localhost:8080/upstream-patch-review/`

### Option 2: GitHub Pages Mode

Push the CI site to a GitHub repository:

```bash
./podman-ktest deploy \
  --hosting github-pages \
  --gerrit-auth /path/to/gerrit-auth.json \
  --github-repo git@github.com:yourusername/lustre-ci.git \
  --name lustre-ci
```

The container will:
1. Clone the GitHub repository to a temporary directory on the host
2. Generate static HTML pages in that directory
3. Automatically commit and push changes to GitHub every 4 minutes

Your GitHub Pages site will be updated automatically.

## How It Works

### Architecture

```
┌─────────────────────────────────────────────┐
│           Host Machine                      │
│                                             │
│  ┌────────────────────────────────────┐    │
│  │  Lustre CI Container               │    │
│  │                                    │    │
│  │  ├─ nginx (optional)               │    │
│  │  ├─ gerrit_build-and-test-new.py  │    │
│  │  │  (monitors Gerrit for patches) │    │
│  │  │                                 │    │
│  │  └─ Calls podman-ktest via API ───┼────┼──> Spawns Job Containers
│  │     --podman-socket=/run/...      │    │         │
│  └────────────────────────────────────┘    │         │
│          │                                 │         │
│          │ Mounts podman socket            │         ▼
│          ▼                                 │    ┌─────────────┐
│  /run/podman/podman.sock                   │    │ Job Runner  │
│                                             │    │ Containers  │
└─────────────────────────────────────────────┘    └─────────────┘
```

### Container Details

- **Base Image**: `ktest-runner:latest` (extends Containerfile.ktest.u24)
- **Additional Packages**: nginx, python3-requests, python3-dateutil, attr, podman Python bindings
- **Entrypoint**: Starts nginx (if in nginx mode) and the Gerrit daemon
- **Paths**:
  - `/var/www/ci-lustre/upstream-patch-review/` - Static site output
  - `/home/ktest/ci-lustre/` - CI scripts and styles
  - `/home/ktest/ktest/` - ktest repository
  - `/run/podman/podman.sock` - Podman socket (mounted from host)

### Podman Socket

The CI container needs access to the host's podman socket to spawn job containers. This is mounted at `/run/podman/podman.sock` in the container and passed to `podman-ktest` via the `--podman-socket` parameter.

## Management

### View Logs

```bash
podman logs -f lustre-ci
```

### Stop the CI

```bash
podman stop lustre-ci
```

### Restart the CI

```bash
podman restart lustre-ci
```

### Remove the CI

```bash
podman stop lustre-ci
podman rm lustre-ci
```

## Configuration

### Environment Variables

You can customize the CI behavior by setting environment variables when deploying:

- `HOSTING_MODE`: Set automatically by `--hosting` flag (nginx or github-pages)
- `GERRIT_HOST`: Gerrit server hostname (default: review.whamcloud.com)
- `GERRIT_PROJECT`: Gerrit project (default: fs/lustre-release)
- `GERRIT_USERNAME`: Gerrit HTTP authentication username (required, set from --gerrit-auth file)
- `GERRIT_PASSWORD`: Gerrit HTTP authentication password (required, set from --gerrit-auth file)
- `OUTPUT_DIR`: Output directory for static site (default: /var/www/ci-lustre/upstream-patch-review)
- `PODMAN_SOCKET`: Path to podman socket in container (default: /run/podman/podman.sock)

### Custom Configuration

To use custom environment variables, modify the deploy command or edit the container after creation.

## Differences from Original CI

1. **Containerized**: Runs entirely in a container, no systemd service needed
2. **No Host Paths**: Only mounts the podman socket (and optionally GitHub repo)
3. **Podman API**: Uses `--podman-socket` parameter to spawn job containers
4. **Flexible Hosting**: Choose nginx or GitHub Pages at deployment time
5. **Single Command Deploy**: Use `podman-ktest deploy` instead of manual setup

## Troubleshooting

### Container won't start

Check logs:
```bash
podman logs lustre-ci
```

### Job containers not spawning

Verify the podman socket is mounted correctly:
```bash
podman exec lustre-ci ls -l /run/podman/podman.sock
```

### Nginx not serving pages

Check if hosting mode is set to nginx:
```bash
podman exec lustre-ci env | grep HOSTING_MODE
```

### GitHub Pages not updating

1. Check that `--hosting=github-pages` was specified
2. Verify the GitHub repository was cloned correctly
3. Check container logs for git errors

## Building the CI Image Manually

If you want to build the CI image separately:

```bash
cd /path/to/ktest
podman build -f containers/Containerfile.ci-lustre -t ci-lustre:latest .
```

Note: This requires the `ktest-runner:latest` base image to exist. Build it first with:

```bash
./podman-ktest build
```
