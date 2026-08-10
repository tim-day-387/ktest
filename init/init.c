// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

/*
 * init - the ktest boot binary, installed in the initramfs as
 * /sbin/ktest-init.  /init (a bash script, see init/initramfs-init.sh)
 * always drops into a shell; running `boot` there makes /init exec this
 * binary as PID 1.  Runs in two modes selected by the cmdline - the one
 * `boot` staged in /run/ktest-cmdline when present, else /proc/cmdline:
 *
 *   Standard root: parse root= (and optional rootfstype=), mount that block
 *   device on /newroot, switch_root into it.
 *
 *   Lustre root: when lustreroot=<pool>,device=<path>[,fsname=<name>] is
 *   present, load ZFS+Lustre modules, import the ZFS pool from the boot
 *   device, hand off to mount.lustreroot to bring up the local servers
 *   (MGS/MDT + OSTs) and mount the client on /newroot, then switch_root.
 *
 * Build (standalone, outside kernel tree):
 *   cc -Wall -static -o init init.c init-common.c zfs-import.c
 */

#include "init-common.h"
#include "zfs-import.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>

#define MOUNTPOINT	"/newroot"
/* Where the whole initramfs is preserved on the new root across switch_root. */
#define INITRAMFS_SAVE	"/init.initramfs"
#define CMDLINE_PATH	"/proc/cmdline"
/* Cmdline assembled by the initramfs `boot` script (defaults plus its
 * -d/-f/-m overrides); preferred over CMDLINE_PATH and consumed on read. */
#define CMDLINE_STAGED_PATH	"/run/ktest-cmdline"
#define CMDLINE_MAX	4096

/* Path to the Lustre mount helper bundled alongside /init in the initramfs. */
#define MOUNT_LUSTREROOT	"/sbin/mount.lustreroot"

/*
 * load_modules - load all required ZFS and Lustre modules
 *
 * For each entry, resolves its path via modules.dep, then recursively loads
 * all transitive dependencies (depth-first) before loading the module itself.
 * Returns 0 if all modules loaded successfully, -1 on the first failure.
 */
static int load_modules(void)
{
	static const char * const modules[] = {
		/* NVMe block device */
		"nvme_core",
		"nvme",
		/* ZFS */
		"zfs",
		/* Lustre networking */
		"lnet",
		"ksocklnd",
		/* Lustre client and OSD */
		"lustre",
		"osd_zfs",
		/* Hardware drivers */
		"iwlwifi",	/* laptop Wi-Fi (Intel) */
		"iwlmvm",
		"mt7921e",	/* desktop Wi-Fi (MediaTek MT7921K/RZ608) */
		"i915",
		"nouveau",
		NULL,
	};
	struct utsname uts;
	int ret = 0;
	int i;

	if (uname(&uts) < 0) {
		kmsg_log(KMSG_ERR, "uname: %s\n", strerror(errno));
		return -1;
	}

	for (i = 0; modules[i]; i++) {
		kmsg_log(KMSG_INFO, "loading module %s\n", modules[i]);
		if (load_one_module(modules[i], uts.release) < 0) {
			kmsg_log(KMSG_ERR, "failed to load %s\n", modules[i]);
			ret = -1;
		}
	}
	return ret;
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
 * run_mount_lustreroot - exec the mount.lustreroot helper and wait for it
 *
 * Brings up the local Lustre servers and mounts the client on @path by
 * running the bundled mount.lustreroot tool as a child process.
 * Returns 0 if the helper exited successfully, -1 otherwise.
 */
static int run_mount_lustreroot(const char *fsname, const char *pool,
				const char *path)
{
	pid_t pid;
	int status;

	pid = fork();
	if (pid < 0) {
		kmsg_log(KMSG_ERR, "fork for mount.lustreroot: %s\n",
			 strerror(errno));
		return -1;
	}

	if (pid == 0) {
		/* Tell the helper it was launched by /init so it logs to
		 * /dev/kmsg rather than stdout. */
		setenv("MOUNT_LUSTREROOT_FROM_INIT", "1", 1);
		execl(MOUNT_LUSTREROOT, "mount.lustreroot",
		      fsname, pool, path, (char *)NULL);
		kmsg_log(KMSG_ERR, "exec %s: %s\n",
			 MOUNT_LUSTREROOT, strerror(errno));
		_exit(127);
	}

	if (waitpid(pid, &status, 0) < 0) {
		kmsg_log(KMSG_ERR, "waitpid mount.lustreroot: %s\n",
			 strerror(errno));
		return -1;
	}

	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		kmsg_log(KMSG_ERR, "mount.lustreroot failed (status %d)\n",
			 status);
		return -1;
	}

	return 0;
}

