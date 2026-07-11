# kexec into a /boot kernel

The `/boot/UkImage-*` kernels are systemd-stub UKIs. Distro kexec-tools (2.0.28)
can't load them; UKI support needs >= 2.0.30, so build a newer one once:

```sh
cd /tmp && curl -sO https://mirrors.edge.kernel.org/pub/linux/utils/kernel/kexec/kexec-tools-2.0.32.tar.xz
tar xf kexec-tools-2.0.32.tar.xz && cd kexec-tools-2.0.32 && ./configure && make -j"$(nproc)" && sudo make install
```

Then boot any image (`/usr/local/sbin/kexec`, uki loader, no `-s`):

```sh
sudo kexec -l /boot/UkImage-v11-7.1 --reuse-cmdline && sudo kexec -e
```
