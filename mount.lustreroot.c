// SPDX-License-Identifier: GPL-2.0

/*
 * Copyright (c) 2024, OpenSFS.
 *
 * mount.lustreroot - Mount Lustre root filesystem from kernel command line
 *
 * Reads the lustreroot= parameter from /proc/cmdline, starts local ZFS-backed
 * Lustre servers (MGS/MDT + OSTs) via normal mount calls, then mounts the
 * Lustre client on /newroot and pivots into it.
 *
 * Intended to run as /init in an initramfs when the kernel command line
 * contains:
 *   lustreroot=<pool>,device=<path>[,fsname=<name>]
 *
 * The initramfs issues individual mount -t lustre_tgt calls for each server
 * target, then mount -t lustre for the client. No custom kernel mount code
 * is required.
 *
 * Build (standalone, outside kernel tree):
 *   cc -Wall -static -o mount.lustreroot mount.lustreroot.c
 */

#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define PROG		"mount.lustreroot"
#define MOUNTPOINT	"/newroot"
#define CMDLINE_PATH	"/proc/cmdline"
#define KMSG_PATH	"/dev/kmsg"
#define CMDLINE_MAX	4096

/* Kernel log levels for /dev/kmsg */
#define KMSG_ERR	3
#define KMSG_INFO	6

static int kmsg_fd = -1;

static void kmsg_open(void)
{
	kmsg_fd = open(KMSG_PATH, O_WRONLY | O_CLOEXEC);
}

static void kmsg_log(int level, const char *fmt, ...)
{
	char buf[512];
	va_list ap;
	int len;

	if (kmsg_fd < 0)
		return;

	len = snprintf(buf, sizeof(buf), "<%d>" PROG ": ", level);
	if (len < 0 || len >= (int)sizeof(buf))
		return;

	va_start(ap, fmt);
	vsnprintf(buf + len, sizeof(buf) - len, fmt, ap);
	va_end(ap);

	buf[sizeof(buf) - 1] = '\0';
	write(kmsg_fd, buf, strlen(buf));
}

/*
 * find_cmdline_arg - locate a named argument in a kernel cmdline string
 *
 * Searches @cmdline for a token starting with @name= and returns a pointer
 * to the value portion (after the '='), or NULL if not found.
 */
static char *find_cmdline_arg(char *cmdline, const char *name)
{
	size_t namelen = strlen(name);
	char *p = cmdline;

	while ((p = strstr(p, name)) != NULL) {
		/* Must be at start of cmdline or preceded by whitespace */
		if (p != cmdline && p[-1] != ' ' && p[-1] != '\t') {
			p++;
			continue;
		}
		if (p[namelen] == '=')
			return p + namelen + 1;
		p++;
	}
	return NULL;
}

/*
 * parse_lustreroot - extract pool, device, and fsname from lustreroot= cmdline value
 *
 * The value format is: <pool>,device=<path>[,fsname=<name>][,...]
 *
 * Writes the pool name into @pool (size @pool_size), the block device path
 * into @device (size @device_size), and the filesystem name into @fsname
 * (size @fsname_size).  If no fsname= sub-option is present, @fsname defaults
 * to "lustre".  The device= sub-option is required.
 *
 * Returns 0 on success, -1 on parse error.
 */
