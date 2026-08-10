// SPDX-License-Identifier: GPL-2.0-only

/*
 * zimport - import a ZFS pool from a device by raw /dev/zfs ioctl.
 *
 * Root-image pools are created on image files, so their labels record the
 * leaf vdev as type=file with the build-time file path; once the image is
 * on a block device, stock zpool(8) shows such a pool in an import scan
 * but cannot import it (the scan fixes the vdev path, never the type).
 * This tool wraps the same importer ktest-init uses at boot
 * (init/zfs-import.c): it reads the label, rewrites the leaf vdev's path
 * to the given device and its type from file to disk, and issues
 * ZFS_IOC_POOL_IMPORT.  The pool name defaults to the one in the label.
 *
 * After one successful import ZFS rewrites the labels itself, so
 * subsequent imports work with stock zpool(8).  See also
 * tools/zlabel-file2disk, which rewrites the labels without importing.
 */

#include "init-common.h"
#include "zfs-import.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void usage(FILE *out)
{
	fprintf(out,
		"Usage: zimport [-p POOL] [-n] DEVICE\n"
		"\n"
		"Import the ZFS pool on DEVICE via ZFS_IOC_POOL_IMPORT,\n"
		"rewriting the leaf vdev's path to DEVICE and its type from\n"
		"file to disk (root images are pools created on files).\n"
		"\n"
		"Options:\n"
		"  -p POOL   import under this name (default: name in the label)\n"
		"  -n        read and report the label, don't import\n"
		"  -h        show this help\n");
}

int main(int argc, char **argv)
{
	const char *device = NULL, *pool = NULL;
	char label_name[ZFS_POOL_NAME_MAX] = "";
	unsigned char *conf;
	size_t conf_len = 0;
	uint64_t guid = 0;
	int dry_run = 0, rc, i;

	kmsg_open_stdout("zimport");

	for (i = 1; i < argc; i++) {
		if (strcmp(argv[i], "-h") == 0 ||
		    strcmp(argv[i], "--help") == 0) {
			usage(stdout);
			return 0;
		} else if (strcmp(argv[i], "-n") == 0) {
			dry_run = 1;
		} else if (strcmp(argv[i], "-p") == 0) {
			if (++i == argc) {
				fprintf(stderr, "zimport: -p needs a value\n");
				return 1;
			}
			pool = argv[i];
		} else if (argv[i][0] == '-') {
			fprintf(stderr, "zimport: unknown option: %s\n",
				argv[i]);
			usage(stderr);
			return 1;
		} else if (!device) {
			device = argv[i];
		} else {
			fprintf(stderr, "zimport: only one device may be given\n");
			return 1;
		}
	}
	if (!device) {
		usage(stderr);
		return 1;
	}

	conf = zfs_read_pool_config(device, &conf_len, &guid, label_name);
	if (!conf) {
		fprintf(stderr, "zimport: no valid label on %s\n", device);
		return 1;
	}
	if (!pool)
		pool = label_name;
	if (!pool[0]) {
		fprintf(stderr,
			"zimport: label on %s has no pool name - use -p\n",
			device);
		free(conf);
		return 1;
	}

	if (dry_run) {
		printf("pool '%s' guid %llu on %s (dry run, not imported)\n",
		       pool, (unsigned long long)guid, device);
		free(conf);
		return 0;
	}

	rc = zfs_import_config(pool, conf, conf_len, guid);
	free(conf);
	return rc ? 1 : 0;
}
