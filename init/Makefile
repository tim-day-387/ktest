# SPDX-License-Identifier: GPL-2.0

# Builds the initramfs /init and the mount.lustreroot helper.  Both are
# linked statically so they run without shared libs, and both share
# init-common.c (kmsg logging + module loading).

CC      ?= cc
CFLAGS  ?= -Wall -g
LDFLAGS ?= -static

COMMON  := init-common.c
BINS    := init mount.lustreroot


quiet_cmd = printf '  %-8s %s\n' '[$(1)]' '$(2)'


.PHONY: all clean

all: $(BINS)

init: init.c $(COMMON) init-common.h
	$(Q)$(call quiet_cmd,CC,init/init)
	$(Q)$(CC) $(CFLAGS) $(LDFLAGS) -o $@ init.c $(COMMON)

mount.lustreroot: mount.lustreroot.c $(COMMON) init-common.h
	$(Q)$(call quiet_cmd,CC,init/mount.lustreroot)
	$(Q)$(CC) $(CFLAGS) $(LDFLAGS) -o $@ mount.lustreroot.c $(COMMON)

clean:
	$(Q)$(call quiet_cmd,CLEAN,init)
	$(Q)rm -f $(BINS)
