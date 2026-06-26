// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE

/*
 * mount.lustreroot - mount a local ZFS-backed Lustre filesystem.
 *
 * Given a filesystem name, an (already imported) ZFS pool, and a target
 * directory, this brings up the three local server targets and mounts the
 * client:
 *
 *   1. MGS/MDT  (pool/<fsname>-mgs_mdt or pool/mgs_mdt)
 *   2. OST0     (pool/<fsname>ost0 or pool/ost0)
 *   3. OST1     (pool/<fsname>ost1 or pool/ost1)
 *   4. client   (0@lo:/<fsname>) mounted on <path>
 *
 * With --no-mgs the local MGS is *not* started; the same combined
 * mgs_mdt target is mounted with its embedded MGS left dormant, so the
 * MDT and OSTs instead register with an MGS already running on this node
 * (at 0@lo) - typically one brought up by an earlier mount.lustreroot for
 * a different filesystem.  Only one MGS can exist per node, so this is how
 * a second local filesystem shares the first filesystem's MGS.  The pool
 * layout (pool/<fsname>-mgs_mdt) is identical either way; only the mount
 * options differ.
 *
 * Server targets are mounted under /mnt/<svname> (e.g. /mnt/<fsname>-MDT0000)
 * so two filesystems brought up on the same node do not collide.
 *
 * All targets are local and reachable over the loopback LNet (network=lo);
 * the caller is responsible for having loaded the zfs/lustre modules,
 * disabled LNet peer discovery, and imported the pool beforehand.
 *
 * Usage: mount.lustreroot [--no-mgs] <fsname> <pool> <path>
 *
 * When invoked by /init in the initramfs (signalled by the
 * MOUNT_LUSTREROOT_FROM_INIT environment variable) this logs to /dev/kmsg
 * like /init does.  When run standalone from a shell it logs to stdout so the
 * operator can see what happened.
 *
 * Build (standalone, outside kernel tree):
 *   cc -Wall -static -o mount.lustreroot mount.lustreroot.c init-common.c
 */

#include "init-common.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>

/*
 * mount_lustre_target - Mount a single Lustre server target
 *
 * Issues a normal mount -t lustre call with the appropriate options.
 *
 * Returns 0 on success, -1 on failure.
 */
static int mount_lustre_target(const char *dataset, const char *mntpt,
			       const char *svname, int is_mgs)
{
	char mount_data[512];
	int rc;

	mkdir(mntpt, 0755);

	if (is_mgs)
		rc = snprintf(mount_data, sizeof(mount_data),
			      "svname=%s,mgs,update,network=lo,osd=osd-zfs,device=%s",
			      svname, dataset);
	else
		rc = snprintf(mount_data, sizeof(mount_data),
			      "svname=%s,mgsnode=0@lo,update,network=lo,osd=osd-zfs,device=%s",
			      svname, dataset);

	if (rc < 0 || (size_t)rc >= sizeof(mount_data)) {
		kmsg_log(KMSG_ERR, "mount data too long for %s\n", svname);
		return -1;
	}

	kmsg_log(KMSG_INFO, "mounting target %s (%s) at %s%s\n",
		 dataset, svname, mntpt, is_mgs ? " [MGS]" : "");

	if (mount(dataset, mntpt, "lustre", 0, mount_data) < 0) {
		kmsg_log(KMSG_ERR, "mount %s failed: %s\n",
			 svname, strerror(errno));
		return -1;
	}

	kmsg_log(KMSG_INFO, "target %s mounted successfully\n", svname);
	return 0;
}

/*
 * mount_lustre - bring up the local servers and mount the client on @path.
 *
 * Returns 0 on success, -1 on failure.
 */
