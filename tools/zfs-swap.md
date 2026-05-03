# ZFS Swap Setup

ZFS swap uses a zvol (ZFS volume) as a block device backed by the pool. It
compresses well and avoids a separate swap partition, but requires careful
property selection.

## Create the zvol

```bash
sudo zfs create \
    -V 8G \
    -b $(getconf PAGESIZE) \
    -o compression=zle \
    -o logbias=throughput \
    -o sync=always \
    -o primarycache=metadata \
    -o secondarycache=none \
    -o com.sun:auto-snapshot=false \
    rootfs/swap
```

### Property rationale

| Property | Value | Why |
|----------|-------|-----|
| `-V 8G` | 8 GiB | Size to taste; 2–8G is typical for a dev machine |
| `-b $(getconf PAGESIZE)` | 4096 on x86_64 | Aligns ZFS block size to the kernel page size, avoiding read-modify-write on every swap page |
| `compression=zle` | zero-run encoding | Near-zero CPU cost; compresses zero pages well, which is common in swap |
| `logbias=throughput` | throughput | Writes data directly to the main pool rather than staging through the ZIL first, reducing write amplification |
| `sync=always` | always | Ensures swap writes are committed before the kernel reuses the page — prevents data corruption on power loss |
| `primarycache=metadata` | metadata only | **Critical.** Without this, ZFS caches swap data in ARC. Since swap is used precisely when memory is under pressure, caching it in ARC is counterproductive |
| `secondarycache=none` | none | Prevents swap data polluting L2ARC with ephemeral pages |
| `com.sun:auto-snapshot=false` | false | Prevents snapshot tools from snapshotting the swap volume, which is meaningless and wastes pool space |

## Activate

```bash
sudo mkswap /dev/zvol/rootfs/swap
sudo swapon /dev/zvol/rootfs/swap
```

Verify:

```bash
swapon --show
free -h
```

## Make persistent

Add to `/etc/fstab`:

```
/dev/zvol/rootfs/swap  none  swap  defaults  0  0
```

Or rely on the ZFS `org.freebsd:swap` property if your init supports it.

## Notes

- **No swap on this machine currently.** The system has 15 GiB RAM and no
  swap configured. Under memory pressure the OOM killer fires instead of
  paging. Adding swap provides headroom for workloads that spike briefly.

- **zswap interaction.** If zswap is enabled (see `tune-memory`), the kernel
  compresses cold pages in RAM before they reach the swap device. The zvol
  then acts as a last-resort backing store rather than the primary pressure
  valve. The two work well together.

- **Hibernation.** Swap-to-disk hibernation is not supported through a zvol
  on Linux. If you need hibernation, use a swap partition instead.

- **ZFS ARC and swap.** The `tune-memory` script caps ARC at 512 MiB.
  Combined with `primarycache=metadata` on the swap zvol, ARC will not grow
  to compete with swap for memory.
