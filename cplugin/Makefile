# SPDX-License-Identifier: GPL-2.0

CMAKE ?= cmake
BUILD := build
BIN   := $(BUILD)/xunused
JOBS  ?= $(shell echo $$(( $$(nproc) / 2 > 1 ? $$(nproc) / 2 : 1 )))


quiet_cmd = printf '  %-8s %s\n' '[$(1)]' '$(2)'


.PHONY: all clean

all: $(BIN)

$(BIN):
	$(Q)$(call quiet_cmd,CMAKE,cplugin)
	$(Q)$(CMAKE) -S . -B $(BUILD) $(if $(V),,> /dev/null)
	$(Q)$(call quiet_cmd,BUILD,cplugin)
	$(Q)MAKEFLAGS= $(CMAKE) --build $(BUILD) --parallel $(JOBS) $(if $(V),,> /dev/null)

clean:
	$(Q)$(call quiet_cmd,CLEAN,cplugin)
	$(Q)rm -rf $(BUILD)
