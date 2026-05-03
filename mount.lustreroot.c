// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE

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

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
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

static void kmsg_log(int level, const char *fmt, ...);
static int load_module_file(const char *path);

/*
 * Module loading helpers
 *
 * modules.dep format (one entry per line):
 *   relative/path/to/foo.ko: relative/dep1.ko relative/dep2.ko ...
 * All paths are relative to /lib/modules/<release>/.
 */

#define RELPATH_MAX      256
#define DEPS_LINE_MAX    8192	/* dep lists can be long for lustre/zfs */
#define MAX_SEEN_MODULES 256

static char g_seen[MAX_SEEN_MODULES][RELPATH_MAX];
static int  g_nseen;

static int seen_relpath(const char *rel)
{
	int i;

	for (i = 0; i < g_nseen; i++)
		if (strcmp(g_seen[i], rel) == 0)
			return 1;
	return 0;
}

static void mark_seen(const char *rel)
{
	if (g_nseen < MAX_SEEN_MODULES) {
		strncpy(g_seen[g_nseen], rel, RELPATH_MAX - 1);
		g_seen[g_nseen][RELPATH_MAX - 1] = '\0';
		g_nseen++;
	}
}

/*
 * find_relpath_for_modname - locate the relative path for a module by name
 *
 * Searches modules.dep for a line whose LHS ends with /<modname>.ko.
 * Writes the relative path (without leading slash) into @relpathbuf.
 * Returns 0 on success, -1 if not found.
 */
static int find_relpath_for_modname(const char *modname, const char *release,
				    char *relpathbuf, size_t relpathbuf_size)
{
	char deppath[256];
	char line[DEPS_LINE_MAX];
	char needle[RELPATH_MAX];
	FILE *f;
	int found = 0;

	snprintf(deppath, sizeof(deppath),
		 "/lib/modules/%s/modules.dep", release);
	snprintf(needle, sizeof(needle), "/%s.ko", modname);
	for (char *p = needle; *p; p++)
		if (*p == '_') *p = '-';

	f = fopen(deppath, "r");
	if (!f) {
		kmsg_log(KMSG_ERR, "cannot open %s: %s\n",
			 deppath, strerror(errno));
		return -1;
	}

	while (fgets(line, sizeof(line), f)) {
		char *colon = strchr(line, ':');
		size_t rlen, nlen;

		if (!colon)
			continue;
		*colon = '\0';	/* isolate LHS relpath */
		rlen = strlen(line);
		nlen = strlen(needle);

		/* Match ".../<modname>.ko" or bare "<modname>.ko" */
		if ((rlen >= nlen && strcmp(line + rlen - nlen, needle) == 0) ||
		    strcmp(line, needle + 1) == 0) {
			snprintf(relpathbuf, relpathbuf_size, "%s", line);
			found = 1;
			break;
		}
	}

	fclose(f);

	if (!found)
		return -1;
	return 0;
}

/*
 * find_deps_for_relpath - retrieve the dependency list for a module relpath
 *
 * Finds the modules.dep line whose LHS equals @relpath and writes the
 * RHS (space-separated dep relpaths, may be empty) into @depsbuf.
 * Returns 0 if found, -1 if not found.
 */
static int find_deps_for_relpath(const char *relpath, const char *release,
				 char *depsbuf, size_t depsbuf_size)
{
	char deppath[256];
	char line[DEPS_LINE_MAX];
	FILE *f;
	int found = 0;

	snprintf(deppath, sizeof(deppath),
		 "/lib/modules/%s/modules.dep", release);

	f = fopen(deppath, "r");
	if (!f) {
		kmsg_log(KMSG_ERR, "cannot open %s: %s\n",
			 deppath, strerror(errno));
		return -1;
	}

	while (fgets(line, sizeof(line), f)) {
		char *colon = strchr(line, ':');
		char *deps, *nl;

		if (!colon)
			continue;
		*colon = '\0';

		if (strcmp(line, relpath) != 0)
			continue;

		/* RHS: skip leading whitespace, strip trailing newline */
		deps = colon + 1;
		while (*deps == ' ' || *deps == '\t')
			deps++;
		nl = strchr(deps, '\n');
		if (nl)
			*nl = '\0';
		snprintf(depsbuf, depsbuf_size, "%s", deps);
		found = 1;
		break;
	}

	fclose(f);
	return found ? 0 : -1;
}

/*
 * load_relpath_recursive - load a module and its dependencies depth-first
 *
 * Looks up @relpath in modules.dep to get its deps, loads each dep
 * recursively, then loads @relpath itself.  Already-seen relpaths are
 * skipped so cycles and duplicates are handled safely.
 *
 * Returns 0 on success, -1 on the first failure.
 */
