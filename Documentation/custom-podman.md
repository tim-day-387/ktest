# Custom podman with patched container-libs

Build podman v6 against a local `container-libs` checkout (storage/common/image
monorepo) — e.g. to test containers/storage changes like native overlay on
Lustre — and install it over the distro podman so `pk` uses it.

Checkouts assumed at `~/ws/podman` and `~/ws/container-libs`.

## Build

Podman builds from its `vendor/` tree, so local libs are wired in with
replace directives and re-vendored:

```sh
cd ~/ws/podman
go mod edit \
  -replace go.podman.io/storage=../container-libs/storage \
  -replace go.podman.io/common=../container-libs/common \
  -replace go.podman.io/image/v5=../container-libs/image
make vendor
```

Build deps (Ubuntu 24.04):

```sh
sudo apt-get install libgpgme-dev libseccomp-dev libsystemd-dev
```

`libsystemd-dev` matters: without it podman silently builds without the
`systemd` tag — no journald logging/events, and `make install.systemd`
becomes a no-op. Check with `./hack/systemd_tag.sh` (must print `systemd`).

It also breaks `pk` in a confusing way: without the systemd tag
the default log driver falls back from `journald` to `k8s-file`, and
podman-py's `containers.run()` only collects output for `json-file`/`journald`
drivers — it silently returns `None`. Validation checks that grep for a
positive marker ("ok") then fail (`root image not found`, `ccache directory
is not writable`) even though the underlying commands succeed, while
negative-marker checks (KVM) pass vacuously.

After changing build tags, `make podman` may say "Nothing to be done" —
`rm -f bin/podman` first to force a relink.

```sh
make binaries        # bin/podman etc.; system Go 1.22 is fine, GOTOOLCHAIN fetches the right one
```

Iterating on the libs: edit in `~/ws/container-libs`, then
`make vendor && make podman` and reinstall.

Drop the replace directives before sending podman PRs — upstream CI rejects
them. Library changes go upstream as container-libs PRs.

## Install

```sh
sudo make install.bin install.systemd install.completions
hash -r && podman version    # /usr/local/bin/podman shadows /usr/bin
```

`pk` talks to the rootless API socket
(`/run/user/$UID/podman/podman.sock`), served by the systemd *user*
`podman.socket`/`podman.service`. The distro unit hardcodes
`ExecStart=/usr/bin/podman`; the units installed to
`/usr/local/lib/systemd/user/` take precedence, but only after:

```sh
systemctl --user daemon-reload && systemctl --user restart podman.socket
```

Verify what the socket actually serves:

```sh
curl -s --unix-socket /run/user/$UID/podman/podman.sock http://d/v4.0.0/libpod/version
```

## Runtime deps: distro versions are too old

Podman 6 writes OCI runtime spec v1.3.0 configs. Ubuntu 24.04's crun 1.14
rejects them with `crun: unknown version specified`, which buildah reports as
a bare `exit status 1` on the first RUN step of any build. Install a current
static crun and put it ahead of `/usr/bin/crun` (podman's built-in search
order checks `/usr/bin` first):

```sh
curl -sL -o /tmp/crun https://github.com/containers/crun/releases/download/1.28/crun-1.28-linux-amd64
sudo install -m755 /tmp/crun /usr/local/bin/crun

mkdir -p ~/.config/containers
cat > ~/.config/containers/containers.conf <<'EOF'
[engine.runtimes]
crun = ["/usr/local/bin/crun", "/usr/bin/crun"]
EOF
```

Distro netavark 1.4 is also old relative to podman 6 — if container
networking misbehaves, upgrade it the same way.

## Smoke test

```sh
podman run --rm docker.io/library/ubuntu:24.04 true
podman info --format '{{.Store.GraphDriverName}} {{.Store.GraphStatus}}'
```

On Lustre the graph driver should be `overlay` with
`Native Overlay Diff: true` (no fuse-overlayfs fallback). Then
`pk build` end to end.

## Back out

```sh
sudo rm /usr/local/bin/podman* /usr/local/bin/crun
sudo rm -rf /usr/local/lib/systemd/user/podman* /usr/local/lib/systemd/system/podman*
systemctl --user daemon-reload && systemctl --user restart podman.socket
```

The distro 4.9.3 package takes over again. Note storage written by podman 6
is not guaranteed to downgrade cleanly; `podman system reset` if the old
binary balks.
