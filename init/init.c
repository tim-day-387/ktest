// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

/*
 * init - the ktest boot binary, installed in the initramfs as
 * /sbin/ktest-init.  /init (a bash script, see init/init)
 * always drops into a shell; running `boot` there makes /init exec this
 * binary as PID 1.  Runs in two modes selected by the cmdline - the one
 * `boot` staged in /run/ktest-cmdline when present, else /proc/cmdline:
 *
 *   Standard root: parse root= (and optional rootfstype=), mount that block
 *   device on /newroot, switch_root into it.
 *
 *   Lustre root: when lustreroot=<pool>,device=<path>[,fsname=<name>] is
 *   present, import the ZFS pool from the boot device with zpool(8), hand
 *   off to mount.lustreroot to bring up the local servers (MGS/MDT + OSTs)
 *   and mount the client on /newroot, then switch_root.
 *
 * Build (standalone, outside kernel tree):
 *   cc -Wall -static -o init init.c init-common.c
 */

#include "init-common.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/loop.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
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
/* zpool(8) from the initramfs image's zfsutils-linux. */
#define ZPOOL			"/sbin/zpool"

/*
 * The NVIDIA userland payload mk_initramfs bundles next to the NVIDIA
 * modules (tools/nvidia-userland-payload.sh via build_nvidia in qlkbuild),
 * where it is loop-mounted, and where the overlay upper/work dirs go.  All
 * relative to the new root, under the preserved initramfs copy so they
 * survive switch_root.
 */
#define NVIDIA_PAYLOAD	INITRAMFS_SAVE "/lib/nvidia/userland.squashfs"
#define NVIDIA_MNT	INITRAMFS_SAVE "/nvidia"
#define NVIDIA_OVERLAY	INITRAMFS_SAVE "/nvidia-overlay"
/* Manifest at the payload root: one directory per line, relative to /. */
#define NVIDIA_MANIFEST	".ktest-overlay"

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
 * run_cmd - fork, exec @argv[0] (an absolute path) and wait for it
 *
 * Returns 0 if the child exited successfully, -1 otherwise.
 */
static int run_cmd(char *const argv[])
{
	pid_t pid;
	int status;

	pid = fork();
	if (pid < 0) {
		kmsg_log(KMSG_ERR, "fork for %s: %s\n", argv[0],
			 strerror(errno));
		return -1;
	}

	if (pid == 0) {
		execv(argv[0], argv);
		kmsg_log(KMSG_ERR, "exec %s: %s\n", argv[0], strerror(errno));
		_exit(127);
	}

	if (waitpid(pid, &status, 0) < 0) {
		kmsg_log(KMSG_ERR, "waitpid %s: %s\n", argv[0],
			 strerror(errno));
		return -1;
	}

	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		kmsg_log(KMSG_ERR, "%s failed (status %d)\n", argv[0],
			 status);
		return -1;
	}

	return 0;
}

/*
 * zpool_import - import the ZFS pool backing the Lustre root
 *
 * Runs `zpool import -N -f -d @device @pool`.  -d points zpool at the
 * boot device alone (no /dev scan), -N skips mounting datasets (the
 * Lustre targets have mountpoint=none anyway), and -f covers the pool
 * having last been imported by the host or a VM that never exported it.
 * Returns 0 on success, -1 otherwise.
 */
static int zpool_import(const char *pool, const char *device)
{
	char *const argv[] = {
		ZPOOL, "import", "-N", "-f", "-d", (char *)device,
		(char *)pool, NULL,
	};

	return run_cmd(argv);
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
	char *const argv[] = {
		MOUNT_LUSTREROOT, (char *)fsname, (char *)pool, (char *)path,
		NULL,
	};

	/* Tell the helper it was launched by /init so it logs to /dev/kmsg
	 * rather than stdout. */
	setenv("MOUNT_LUSTREROOT_FROM_INIT", "1", 1);
	return run_cmd(argv);
}

static int copy_file(const char *src, const char *dst, mode_t mode)
{
	char buf[65536];
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
		ssize_t off = 0;

		while (off < nr) {
			ssize_t nw = write(dfd, buf + off, nr - off);

			if (nw < 0) {
				close(sfd);
				close(dfd);
				return -1;
			}
			if (nw == 0) {
				/* No progress and no error: the fs is full. */
				errno = ENOSPC;
				close(sfd);
				close(dfd);
				return -1;
			}
			off += nw;
		}
	}

	close(sfd);
	if (close(dfd) < 0)
		return -1;
	return (nr < 0) ? -1 : 0;
}

