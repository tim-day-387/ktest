# SPDX-License-Identifier: GPL-2.0-only

# Builds ktest-init (the compiled boot binary) and the mount.lustreroot
# helper.  Both are linked statically so they run without shared libs, and
# both share init-common.c (kmsg logging + lustre tunables).

CC      ?= cc
CFLAGS  ?= -Wall -g
LDFLAGS ?= -static

COMMON  := init-common.c
BINS    := ktest-init mount.lustreroot


quiet_cmd = printf '  %-8s %s\n' '[$(1)]' '$(2)'


.PHONY: all clean

all: $(BINS)

ktest-init: init.c $(COMMON) init-common.h
	$(Q)$(call quiet_cmd,CC,init/ktest-init)
	$(Q)$(CC) $(CFLAGS) $(LDFLAGS) -o $@ init.c $(COMMON)

mount.lustreroot: mount.lustreroot.c $(COMMON) init-common.h
	$(Q)$(call quiet_cmd,CC,init/mount.lustreroot)
	$(Q)$(CC) $(CFLAGS) $(LDFLAGS) -o $@ mount.lustreroot.c $(COMMON)

clean:
	$(Q)$(call quiet_cmd,CLEAN,init)
	$(Q)rm -f $(BINS)
