# SPDX-License-Identifier: GPL-2.0

COUNT    ?= 50
BATCH    ?= 10
TEMPLATE := sanity.ktest.in


quiet_cmd = printf '  %-8s %s\n' '[$(1)]' '$(2)'


.PHONY: all clean

all:
	$(Q)$(call quiet_cmd,GEN,sanity tests)
	$(Q)for i in $$(seq 1 $(COUNT)); do \
		sed -e "s/INDEX/$$i/g" -e "s/BATCH/$(BATCH)/g" $(TEMPLATE) > sanity-$$i.ktest; \
		chmod 0755 sanity-$$i.ktest; \
	done

clean:
	$(Q)$(call quiet_cmd,CLEAN,sanity tests)
	$(Q)rm -f sanity-*.ktest