static int mount_lustre(const char *fsname, const char *pool, const char *path,
			int start_mgs)
{
	char dataset[320], svname[64], mntpt[128], client_dev[320];
	char mount_data[512];
	const char *prefix;
	char prefix_buf[128];
	int use_prefix = 1;
	int rc;

	/*
	 * Step 1: Mount the combined MGS/MDT target (must be first - OSTs
	 * register with the MGS).  The dataset is always named mgs_mdt; the
	 * target is formatted as a combined MGS+MDT regardless of how it is
	 * mounted here.
	 *
	 * When start_mgs is set this target also starts the node's single MGS
	 * (the "mgs" mount flag).  Otherwise the embedded MGS is left dormant
	 * and only the MDT is brought up, registering with an MGS already
	 * running at 0@lo - that is how a second local filesystem shares the
	 * first filesystem's MGS.
	 *
	 * Detect the dataset naming convention by trying:
	 *   pool/<fsname>-mgs_mdt  (fsname-prefixed)
	 *   pool/mgs_mdt           (plain)
	 * Then use the same convention for all targets.
	 */
	snprintf(prefix_buf, sizeof(prefix_buf), "%s-", fsname);

	snprintf(svname, sizeof(svname), "%s-MDT0000", fsname);
	snprintf(mntpt, sizeof(mntpt), "/mnt/%s", svname);
	snprintf(dataset, sizeof(dataset), "%s/%s-mgs_mdt", pool, fsname);
	if (mount_lustre_target(dataset, mntpt, svname, start_mgs) < 0) {
		snprintf(dataset, sizeof(dataset), "%s/mgs_mdt", pool);
		if (mount_lustre_target(dataset, mntpt, svname, start_mgs) < 0)
			return -1;
		use_prefix = 0;
	}

	prefix = use_prefix ? prefix_buf : "";

	/* Step 2: Mount OST0 */
	snprintf(svname, sizeof(svname), "%s-OST0000", fsname);
	snprintf(mntpt, sizeof(mntpt), "/mnt/%s", svname);
	snprintf(dataset, sizeof(dataset), "%s/%sost0", pool, prefix);
	if (mount_lustre_target(dataset, mntpt, svname, 0) < 0)
		return -1;

	/* Step 3: Mount OST1 */
	snprintf(svname, sizeof(svname), "%s-OST0001", fsname);
	snprintf(mntpt, sizeof(mntpt), "/mnt/%s", svname);
	snprintf(dataset, sizeof(dataset), "%s/%sost1", pool, prefix);
	if (mount_lustre_target(dataset, mntpt, svname, 0) < 0)
		return -1;

	/*
	 * Step 4: Mount Lustre client to the local servers via loopback.
	 *
	 * The device string format for Lustre client mount is:
	 *   <mgs_nid>:/<fsname>
	 */
	snprintf(client_dev, sizeof(client_dev), "0@lo:/%s", fsname);
	rc = snprintf(mount_data, sizeof(mount_data),
		      "network=lo,user_xattr,device=%s", client_dev);
	if (rc < 0 || (size_t)rc >= sizeof(mount_data)) {
		kmsg_log(KMSG_ERR, "client mount data too long\n");
		return -1;
	}

	mkdir(path, 0755);

	kmsg_log(KMSG_INFO, "mounting lustre client %s on %s\n",
		 client_dev, path);

	if (mount(client_dev, path, "lustre", 0, mount_data) < 0) {
		kmsg_log(KMSG_ERR, "client mount failed: %s\n", strerror(errno));
		return -1;
	}

	kmsg_log(KMSG_INFO, "lustre %s mounted on %s\n", fsname, path);
	return 0;
}

int main(int argc, char **argv)
{
	const char *fsname, *pool, *path;
	int start_mgs = 1;
	int argi = 1;

	/*
	 * /init sets MOUNT_LUSTREROOT_FROM_INIT before exec'ing us; in that
	 * case log to the kernel ring buffer like /init.  Run standalone, log
	 * to stdout so the operator sees the result.
	 */
	if (getenv("MOUNT_LUSTREROOT_FROM_INIT"))
		kmsg_open("mount.lustreroot");
	else
		kmsg_open_stdout("mount.lustreroot");

	/* Optional flags precede the positional arguments. */
	for (; argi < argc && argv[argi][0] == '-'; argi++) {
		if (strcmp(argv[argi], "--no-mgs") == 0) {
			start_mgs = 0;
		} else {
			kmsg_log(KMSG_ERR, "unknown option: %s\n", argv[argi]);
			return 1;
		}
	}

	if (argc - argi != 3) {
		kmsg_log(KMSG_ERR,
			 "usage: mount.lustreroot [--no-mgs] <fsname> <pool> <path>\n");
		return 1;
	}

	fsname = argv[argi];
	pool   = argv[argi + 1];
	path   = argv[argi + 2];

	kmsg_log(KMSG_INFO,
		 "mounting lustre fsname=%s pool=%s path=%s mgs=%s\n",
		 fsname, pool, path, start_mgs ? "yes" : "no");

	if (mount_lustre(fsname, pool, path, start_mgs) < 0)
		return 1;

	return 0;
}
