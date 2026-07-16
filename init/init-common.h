/* SPDX-License-Identifier: GPL-2.0-only */

#ifndef KTEST_INIT_COMMON_H
#define KTEST_INIT_COMMON_H

#include <stddef.h>

/* Kernel log levels for /dev/kmsg */
#define KMSG_ERR	3
#define KMSG_INFO	6

void kmsg_open(const char *prog);
void kmsg_open_stdout(const char *prog);
void kmsg_log(int level, const char *fmt, ...);
int load_one_module(const char *modname, const char *release);

#endif /* KTEST_INIT_COMMON_H */