static int load_relpath_recursive(const char *relpath, const char *release)
{
	char abspath[512];
	char depsbuf[DEPS_LINE_MAX];
	char *tok, *save;

	if (seen_relpath(relpath))
		return 0;
	/* Mark before recursing to break any dependency cycles */
	mark_seen(relpath);

	if (find_deps_for_relpath(relpath, release,
				  depsbuf, sizeof(depsbuf)) == 0) {
		tok = strtok_r(depsbuf, " \t", &save);
		while (tok) {
			if (load_relpath_recursive(tok, release) < 0)
				return -1;
			tok = strtok_r(NULL, " \t", &save);
		}
	}

	snprintf(abspath, sizeof(abspath),
		 "/lib/modules/%s/%s", release, relpath);
	return load_module_file(abspath);
}

/*
 * load_module_file - load a .ko file into the kernel via finit_module(2)
 *
 * Returns 0 on success.  EEXIST (already loaded) is treated as success.
 */
static int load_module_file(const char *path)
{
	int fd, ret;

	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		kmsg_log(KMSG_ERR, "open %s: %s\n", path, strerror(errno));
		return -1;
	}

	ret = syscall(SYS_finit_module, fd, "", 0);
	close(fd);

	if (ret < 0 && errno != EEXIST) {
		kmsg_log(KMSG_ERR, "finit_module %s: %s\n",
			 path, strerror(errno));
		return -1;
	}
	return 0;
}

/* State for nftw-based module search (single-threaded, so globals are fine) */
static const char *g_walk_needle;
static char g_walk_result[512];
static int g_walk_found;

static int walk_cb(const char *path, const struct stat *sb,
		   int typeflag, struct FTW *ftwbuf)
{
	(void)sb;
	if (typeflag != FTW_F)
		return 0;
	if (strcmp(path + ftwbuf->base, g_walk_needle) == 0) {
		snprintf(g_walk_result, sizeof(g_walk_result), "%s", path);
		g_walk_found = 1;
		return 1;
	}
	return 0;
}

/*
 * find_module_file_walk - find a .ko file by walking /lib/modules/<release>/
 *
 * Normalizes hyphens to underscores in the module name before searching.
 * Used as a fallback when the module is absent from modules.dep.
 * Returns 0 and writes the absolute path into @pathbuf on success, -1 if not found.
 */
static int find_module_file_walk(const char *modname, const char *release,
				 char *pathbuf, size_t pathbuf_size)
{
	char searchdir[256];
	char needle[RELPATH_MAX];

	snprintf(needle, sizeof(needle), "%s.ko", modname);
	for (char *p = needle; *p; p++)
		if (*p == '_') *p = '-';

	snprintf(searchdir, sizeof(searchdir), "/lib/modules/%s", release);

	g_walk_needle = needle;
	g_walk_found = 0;
	nftw(searchdir, walk_cb, 16, FTW_PHYS);

	if (!g_walk_found) {
		kmsg_log(KMSG_ERR, "module %s not found under %s\n",
			 modname, searchdir);
		return -1;
	}

	snprintf(pathbuf, pathbuf_size, "%s", g_walk_result);
	return 0;
}

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
		"iwlwifi",
		"iwlmvm",
		"i915",
		NULL,
	};
	struct utsname uts;
	char relpath[RELPATH_MAX];
	int i;

	if (uname(&uts) < 0) {
		kmsg_log(KMSG_ERR, "uname: %s\n", strerror(errno));
		return -1;
	}

	int ret = 0;

	for (i = 0; modules[i]; i++) {
		kmsg_log(KMSG_INFO, "loading module %s\n", modules[i]);
		if (find_relpath_for_modname(modules[i], uts.release,
					     relpath, sizeof(relpath)) == 0) {
			if (load_relpath_recursive(relpath, uts.release) < 0)
				ret = -1;
		} else {
			/* Not in modules.dep — search filesystem and load directly */
			char abspath[512];

			kmsg_log(KMSG_INFO,
				 "module %s not in modules.dep, searching filesystem\n",
				 modules[i]);
			if (find_module_file_walk(modules[i], uts.release,
						  abspath, sizeof(abspath)) < 0) {
				ret = -1;
				continue;
			}
			if (load_module_file(abspath) < 0)
				ret = -1;
		}
	}
	return ret;
}

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

