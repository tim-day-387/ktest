# SPDX-License-Identifier: GPL-2.0-only

# Builds the Workbench desktop app (Electron + TypeScript).  Needs node and
# npm (>= 22) on PATH.  `deps` fetches the npm packages and vendors uBlock
# Origin, both of which need network access, so workbench is not part of the
# top-level `all`; use `make workbench` / `make workbench-install` there.

NPM ?= npm
NODE_MODULES := node_modules
UBLOCK       := vendor/ublock/manifest.json
SILENT       := $(if $(V),,--silent)
DEVNULL      := $(if $(V),,> /dev/null)

# Quiet by default when run directly; the top-level Makefile passes Q in.
ifeq ($(V),1)
  Q :=
else
  Q ?= @
endif


quiet_cmd = printf '  %-8s %s\n' '[$(1)]' '$(2)'


.PHONY: all deps build check fmt fmt-check dev install dist clean distclean

all: build

deps: $(NODE_MODULES) $(UBLOCK)

$(NODE_MODULES): package.json package-lock.json
	$(Q)$(call quiet_cmd,NPM,workbench/node_modules)
	$(Q)$(NPM) install $(SILENT)
	$(Q)touch $@

$(UBLOCK):
	$(Q)$(call quiet_cmd,FETCH,workbench/vendor/ublock)
	$(Q)$(NPM) run $(SILENT) fetch-ublock $(DEVNULL)

build: deps
	$(Q)$(call quiet_cmd,VITE,workbench/out)
	$(Q)$(NPM) run $(SILENT) build $(DEVNULL)

check: deps
	$(Q)$(call quiet_cmd,TSC,workbench)
	$(Q)$(NPM) run --silent typecheck

fmt: deps
	$(Q)$(call quiet_cmd,FMT,workbench)
	$(Q)$(NPM) run --silent fmt $(DEVNULL)

fmt-check: deps
	$(Q)$(NPM) run --silent fmt-check

dev: deps
	$(Q)$(NPM) run dev

install: deps
	$(Q)$(call quiet_cmd,INSTALL,~/.local/lib/workbench)
	$(Q)$(NPM) run $(SILENT) install-local $(DEVNULL)

dist: deps
	$(Q)$(call quiet_cmd,DIST,workbench/dist)
	$(Q)$(NPM) run $(SILENT) dist $(DEVNULL)

clean:
	$(Q)$(call quiet_cmd,CLEAN,workbench)
	$(Q)rm -rf out dist

distclean: clean
	$(Q)rm -rf $(NODE_MODULES) vendor/ublock
