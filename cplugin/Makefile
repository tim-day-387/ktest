# SPDX-License-Identifier: GPL-2.0

CMAKE ?= cmake
BUILD := build
BIN   := $(BUILD)/xunused
JOBS  ?= $(shell echo $$(( $$(nproc) / 2 > 1 ? $$(nproc) / 2 : 1 )))


quiet_cmd = printf '  %-8s %s\n' '[$(1)]' '$(2)'

# lit (the LLVM test driver) for 'make check': taken from PATH, from the
# newest llvm-NN-tools copy (apt.llvm.org ships /usr/lib/llvm-NN/bin/lit,
# Debian/Ubuntu ship /usr/lib/llvm-NN/build/utils/lit/lit.py), or pass
# LIT explicitly, e.g. LIT="python3 ~/src/llvm-project/llvm/utils/lit/lit.py"
LIT ?= $(shell command -v lit 2> /dev/null || command -v llvm-lit 2> /dev/null || \
	{ lit=$$(ls /usr/lib/llvm-*/bin/lit 2> /dev/null | sort -V | tail -1); \
	  test -n "$$lit" && echo "$$lit"; } || \
	{ lit=$$(ls /usr/lib/llvm-*/build/utils/lit/lit.py 2> /dev/null | sort -V | tail -1); \
	  test -n "$$lit" && echo "python3 $$lit"; })


.PHONY: all check clean

all: $(BIN)

check: $(BIN)
	$(Q)$(call quiet_cmd,LIT,test)
	$(Q)test -n "$(LIT)" || { \
		echo "lit not found: pip install lit, or set LIT=..." >&2; \
		exit 1; }
	$(Q)$(LIT) $(if $(V),-v,-sv) test

$(BIN):
	$(Q)$(call quiet_cmd,CMAKE,cplugin)
	$(Q)$(CMAKE) -S . -B $(BUILD) $(if $(V),,> /dev/null)
	$(Q)$(call quiet_cmd,BUILD,cplugin)
	$(Q)MAKEFLAGS= $(CMAKE) --build $(BUILD) --parallel $(JOBS) $(if $(V),,> /dev/null)

clean:
	$(Q)$(call quiet_cmd,CLEAN,cplugin)
	$(Q)rm -rf $(BUILD)
