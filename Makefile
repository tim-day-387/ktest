# SPDX-License-Identifier: GPL-2.0

# Use bash so that pipefail propagates cargo's exit status through the
# JSON output filter (otherwise a compile failure would be masked).
SHELL        := /bin/bash
.SHELLFLAGS  := -o pipefail -c


CARGO  ?= cargo
BIN    := lustre-ktest
FILTER := tools/cargo-quiet
SANITY_DIR  := tests/fs/lustre/sanity
CPLUGIN_DIR := cplugin
INIT_DIR    := init
JOBS := $(shell echo $$(( $$(nproc) / 2 > 1 ? $$(nproc) / 2 : 1 )))
MAKEFLAGS += -j$(JOBS)


ifeq ($(V),1)
  Q :=
  # Verbose: run cargo directly, no filtering.
  cargo_run = $(CARGO) $(1)
else
  Q := @
  # Quiet: suppress cargo's own progress (--quiet) and reformat the JSON
  # build events kernel-style.
  cargo_run = $(CARGO) $(1) --quiet --message-format=json | $(FILTER)
endif


# quiet_cmd prints a single kernel-style "  [TAG]    target" line, used for
# the targets that don't produce per-crate build events.
quiet_cmd = printf '  %-8s %s\n' '[$(1)]' '$(2)'


.PHONY: all build release check test clippy fmt clean help gen-tests cplugin init-bins

all: release cplugin init-bins

gen-tests:
	$(Q)$(MAKE) --no-print-directory -C $(SANITY_DIR) Q=$(Q)

cplugin:
	$(Q)$(MAKE) --no-print-directory -C $(CPLUGIN_DIR) Q=$(Q) V=$(V) JOBS=$(JOBS)

init-bins:
	$(Q)$(MAKE) --no-print-directory -C $(INIT_DIR) Q=$(Q)

build: gen-tests
	$(Q)$(call cargo_run,build)

release: gen-tests
	$(Q)$(call cargo_run,build --release)

check:
	$(Q)$(call cargo_run,check)

clippy:
	$(Q)$(call cargo_run,clippy)

test:
	@$(call quiet_cmd,TEST,$(BIN))
	$(Q)$(CARGO) test $(if $(V),,--quiet)

fmt:
	@$(call quiet_cmd,FMT,$(BIN))
	$(Q)$(CARGO) fmt

clean:
	@$(call quiet_cmd,CLEAN,target)
	$(Q)$(CARGO) clean $(if $(V),,--quiet)
	$(Q)$(MAKE) --no-print-directory -C $(SANITY_DIR) clean Q=$(Q)
	$(Q)$(MAKE) --no-print-directory -C $(CPLUGIN_DIR) clean Q=$(Q)
	$(Q)$(MAKE) --no-print-directory -C $(INIT_DIR) clean Q=$(Q)

help:
	@echo 'Targets:'
	@echo '  build     - debug build'
	@echo '  release   - optimized build'
	@echo '  cplugin   - build the xunused C++ tool'
	@echo '  init-bins - build the initramfs init + mount.lustreroot'
	@echo '  gen-tests - generate Lustre sanity test runners'
	@echo '  check     - cargo check'
	@echo '  test      - run tests'
	@echo '  clippy    - run clippy lints'
	@echo '  fmt       - format sources'
	@echo '  clean     - remove build artifacts'
	@echo
	@echo 'Use "make V=1" for verbose cargo output.'
