# SPDX-License-Identifier: GPL-2.0

#
# Copyright (c) 2025, Amazon and/or its affiliates. All rights reserved.
# Use is subject to license terms.
#

#
# Author: Timothy Day <timday@amazon.com>
#

all:
	@for i in $(shell seq 1 50); do \
		cp "sanity.ktest.in" "sanity-$$i.ktest"; \
		sed -i "s/INDEX/$$i/g" "sanity-$$i.ktest"; \
		sed -i "s/BATCH/10/g" "sanity-$$i.ktest"; \
	done