static int parse_lustreroot(const char *value,
			     char *pool, size_t pool_size,
			     char *device, size_t device_size,
			     char *fsname, size_t fsname_size)
{
	char buf[256];
	char *p, *tok, *save;

	strncpy(fsname, "lustre", fsname_size - 1);
	fsname[fsname_size - 1] = '\0';
	device[0] = '\0';

	/* Work on a local copy; value ends at whitespace or end-of-string */
	{
		size_t vlen = strcspn(value, " \t\n");

		if (vlen >= sizeof(buf)) {
			kmsg_log(KMSG_ERR, "lustreroot= value too long\n");
			return -1;
		}
		memcpy(buf, value, vlen);
		buf[vlen] = '\0';
	}

	/* First token (before first comma) is the pool name */
	tok = strtok_r(buf, ",", &save);
	if (!tok || !*tok) {
		kmsg_log(KMSG_ERR, "lustreroot= missing pool name\n");
		return -1;
	}
	strncpy(pool, tok, pool_size - 1);
	pool[pool_size - 1] = '\0';

	/* Remaining comma-separated tokens are key=value sub-options */
	while ((tok = strtok_r(NULL, ",", &save)) != NULL) {
		if (strncmp(tok, "device=", 7) == 0) {
			p = tok + 7;
			if (!*p) {
				kmsg_log(KMSG_ERR, "empty device=\n");
				return -1;
			}
			strncpy(device, p, device_size - 1);
			device[device_size - 1] = '\0';
		} else if (strncmp(tok, "fsname=", 7) == 0) {
			p = tok + 7;
			if (!*p) {
				kmsg_log(KMSG_ERR, "empty fsname=\n");
				return -1;
			}
			strncpy(fsname, p, fsname_size - 1);
			fsname[fsname_size - 1] = '\0';
		}
	}

	if (!device[0]) {
		kmsg_log(KMSG_ERR, "lustreroot= requires device=<path>\n");
		return -1;
	}
	return 0;
}

