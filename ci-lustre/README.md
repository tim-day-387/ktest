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