/*
 * Hard links seen so far by copy_tree: the source inode and the path of the
 * copy already made for it.  Multiply-linked files are copied once and the
 * remaining names become link(2)s to that copy, so a multicall binary
 * installed under many names (Ubuntu 26.04's Rust coreutils: one ~11MB
 * binary, ~110 names) costs its size once rather than ~110 times.  Without
 * this the copied initramfs is ~3.5x the size of the cpio and overflows the
 * destination tmpfs on small VMs.
 */
struct link_entry {
	dev_t dev;
	ino_t ino;
	char *dst;
};

struct link_table {
	struct link_entry *ents;
	size_t n, cap;
};

static const char *link_table_find(const struct link_table *tab,
				   const struct stat *st)
{
	size_t i;

	for (i = 0; i < tab->n; i++)
		if (tab->ents[i].dev == st->st_dev &&
		    tab->ents[i].ino == st->st_ino)
			return tab->ents[i].dst;

	return NULL;
}

static int link_table_add(struct link_table *tab, const struct stat *st,
			  const char *dst)
{
	struct link_entry *ent;

	if (tab->n == tab->cap) {
		size_t cap = tab->cap ? tab->cap * 2 : 64;
		struct link_entry *ents;

		ents = realloc(tab->ents, cap * sizeof(*ents));
		if (!ents)
			return -1;
		tab->ents = ents;
		tab->cap = cap;
	}

	ent = &tab->ents[tab->n];
	ent->dst = strdup(dst);
	if (!ent->dst)
		return -1;
	ent->dev = st->st_dev;
	ent->ino = st->st_ino;
	tab->n++;

	return 0;
}

static void link_table_free(struct link_table *tab)
{
	size_t i;

	for (i = 0; i < tab->n; i++)
		free(tab->ents[i].dst);
	free(tab->ents);
	tab->ents = NULL;
	tab->n = tab->cap = 0;
}

static int copy_tree_rec(const char *src, const char *dst, dev_t dev,
			 struct link_table *links)
{
	char ssub[512], dsub[512];
	struct dirent *e;
	struct stat st;
	int ret = 0;
	DIR *d;

	d = opendir(src);
	if (!d) {
		kmsg_log(KMSG_ERR, "opendir %s: %s\n", src, strerror(errno));
		return -1;
	}

	while (ret == 0 && (e = readdir(d)) != NULL) {
		if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
			continue;

		if (snprintf(ssub, sizeof(ssub), "%s/%s", src, e->d_name) >=
		    (int)sizeof(ssub) ||
		    snprintf(dsub, sizeof(dsub), "%s/%s", dst, e->d_name) >=
		    (int)sizeof(dsub)) {
			kmsg_log(KMSG_ERR, "path too long: %s/%s\n", src,
				 e->d_name);
			ret = -1;
			break;
		}

		if (lstat(ssub, &st) < 0) {
			/* Vanished under us; nothing to copy. */
			if (errno == ENOENT)
				continue;
			kmsg_log(KMSG_ERR, "lstat %s: %s\n", ssub,
				 strerror(errno));
			ret = -1;
			break;
		}

		if (S_ISDIR(st.st_mode)) {
			if (mkdir(dsub, st.st_mode & 0777) < 0 &&
			    errno != EEXIST) {
				kmsg_log(KMSG_ERR, "mkdir %s: %s\n", dsub,
					 strerror(errno));
				ret = -1;
				break;
			}
			if (st.st_dev == dev)
				ret = copy_tree_rec(ssub, dsub, dev, links);
		} else if (S_ISREG(st.st_mode)) {
			const char *existing = NULL;

			if (st.st_nlink > 1)
				existing = link_table_find(links, &st);

			if (existing) {
				if (link(existing, dsub) < 0) {
					kmsg_log(KMSG_ERR, "link %s -> %s: %s\n",
						 dsub, existing,
						 strerror(errno));
					ret = -1;
				}
				continue;
			}

			if (copy_file(ssub, dsub, st.st_mode & 0777) < 0) {
				kmsg_log(KMSG_ERR, "copy %s: %s\n", ssub,
					 strerror(errno));
				ret = -1;
				break;
			}

			if (st.st_nlink > 1 &&
			    link_table_add(links, &st, dsub) < 0) {
				kmsg_log(KMSG_ERR, "link table: %s\n",
					 strerror(errno));
				ret = -1;
				break;
			}
		} else if (S_ISLNK(st.st_mode)) {
			char lbuf[512];
			ssize_t llen = readlink(ssub, lbuf, sizeof(lbuf) - 1);

			if (llen < 0) {
				kmsg_log(KMSG_ERR, "readlink %s: %s\n", ssub,
					 strerror(errno));
				ret = -1;
				break;
			}
			lbuf[llen] = '\0';
			if (symlink(lbuf, dsub) < 0) {
				kmsg_log(KMSG_ERR, "symlink %s: %s\n", dsub,
					 strerror(errno));
				ret = -1;
				break;
			}
		}
	}

