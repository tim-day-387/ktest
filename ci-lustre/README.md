# Containerized Lustre CI

This is a containerized version of the Lustre Gerrit CI that runs entirely in a container and spawns job containers via the podman API on the host.

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

## Podman Ktest CI

### Option 1: Nginx Mode (Default)

Serve the CI site directly from the container using nginx:

```bash
ssh -N -L /tmp/socket.sock:/run/user/387/podman/podman.sock me@lustre-bot

podman-ktest --podman-socket unix:///tmp/socket.sock \
  build
podman-ktest --podman-socket unix:///tmp/socket.sock \
  deploy \
  --hosting nginx \
  --gerrit-auth /path/to/gerrit-auth.json
```

The web interface will be available at `http://localhost:8080/upstream-patch-review/`

### Option 2: GitHub Pages Mode

Push the CI site to a GitHub repository:

```
ssh -N -L /tmp/socket.sock:/run/user/387/podman/podman.sock me@lustre-bot

podman-ktest --podman-socket unix:///tmp/socket.sock \
  build
podman-ktest --podman-socket unix:///tmp/socket.sock \
  deploy \
  --hosting github-pages \
  --gerrit-auth /my/auth/file \
  --ci-container-socket unix:///run/user/387/podman/podman.sock \
  --github-token mytesttoken

```

The container will:
1. Clone the GitHub repository to a temporary directory on the host
2. Generate static HTML pages in that directory
3. Automatically commit and push changes to GitHub every 4 minutes

Your GitHub Pages site will be updated automatically.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│           Host Machine                                                  │                          
│                                                                         │
│  ┌────────────────────────────────────┐                                 │
│  │  Lustre CI Container               │                                 │
│  │                                    │                                 │
│  │  ├─ nginx (optional)               │                                 │
│  │  ├─ gerrit_build-and-test-new.py   │                                 │
│  │  │  (monitors Gerrit for patches)  │                                 │
│  │  │                                 │                                 │
│  │  └─ Calls podman-ktest via API ────┼─────> Spawns Job Containers     │
│  │     --podman-socket=/run/...       │              │                  │
│  └────────────────────────────────────┘              ▼                  │
│          │                                      ┌─────────────┐         │
│          │ Mounts podman socket                 │ Job Runner  │         │     
│          ▼                                      │ Containers  │         │
│  /host/podman/podman.sock                       └─────────────┘         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘    
```

## Management

The CI container needs access to the host's podman socket to spawn job containers. This is mounted at `/run/podman/podman.sock` in the container and passed to `podman-ktest` via the `--podman-socket` parameter.

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

# Legacy Notes

## Install and Enable Service
```
cp ci-lustre/lustre-bot.service ~/.config/systemd/user/
systemctl --user enable lustre-bot.service
journalctl --user -u lustre-bot.service -f
```

## Get Clang and LLVM

https://apt.llvm.org/

```
bash -c "$(wget -O - https://apt.llvm.org/llvm.sh)"
```

## Enable ccache
```
sudo ln -s /path/to/ccache /usr/local/bin/clang-20
```

## Enable login linger
```
loginctl enable-linger myusername
```

## Install libguestfs
```
sudo apt-get install libguestfs-tools
sudo chmod 0644 /boot/vmlinuz*
```

## Podman
```
podman build -t ktest-runner -f ci-lustre/Containerfile.ktest-runner .
podman run -it --pids-limit 100000 -v /boot:/boot:ro -v /home/timothy/git/linux:/home/ktest/git/linux:O -v /home/timothy/git/lustre-release/:/home/ktest/git/lustre-release/:O -v /var/lib/ktest/:/var/lib/ktest/:O --rm ktest-runner:latest ./qlkbuild build --purge-ktest-out 1 --clean-git 1 --allow-warnings 1 --build-lustre 1
```

