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