static int copy_tree(const char *src, const char *dst)
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
			copy_tree(ssub, dsub);
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

	/* Load ZFS and Lustre kernel modules before anything else */
	if (load_modules() < 0)
		kmsg_log(KMSG_INFO, "module loading failed, continuing anyway\n");

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
		goto fail;
	}
	if (!fgets(cmdline, sizeof(cmdline), f)) {
		kmsg_log(KMSG_ERR, "cannot read %s: %s\n",
			 CMDLINE_PATH, strerror(errno));
		fclose(f);
		goto fail;
	}
	fclose(f);

	/* Find lustreroot= argument */
	lustreroot_val = find_cmdline_arg(cmdline, "lustreroot");
	if (!lustreroot_val) {
		kmsg_log(KMSG_ERR, "lustreroot= not found in %s\n", CMDLINE_PATH);
		goto fail;
	}

	/* Parse pool, device, and fsname out of the boot parameter value */
	if (parse_lustreroot(lustreroot_val, pool, sizeof(pool),
			     device, sizeof(device),
			     fsname, sizeof(fsname)) < 0)
		goto fail;

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
				goto fail;
			use_prefix = 0;
		}

		prefix = use_prefix ? prefix_buf : "";

		/* Step 2: Mount OST0 */
		snprintf(dataset, sizeof(dataset), "%s/%sost0", pool, prefix);
		snprintf(svname, sizeof(svname), "%s-OST0000", fsname);
		if (mount_lustre_target(dataset, "/mnt/ost0", svname, 0) < 0)
			goto fail;

		/* Step 3: Mount OST1 */
		snprintf(dataset, sizeof(dataset), "%s/%sost1", pool, prefix);
		snprintf(svname, sizeof(svname), "%s-OST0001", fsname);
		if (mount_lustre_target(dataset, "/mnt/ost1", svname, 0) < 0)
			goto fail;
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
		goto fail;
	}

	kmsg_log(KMSG_INFO, "mounting lustre client %s on %s\n",
		 client_dev, MOUNTPOINT);

	if (mount(client_dev, MOUNTPOINT, "lustre", 0, mount_data) < 0) {
		kmsg_log(KMSG_ERR, "client mount failed: %s\n", strerror(errno));
		goto fail;
	}

	kmsg_log(KMSG_INFO, "mounted successfully, switching root\n");

	/*
	 * Copy /lib/modules from the initramfs into a tmpfs mounted at
	 * /newroot/lib/modules so kernel modules remain accessible after
	 * switch_root.  The MS_MOVE below carries the tmpfs into the new root.
	 */
	mkdir(MOUNTPOINT "/lib", 0755);
	mkdir(MOUNTPOINT "/lib/modules", 0755);

	if (mount("tmpfs", MOUNTPOINT "/lib/modules", "tmpfs", 0,
		  "mode=0755") < 0) {
		kmsg_log(KMSG_ERR, "mount tmpfs on /lib/modules: %s\n",
			 strerror(errno));
	} else {
		kmsg_log(KMSG_INFO, "copying /lib/modules to new root\n");
		if (copy_tree("/lib/modules", MOUNTPOINT "/lib/modules") < 0)
			kmsg_log(KMSG_ERR, "copy /lib/modules failed\n");
		else
			kmsg_log(KMSG_INFO, "copied /lib/modules successfully\n");
	}

	/*
	 * The initial ramfs cannot be pivot_root()'d.  Instead, move the new
	 * root on top of "/" and chroot into it (switch_root(8) semantics).
	 */
	if (chdir(MOUNTPOINT) < 0) {
		kmsg_log(KMSG_ERR, "chdir %s: %s\n", MOUNTPOINT, strerror(errno));
		goto fail;
	}

	if (mount(".", "/", NULL, MS_MOVE, NULL) < 0) {
		kmsg_log(KMSG_ERR, "mount --move: %s\n", strerror(errno));
		goto fail;
	}

	if (chroot(".") < 0) {
		kmsg_log(KMSG_ERR, "chroot: %s\n", strerror(errno));
		goto fail;
	}

	if (chdir("/") < 0) {
		kmsg_log(KMSG_ERR, "chdir /: %s\n", strerror(errno));
		goto fail;
	}

	kmsg_log(KMSG_INFO, "switch_root done\n");

	if (mount("tmpfs", "/run", "tmpfs",
		  MS_NODEV | MS_NOSUID | MS_STRICTATIME,
		  "mode=0755") < 0) {
		kmsg_log(KMSG_ERR, "mount /run: %s\n", strerror(errno));
		goto fail;
	}

	execl("/sbin/init", "init", NULL);
	execl("/init", "init", NULL);
	kmsg_log(KMSG_ERR, "exec init: %s\n", strerror(errno));
fail:
	sleep(30);
	return 1;
}
