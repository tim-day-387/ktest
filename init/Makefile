# SPDX-License-Identifier: GPL-2.0-only

# Builds ktest-init (the compiled boot binary), the mount.lustreroot helper
# and the zimport CLI.  All are linked statically so they run without shared
# libs, and all share init-common.c (kmsg logging + lustre tunables);
# ktest-init and zimport also share zfs-import.c (the raw-ioctl pool
# importer).

CC      ?= cc
CFLAGS  ?= -Wall -g
LDFLAGS ?= -static

COMMON  := init-common.c
ZFS     := zfs-import.c
BINS    := ktest-init mount.lustreroot zimport


quiet_cmd = printf '  %-8s %s\n' '[$(1)]' '$(2)'


.PHONY: all clean

all: $(BINS)

ktest-init: init.c $(COMMON) $(ZFS) init-common.h zfs-import.h
	$(Q)$(call quiet_cmd,CC,init/ktest-init)
	$(Q)$(CC) $(CFLAGS) $(LDFLAGS) -o $@ init.c $(COMMON) $(ZFS)

mount.lustreroot: mount.lustreroot.c $(COMMON) init-common.h
	$(Q)$(call quiet_cmd,CC,init/mount.lustreroot)
	$(Q)$(CC) $(CFLAGS) $(LDFLAGS) -o $@ mount.lustreroot.c $(COMMON)

zimport: zimport.c $(COMMON) $(ZFS) init-common.h zfs-import.h
	$(Q)$(call quiet_cmd,CC,init/zimport)
	$(Q)$(CC) $(CFLAGS) $(LDFLAGS) -o $@ zimport.c $(COMMON) $(ZFS)

clean:
	$(Q)$(call quiet_cmd,CLEAN,init)
	$(Q)rm -f $(BINS)