static int copy_file(const char *src, const char *dst, mode_t mode)
{
	char buf[4096];
	ssize_t nr;
	int sfd, dfd;

	sfd = open(src, O_RDONLY | O_CLOEXEC);
	if (sfd < 0)
		return -1;

	dfd = open(dst, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, mode);
	if (dfd < 0) {
		close(sfd);
		return -1;
	}

	while ((nr = read(sfd, buf, sizeof(buf))) > 0) {
		if (write(dfd, buf, nr) != nr) {
			close(sfd);
			close(dfd);
			return -1;
		}
	}

	close(sfd);
	close(dfd);
	return (nr < 0) ? -1 : 0;
}

/*
 * copy_tree - recursively copy src into dst, staying on filesystem @dev
 *
 * Only entries residing on device @dev are descended into; a directory on a
 * different device is a mount point (e.g. /proc, /sys, /dev, /newroot, or the
 * destination tmpfs when copying "/"), so it is recreated empty as a mount
 * target but not crossed.  Pass the st_dev of @src to copy everything.
 */
static int copy_tree(const char *src, const char *dst, dev_t dev)
{
	char ssub[512], dsub[512];
	struct dirent *e;
	struct stat st;
	DIR *d;

	d = opendir(src);
	if (!d)
		return -1;

	while ((e = readdir(d)) != NULL) {
		if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
			continue;

		snprintf(ssub, sizeof(ssub), "%s/%s", src, e->d_name);
		snprintf(dsub, sizeof(dsub), "%s/%s", dst, e->d_name);

		if (lstat(ssub, &st) < 0)
			continue;

		if (S_ISDIR(st.st_mode)) {
			mkdir(dsub, st.st_mode & 0777);
			if (st.st_dev == dev)
				copy_tree(ssub, dsub, dev);
		} else if (S_ISREG(st.st_mode)) {
			copy_file(ssub, dsub, st.st_mode & 0777);
		} else if (S_ISLNK(st.st_mode)) {
			char lbuf[512];
			ssize_t llen = readlink(ssub, lbuf, sizeof(lbuf) - 1);

			if (llen >= 0) {
				lbuf[llen] = '\0';
				symlink(lbuf, dsub);
			}
		}
	}

	closedir(d);
	return 0;
}

/*
 * copy_initramfs_to_newroot - preserve the whole initramfs after switch_root
 *
 * switch_root discards the initramfs, but we want both its freshly built
 * kernel modules (lustre/zfs, under /lib/modules/<release>) and its bundled
 * userspace tools (busybox, mount.lustreroot, the zfs utilities) to remain
 * available in the booted system.  Mount a tmpfs at /newroot/init.initramfs
 * and copy the entire initramfs tree into it (copy_tree stays on the initramfs
 * filesystem, so mounted pseudo-filesystems and /newroot itself are skipped),
 * then bind /newroot/init.initramfs/lib/modules onto /newroot/lib/modules so
 * modprobe(8) finds the modules at the usual path, and likewise
 * /newroot/init.initramfs/lib/firmware onto /newroot/lib/firmware so the
 * kernel firmware loader finds the bundled blobs.  These submounts ride along
 * when switch_root_and_exec() moves /newroot onto /; the later /run remount
 * does not shadow /init.initramfs.  A no-op if the initramfs bundled no
 * modules.
 */