/*
 * mount_lustre_target - Mount a single Lustre server target
 *
 * Issues a normal mount -t lustre_tgt call with the appropriate options.
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

int main(void)
{
	char cmdline[CMDLINE_MAX];
	char pool[256], device[256], fsname[64];
	char dataset[320], svname[64], client_dev[320];
	char mount_data[512];
	char *lustreroot_val;
	FILE *f;
	int rc;

	/* Mount essential pseudo-filesystems */
	mount("proc",     "/proc", "proc",     0, NULL);
	mount("sysfs",    "/sys",  "sysfs",    0, NULL);
	mount("devtmpfs", "/dev",  "devtmpfs", 0, NULL);

	kmsg_open();
	kmsg_log(KMSG_INFO, "starting lustre root filesystem setup\n");

	/*
	 * Disable LNet Dynamic Peer Discovery before any Lustre mounts.
	 * When discovery is enabled, the "network=" mount option is rejected.
	 * Since all targets are local (loopback), discovery is not needed.
	 */
	{
		int dfd = open("/sys/module/lnet/parameters/lnet_peer_discovery_disabled",
			       O_WRONLY);
		if (dfd >= 0) {
			write(dfd, "1", 1);
			close(dfd);
			kmsg_log(KMSG_INFO, "disabled LNet peer discovery\n");
		} else {
			kmsg_log(KMSG_ERR, "cannot disable peer discovery: %s\n",
				 strerror(errno));
		}
	}

	/* Read kernel command line */
	f = fopen(CMDLINE_PATH, "r");
	if (!f) {
		kmsg_log(KMSG_ERR, "cannot open %s: %s\n",
			 CMDLINE_PATH, strerror(errno));
		return 1;
	}
	if (!fgets(cmdline, sizeof(cmdline), f)) {
		kmsg_log(KMSG_ERR, "cannot read %s: %s\n",
			 CMDLINE_PATH, strerror(errno));
		fclose(f);
		return 1;
	}
	fclose(f);

	/* Find lustreroot= argument */
	lustreroot_val = find_cmdline_arg(cmdline, "lustreroot");
	if (!lustreroot_val) {
		kmsg_log(KMSG_ERR, "lustreroot= not found in %s\n", CMDLINE_PATH);
		return 1;
	}

	/* Parse pool, device, and fsname out of the boot parameter value */
	if (parse_lustreroot(lustreroot_val, pool, sizeof(pool),
			     device, sizeof(device),
			     fsname, sizeof(fsname)) < 0)
		return 1;

	kmsg_log(KMSG_INFO, "pool=%s device=%s fsname=%s\n",
		 pool, device, fsname);

	/*
	 * Step 1: Mount MGS/MDT (must be first - OSTs register with MGS)
	 *
	 * Detect dataset naming convention by trying the MGS/MDT:
	 *   pool/fsname-mgs_mdt  (fsname-prefixed)
	 *   pool/mgs_mdt         (plain)
	 * Then use the same convention for all targets.
	 */
	{
		const char *prefix;
		char prefix_buf[128];
		int use_prefix = 1;

		snprintf(prefix_buf, sizeof(prefix_buf), "%s-", fsname);

		snprintf(dataset, sizeof(dataset), "%s/%s-mgs_mdt", pool, fsname);
		snprintf(svname, sizeof(svname), "%s-MDT0000", fsname);
		if (mount_lustre_target(dataset, "/mnt/mgs", svname, 1) < 0) {
			snprintf(dataset, sizeof(dataset), "%s/mgs_mdt", pool);
			if (mount_lustre_target(dataset, "/mnt/mgs", svname, 1) < 0)
				return 1;
			use_prefix = 0;
		}

		prefix = use_prefix ? prefix_buf : "";

		/* Step 2: Mount OST0 */
		snprintf(dataset, sizeof(dataset), "%s/%sost0", pool, prefix);
		snprintf(svname, sizeof(svname), "%s-OST0000", fsname);
		if (mount_lustre_target(dataset, "/mnt/ost0", svname, 0) < 0)
			return 1;

		/* Step 3: Mount OST1 */
		snprintf(dataset, sizeof(dataset), "%s/%sost1", pool, prefix);
		snprintf(svname, sizeof(svname), "%s-OST0001", fsname);
		if (mount_lustre_target(dataset, "/mnt/ost1", svname, 0) < 0)
			return 1;
	}

	/*
	 * Step 4: Mount Lustre client to the local servers via loopback.
	 *
	 * The device string format for Lustre client mount is:
	 *   <mgs_nid>:/<fsname>
	 */
	snprintf(client_dev, sizeof(client_dev), "0@lo:/%s", fsname);
	rc = snprintf(mount_data, sizeof(mount_data),
		      "network=lo,device=%s", client_dev);
	if (rc < 0 || (size_t)rc >= sizeof(mount_data)) {
		kmsg_log(KMSG_ERR, "client mount data too long\n");
		return 1;
	}

	kmsg_log(KMSG_INFO, "mounting lustre client %s on %s\n",
		 client_dev, MOUNTPOINT);

	if (mount(client_dev, MOUNTPOINT, "lustre", 0, mount_data) < 0) {
		kmsg_log(KMSG_ERR, "client mount failed: %s\n", strerror(errno));
		return 1;
	}

	kmsg_log(KMSG_INFO, "mounted successfully, switching root\n");

	/*
	 * The initial ramfs cannot be pivot_root()'d.  Instead, move the new
	 * root on top of "/" and chroot into it (switch_root(8) semantics).
	 */
	if (chdir(MOUNTPOINT) < 0) {
		kmsg_log(KMSG_ERR, "chdir %s: %s\n", MOUNTPOINT, strerror(errno));
		return 1;
	}

	if (mount(".", "/", NULL, MS_MOVE, NULL) < 0) {
		kmsg_log(KMSG_ERR, "mount --move: %s\n", strerror(errno));
		return 1;
	}

	if (chroot(".") < 0) {
		kmsg_log(KMSG_ERR, "chroot: %s\n", strerror(errno));
		return 1;
	}

	if (chdir("/") < 0) {
		kmsg_log(KMSG_ERR, "chdir /: %s\n", strerror(errno));
		return 1;
	}

	kmsg_log(KMSG_INFO, "switch_root done\n");

	if (mount("tmpfs", "/run", "tmpfs",
		  MS_NODEV | MS_NOSUID | MS_STRICTATIME,
		  "mode=0755") < 0) {
		kmsg_log(KMSG_ERR, "mount /run: %s\n", strerror(errno));
		return 1;
	}

	execl("/sbin/init", "init", NULL);
	execl("/init", "init", NULL);
	kmsg_log(KMSG_ERR, "exec init: %s\n", strerror(errno));
	return 1;
}