	closedir(d);
	return ret;
}

/*
 * copy_tree - recursively copy src into dst, staying on filesystem @dev
 *
 * Only entries residing on device @dev are descended into; a directory on a
 * different device is a mount point (e.g. /proc, /sys, /dev, /newroot, or the
 * destination tmpfs when copying "/"), so it is recreated empty as a mount
 * target but not crossed.  Pass the st_dev of @src to copy everything.
 *
 * Hard links among regular files are preserved (see struct link_table).
 * Any failure to create an entry (typically ENOSPC on the destination) is
 * logged with the path and aborts the copy with -1: a silently truncated
 * tree would later surface as baffling "module not found" errors from
 * modprobe(8), since an empty modules.dep.bin hides every module.
 */
static int copy_tree(const char *src, const char *dst, dev_t dev)
{
	struct link_table links = { 0 };
	int ret;

	ret = copy_tree_rec(src, dst, dev, &links);
	link_table_free(&links);

	return ret;
}

/*
 * mkdir_p - create @path and any missing parents, mode 0755
 */
static int mkdir_p(const char *path)
{
	char buf[PATH_MAX];
	char *p;

	if (strlen(path) >= sizeof(buf)) {
		errno = ENAMETOOLONG;
		return -1;
	}
	strcpy(buf, path);

	for (p = buf + 1; *p; p++) {
		if (*p != '/')
			continue;
		*p = '\0';
		if (mkdir(buf, 0755) < 0 && errno != EEXIST)
			return -1;
		*p = '/';
	}
	if (mkdir(buf, 0755) < 0 && errno != EEXIST)
		return -1;
	return 0;
}

/*
 * loop_attach - attach @file read-only to a free loop device
 *
 * Returns an open fd on the loop device, whose path is written to @dev, or
 * -1 after logging.  The device autoclears, so it detaches once nothing
 * holds it open any more: the caller must mount it before closing the fd.
 */
static int loop_attach(const char *file, char *dev, size_t devlen)
{
	struct loop_info64 info;
	int ctl, ffd, lfd = -1, n, i;

	ctl = open("/dev/loop-control", O_RDWR | O_CLOEXEC);
	if (ctl < 0) {
		kmsg_log(KMSG_ERR, "open /dev/loop-control: %s\n",
			 strerror(errno));
		return -1;
	}
	n = ioctl(ctl, LOOP_CTL_GET_FREE);
	close(ctl);
	if (n < 0) {
		kmsg_log(KMSG_ERR, "LOOP_CTL_GET_FREE: %s\n", strerror(errno));
		return -1;
	}
	snprintf(dev, devlen, "/dev/loop%d", n);

	/* devtmpfs creates the node a moment after the device appears */
	for (i = 0; i < 50; i++) {
		lfd = open(dev, O_RDONLY | O_CLOEXEC);
		if (lfd >= 0 || errno != ENOENT)
			break;
		usleep(100000);
	}
	if (lfd < 0) {
		kmsg_log(KMSG_ERR, "open %s: %s\n", dev, strerror(errno));
		return -1;
	}

	ffd = open(file, O_RDONLY | O_CLOEXEC);
	if (ffd < 0) {
		kmsg_log(KMSG_ERR, "open %s: %s\n", file, strerror(errno));
		close(lfd);
		return -1;
	}

	/* A read-only backing fd makes the loop device read-only. */
	if (ioctl(lfd, LOOP_SET_FD, ffd) < 0) {
		kmsg_log(KMSG_ERR, "LOOP_SET_FD %s: %s\n", dev, strerror(errno));
		close(ffd);
		close(lfd);
		return -1;
	}
	close(ffd);

	memset(&info, 0, sizeof(info));
	info.lo_flags = LO_FLAGS_AUTOCLEAR;
	strncpy((char *)info.lo_file_name, file, LO_NAME_SIZE - 1);
	if (ioctl(lfd, LOOP_SET_STATUS64, &info) < 0) {
		kmsg_log(KMSG_ERR, "LOOP_SET_STATUS64 %s: %s\n", dev,
			 strerror(errno));
		ioctl(lfd, LOOP_CLR_FD, 0);
		close(lfd);
		return -1;
	}

	return lfd;
}

