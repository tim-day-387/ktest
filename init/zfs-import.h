/* SPDX-License-Identifier: GPL-2.0-only */

#ifndef KTEST_ZFS_IMPORT_H
#define KTEST_ZFS_IMPORT_H

#include <stddef.h>
#include <stdint.h>

/* MAXNAMELEN from the zfs_cmd_t layout. */
#define ZFS_POOL_NAME_MAX	256

unsigned char *zfs_read_pool_config(const char *device, size_t *conf_len,
				    uint64_t *guid, char *name);
int zfs_import_config(const char *pool, const unsigned char *conf,
		      size_t conf_len, uint64_t guid);
int zfs_import_pool(const char *pool, const char *device);

#endif /* KTEST_ZFS_IMPORT_H */
