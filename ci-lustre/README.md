## Install and Enable Service
```
cp ci-lustre/lustre-bot.service ~/.config/systemd/user/
systemctl --user enable lustre-bot.service
journalctl --user -u lustre-bot.service -f
```