/*
 * overlay_nvidia_userland - lay the bundled NVIDIA userland over the new root
 *
 * The NVIDIA kernel modules only talk to userland of the exact same driver
 * release, so instead of baking a userland into every root image,
 * mk_initramfs bundles the one matching the modules it packed, as a
 * squashfs (NVIDIA_PAYLOAD).  Loop-mount it out of the preserved initramfs
 * copy and, for each directory its manifest names (the 64- and 32-bit
 * multiarch lib dirs and /usr/lib32, /usr/bin, the Xorg module dirs, the
 * glvnd/Vulkan/OpenCL vendor JSON dirs, ...), mount an overlay on the same
 * path in the new root with the payload stacked on top of the root's own
 * contents.  A tmpfs upperdir keeps the directories writable for the booted
 * system (writes there don't persist, like /lib/modules).  Directories the
 * root lacks get the payload alone as lower layer.  The mounts sit under
 * /newroot and ride along with switch_root_and_exec()'s move.  A no-op when
 * no payload was bundled, and a logged no-op for any directory whose
 * overlay fails, so a broken payload costs the GPU, not the boot.
 */
static void overlay_nvidia_userland(void)
{
	char dev[32], line[PATH_MAX];
	char lower[PATH_MAX], target[PATH_MAX];
	char upper[PATH_MAX], work[PATH_MAX];
	char opts[4 * PATH_MAX + 64];
	struct stat st;
	FILE *f;
	int lfd, n = 0, done = 0;

	if (access(MOUNTPOINT NVIDIA_PAYLOAD, F_OK) != 0)
		return;

	lfd = loop_attach(MOUNTPOINT NVIDIA_PAYLOAD, dev, sizeof(dev));
	if (lfd < 0)
		return;

	mkdir(MOUNTPOINT NVIDIA_MNT, 0755);
	if (mount(dev, MOUNTPOINT NVIDIA_MNT, "squashfs", MS_RDONLY, NULL) < 0) {
		kmsg_log(KMSG_ERR, "mount NVIDIA userland payload: %s\n",
			 strerror(errno));
		ioctl(lfd, LOOP_CLR_FD, 0);
		close(lfd);
		return;
	}
	close(lfd);

	f = fopen(MOUNTPOINT NVIDIA_MNT "/" NVIDIA_MANIFEST, "r");
	if (!f) {
		kmsg_log(KMSG_ERR, "NVIDIA userland payload has no %s: %s\n",
			 NVIDIA_MANIFEST, strerror(errno));
		return;
	}

	while (fgets(line, sizeof(line), f)) {
		int len = (int)strcspn(line, "\n");
		int r;

		line[len] = '\0';
		/* Relative, no traversal, nothing the overlay option parser
		 * would need escaping. */
		if (!len || line[0] == '/' || line[len - 1] == '/' ||
		    strstr(line, "..") || strpbrk(line, ":,\\")) {
			kmsg_log(KMSG_ERR, "NVIDIA overlay: bad manifest entry '%s'\n",
				 line);
			continue;
		}

		r = snprintf(lower, sizeof(lower), MOUNTPOINT NVIDIA_MNT "/%s", line);
		if (r < 0 || r >= (int)sizeof(lower))
			continue;
		r = snprintf(target, sizeof(target), MOUNTPOINT "/%s", line);
		if (r < 0 || r >= (int)sizeof(target))
			continue;
		snprintf(upper, sizeof(upper), MOUNTPOINT NVIDIA_OVERLAY "/%d/upper", n);
		snprintf(work, sizeof(work), MOUNTPOINT NVIDIA_OVERLAY "/%d/work", n);
		n++;

		if (mkdir_p(upper) < 0 || mkdir_p(work) < 0) {
			kmsg_log(KMSG_ERR, "NVIDIA overlay %s: mkdir upper/work: %s\n",
				 line, strerror(errno));
			continue;
		}

		if (stat(target, &st) == 0 && S_ISDIR(st.st_mode)) {
			snprintf(opts, sizeof(opts),
				 "lowerdir=%s:%s,upperdir=%s,workdir=%s",
				 lower, target, upper, work);
		} else {
			if (mkdir_p(target) < 0) {
				kmsg_log(KMSG_ERR, "NVIDIA overlay %s: mkdir: %s\n",
					 line, strerror(errno));
				continue;
			}
			snprintf(opts, sizeof(opts),
				 "lowerdir=%s,upperdir=%s,workdir=%s",
				 lower, upper, work);
		}

		if (mount("overlay", target, "overlay", 0, opts) < 0) {
			kmsg_log(KMSG_ERR, "NVIDIA overlay %s: %s\n", line,
				 strerror(errno));
			continue;
		}
		done++;
	}
	fclose(f);

	kmsg_log(KMSG_INFO, "overlaid NVIDIA userland on %d/%d directories\n",
		 done, n);
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
 * kernel firmware loader finds the bundled blobs.  A bundled NVIDIA userland
 * is then overlaid from the same copy (overlay_nvidia_userland).  These
 * submounts ride along when switch_root_and_exec() moves /newroot onto /;
 * the later /run remount does not shadow /init.initramfs.  A no-op if the
 * initramfs bundled no modules.
 *
 * The tmpfs has no explicit size, so it is capped at half of RAM.  If the
 * copy does not fit, copy_tree() logs the failing path and the copy is
 * abandoned without bind-mounting the partial tree.
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

	overlay_nvidia_userland();
}