static void copy_initramfs_to_newroot(void)
{
	struct stat st;
	dev_t rootdev;

	if (lstat("/lib/modules", &st) < 0 || !S_ISDIR(st.st_mode))
		return;

	if (stat("/", &st) < 0) {
		kmsg_log(KMSG_ERR, "stat /: %s\n", strerror(errno));
		return;
	}
	rootdev = st.st_dev;

	mkdir(MOUNTPOINT INITRAMFS_SAVE, 0755);

	if (mount("tmpfs", MOUNTPOINT INITRAMFS_SAVE, "tmpfs", 0,
		  "mode=0755") < 0) {
		kmsg_log(KMSG_ERR, "mount tmpfs on %s: %s\n",
			 INITRAMFS_SAVE, strerror(errno));
		return;
	}

	kmsg_log(KMSG_INFO, "copying initramfs to new root\n");
	if (copy_tree("/", MOUNTPOINT INITRAMFS_SAVE, rootdev) < 0) {
		kmsg_log(KMSG_ERR, "copy initramfs failed\n");
		return;
	}
	kmsg_log(KMSG_INFO, "copied initramfs successfully\n");

	mkdir(MOUNTPOINT "/lib", 0755);
	mkdir(MOUNTPOINT "/lib/modules", 0755);

	if (mount(MOUNTPOINT INITRAMFS_SAVE "/lib/modules",
		  MOUNTPOINT "/lib/modules", NULL, MS_BIND, NULL) < 0)
		kmsg_log(KMSG_ERR, "bind /lib/modules from initramfs: %s\n",
			 strerror(errno));
	else
		kmsg_log(KMSG_INFO, "bind-mounted /lib/modules from initramfs\n");

	if (lstat("/lib/firmware", &st) == 0 && S_ISDIR(st.st_mode)) {
		mkdir(MOUNTPOINT "/lib/firmware", 0755);

		if (mount(MOUNTPOINT INITRAMFS_SAVE "/lib/firmware",
			  MOUNTPOINT "/lib/firmware", NULL, MS_BIND, NULL) < 0)
			kmsg_log(KMSG_ERR,
				 "bind /lib/firmware from initramfs: %s\n",
				 strerror(errno));
		else
			kmsg_log(KMSG_INFO,
				 "bind-mounted /lib/firmware from initramfs\n");
	}
}

/*
 * install_modparams_to_newroot - propagate module parameters to the new root
 *
 * /etc/modparams.conf (see ktest's conf/modparams.conf) uses modprobe.d(5)
 * "options" syntax, so installing it as a modprobe.d file makes modules
 * loaded after switch_root via modprobe(8) pick up the same parameters
 * /init applied in the initramfs.
 */
static void install_modparams_to_newroot(void)
{
	if (access("/etc/modparams.conf", F_OK) != 0)
		return;

	mkdir(MOUNTPOINT "/etc", 0755);
	mkdir(MOUNTPOINT "/etc/modprobe.d", 0755);

	if (copy_file("/etc/modparams.conf",
		      MOUNTPOINT "/etc/modprobe.d/ktest-modparams.conf",
		      0644) < 0)
		kmsg_log(KMSG_ERR, "install modparams.conf in new root: %s\n",
			 strerror(errno));
}

/*
 * install_setparams_to_newroot - propagate Lustre tunables to the new root
 *
 * mount.lustreroot applied /etc/setparams.conf (see ktest's
 * conf/setparams.conf) during the root mount; installing it at the same
 * path in the new root makes standalone mount.lustreroot runs after
 * switch_root (e.g. bringing up a second filesystem) apply the same
 * tunables.
 */
static void install_setparams_to_newroot(void)
{
	if (access("/etc/setparams.conf", F_OK) != 0)
		return;

	mkdir(MOUNTPOINT "/etc", 0755);

	if (copy_file("/etc/setparams.conf",
		      MOUNTPOINT "/etc/setparams.conf", 0644) < 0)
		kmsg_log(KMSG_ERR, "install setparams.conf in new root: %s\n",
			 strerror(errno));
}

/*
 * switch_root_and_exec - move /newroot on top of /, chroot in, exec init
 *
 * Returns only on failure (caller is expected to exit, panicking PID 1).
 */