/*
 * install_modparams_to_newroot - propagate module parameters to the new root
 *
 * /etc/modparams.conf (see ktest's conf/modparams.conf) uses modprobe.d(5)
 * "options" syntax, so installing it as a modprobe.d file makes modules
 * loaded after switch_root via modprobe(8) pick up the same parameters the
 * initramfs's modprobe.d applied before the handoff.
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
 * install_udev_rules - stage ktest udev rule overrides in /run
 *
 * udevd reads /run/udev/rules.d between /usr/lib and /etc; a 99- prefix
 * sorts after the distro's 50-udev-default.rules, so its assignments win.
 * Staging the rules here ships them with the UKI without touching the
 * root filesystem: systemd leaves an already-mounted /run in place, so
 * the file survives into the booted system.
 *
 * The kvm rule undoes Ubuntu's MODE="0660" on /dev/kvm.  With KVM built
 * into the kernel there is no module-load uevent; static_node makes udevd
 * apply the mode to the existing devtmpfs node at daemon startup.
 */
static void install_udev_rules(void)
{
	static const char kvm_rule[] =
		"KERNEL==\"kvm\", MODE=\"0666\", OPTIONS+=\"static_node=kvm\"\n";
	ssize_t len = sizeof(kvm_rule) - 1;
	int fd;

	mkdir("/run/udev", 0755);
	mkdir("/run/udev/rules.d", 0755);

	fd = open("/run/udev/rules.d/99-ktest-kvm.rules",
		  O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
	if (fd < 0) {
		kmsg_log(KMSG_ERR, "open 99-ktest-kvm.rules: %s\n",
			 strerror(errno));
		return;
	}

	if (write(fd, kvm_rule, len) != len)
		kmsg_log(KMSG_ERR, "write 99-ktest-kvm.rules: %s\n",
			 strerror(errno));
	close(fd);
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

	install_udev_rules();

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
	 * Import the ZFS pool from the device before mounting any targets;
	 * without this the OSD can't open the datasets.  root_image creates
	 * the pool on a loop device over the image, so the label's leaf vdev
	 * is already type=disk and zpool resolves the stale /dev/loopN path
	 * by GUID on its own.
	 */
	if (zpool_import(pool, device) < 0)
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
	 * diagnostics (ZFS import, per-target mounts) and the
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
	 * Modules are not loaded here: /init (init/init) loads
	 * everything with modprobe(8) before the shell or any handoff, so
	 * both the standard root's block driver (e.g. nvme) and the lustre
	 * path's ZFS/Lustre stack are already in place.
	 */
	if (find_cmdline_arg(cmdline, "lustreroot"))
		lustre_main(cmdline);
	else
		standard_main(cmdline);

	/* Boot path failed (success paths exec into the new root) */
	exec_initramfs_shell();
	return 1;
}