static void switch_root_and_exec(void)
{
	if (chdir(MOUNTPOINT) < 0) {
		kmsg_log(KMSG_ERR, "chdir %s: %s\n", MOUNTPOINT, strerror(errno));
		return;
	}

	if (mount(".", "/", NULL, MS_MOVE, NULL) < 0) {
		kmsg_log(KMSG_ERR, "mount --move: %s\n", strerror(errno));
		return;
	}

	if (chroot(".") < 0) {
		kmsg_log(KMSG_ERR, "chroot: %s\n", strerror(errno));
		return;
	}

	if (chdir("/") < 0) {
		kmsg_log(KMSG_ERR, "chdir /: %s\n", strerror(errno));
		return;
	}

	kmsg_log(KMSG_INFO, "switch_root done\n");

	if (mount("tmpfs", "/run", "tmpfs",
		  MS_NODEV | MS_NOSUID | MS_STRICTATIME,
		  "mode=0755") < 0) {
		kmsg_log(KMSG_ERR, "mount /run: %s\n", strerror(errno));
		return;
	}

	execl("/sbin/init", "init", NULL);
	execl("/init", "init", NULL);
	kmsg_log(KMSG_ERR, "exec init: %s\n", strerror(errno));
}

/*
 * exec_initramfs_shell - drop back to the initramfs shell after a failed boot
 *
 * /init (the bash script) respawns the interactive shell; its filesystem
 * setup is idempotent, and the "ktest-boot-failed" argument makes it announce
 * the failure on the fresh shell's screen.  Returns only if the exec itself
 * fails (e.g. the root was already moved by switch_root_and_exec); the
 * caller then exits, panicking PID 1.
 */
static void exec_initramfs_shell(void)
{
	kmsg_log(KMSG_ERR, "boot failed, returning to the initramfs shell\n");
	execl("/init", "init", "ktest-boot-failed", (char *)NULL);
	kmsg_log(KMSG_ERR, "exec /init: %s\n", strerror(errno));
}

/*
 * standard_main - mount the block device named by root= and switch into it.
 *
 * Filesystem type comes from rootfstype= when present, otherwise a small
 * list of common types is tried in order.
 */
static int standard_main(char *cmdline)
{
	static const char * const fstypes[] = {
		"ext4", "xfs", "btrfs", "ext3", "ext2", NULL,
	};
	char rootspec[256] = "";
	char rootfstype[64] = "";
	char *val;
	size_t n;
	int i;

	kmsg_log(KMSG_INFO, "starting standard root setup\n");

	val = find_cmdline_arg(cmdline, "root");
	if (!val) {
		kmsg_log(KMSG_ERR, "root= not found on cmdline\n");
		return 1;
	}
	n = strcspn(val, " \t\n");
	if (n == 0 || n >= sizeof(rootspec)) {
		kmsg_log(KMSG_ERR, "invalid root= value\n");
		return 1;
	}
	memcpy(rootspec, val, n);
	rootspec[n] = '\0';

	val = find_cmdline_arg(cmdline, "rootfstype");
	if (val) {
		n = strcspn(val, " \t\n");
		if (n > 0 && n < sizeof(rootfstype)) {
			memcpy(rootfstype, val, n);
			rootfstype[n] = '\0';
		}
	}

	/* devtmpfs may take a moment to populate the root device node */
	for (i = 0; i < 50; i++) {
		if (access(rootspec, F_OK) == 0)
			break;
		usleep(100000);
	}

	/* The initramfs image ships without /newroot; the lustre path gets
	 * it from mount.lustreroot, here we make it ourselves. */
	mkdir(MOUNTPOINT, 0755);

	if (rootfstype[0]) {
		if (mount(rootspec, MOUNTPOINT, rootfstype, 0, NULL) < 0) {
			kmsg_log(KMSG_ERR, "mount %s as %s: %s\n",
				 rootspec, rootfstype, strerror(errno));
			return 1;
		}
	} else {
		int mounted = 0;

		for (i = 0; fstypes[i]; i++) {
			if (mount(rootspec, MOUNTPOINT, fstypes[i], 0, NULL) == 0) {
				kmsg_log(KMSG_INFO, "mounted %s as %s\n",
					 rootspec, fstypes[i]);
				mounted = 1;
				break;
			}
			/* A probe miss is EINVAL; anything else (ENOENT,
			 * ENODEV...) means more than a wrong guess. */
			kmsg_log(KMSG_INFO, "mount %s as %s: %s\n",
				 rootspec, fstypes[i], strerror(errno));
		}
		if (!mounted) {
			kmsg_log(KMSG_ERR, "no fstype matched %s\n", rootspec);
			return 1;
		}
	}

	copy_initramfs_to_newroot();
	install_modparams_to_newroot();
	install_setparams_to_newroot();

	switch_root_and_exec();
	return 1;
}

/*
 * lustre_main - bring up local ZFS-backed Lustre and switch into the client.
 */
static int lustre_main(char *cmdline)
{
	char pool[256], device[256], fsname[64];
	char *lustreroot_val;

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

	/* lustreroot= is guaranteed present here — the dispatcher checked. */
	lustreroot_val = find_cmdline_arg(cmdline, "lustreroot");

	/* Parse pool, device, and fsname out of the boot parameter value */
	if (parse_lustreroot(lustreroot_val, pool, sizeof(pool),
			     device, sizeof(device),
			     fsname, sizeof(fsname)) < 0)
		goto fail;

	kmsg_log(KMSG_INFO, "pool=%s device=%s fsname=%s\n",
		 pool, device, fsname);

	/*
	 * Import the ZFS pool from the device before mounting any targets.
	 * The pool was created against an image file on the host, so its label
	 * still names that file; zfs_import_pool() rewrites the leaf vdev to
	 * the runtime device.  Without this the OSD can't open the datasets.
	 */
	if (zfs_import_pool(pool, device) < 0)
		kmsg_log(KMSG_INFO, "zfs pool import failed, continuing anyway\n");

	/*
	 * Hand off to mount.lustreroot to bring up the local MGS/MDT + OSTs
	 * and mount the client on /newroot.
	 */
	if (run_mount_lustreroot(fsname, pool, MOUNTPOINT) < 0)
		goto fail;

	kmsg_log(KMSG_INFO, "mounted successfully, switching root\n");

	copy_initramfs_to_newroot();
	install_modparams_to_newroot();
	install_setparams_to_newroot();

	/*
	 * The initial ramfs cannot be pivot_root()'d.  switch_root_and_exec()
	 * implements switch_root(8) semantics and execs /sbin/init.
	 */
	switch_root_and_exec();
fail:
	return 1;
}

int main(void)
{
	char cmdline[CMDLINE_MAX];
	FILE *f;

	mount("proc",     "/proc", "proc",     0, NULL);
	mount("sysfs",    "/sys",  "sysfs",    0, NULL);
	mount("devtmpfs", "/dev",  "devtmpfs", 0, NULL);

	kmsg_open("init");

	/*
	 * Disable /dev/kmsg ratelimiting.  The boot path emits a burst of
	 * diagnostics (module loads, ZFS import, per-target mounts) and the
	 * default ratelimiter silently drops them ("N output lines suppressed
	 * due to ratelimiting"), hiding exactly the lines needed to debug a
	 * failed mount.  Safe because no cmdline printk.devkmsg= locked it.
	 */
	{
		int fd = open("/proc/sys/kernel/printk_devkmsg", O_WRONLY);

		if (fd >= 0) {
			write(fd, "on\n", 3);
			close(fd);
		}
	}

	f = fopen(CMDLINE_STAGED_PATH, "r");
	if (f) {
		kmsg_log(KMSG_INFO, "using cmdline staged by boot at %s\n",
			 CMDLINE_STAGED_PATH);
		unlink(CMDLINE_STAGED_PATH);
	} else {
		f = fopen(CMDLINE_PATH, "r");
	}
	if (!f || !fgets(cmdline, sizeof(cmdline), f)) {
		kmsg_log(KMSG_ERR, "cannot read cmdline\n");
		if (f)
			fclose(f);
		exec_initramfs_shell();
		return 1;
	}
	fclose(f);

	kmsg_log(KMSG_INFO, "cmdline: %s", cmdline);

	/*
	 * Load kernel modules before dispatching to either boot path: the
	 * standard root may live on a device whose driver (e.g. nvme) is built
	 * as a module, and the lustre path needs the ZFS/Lustre stack.
	 */
	if (load_modules() < 0)
		kmsg_log(KMSG_INFO, "module loading failed, continuing anyway\n");

	if (find_cmdline_arg(cmdline, "lustreroot"))
		lustre_main(cmdline);
	else
		standard_main(cmdline);

	/* Boot path failed (success paths exec into the new root) */
	exec_initramfs_shell();
	return 1;
}
