// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE

/*
 * init-common - /dev/kmsg logging and kernel module loading shared between
 * the initramfs /init and the mount.lustreroot mount helper.
 */

#include "init-common.h"

#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <unistd.h>

#define KMSG_PATH	"/dev/kmsg"

static int kmsg_fd = -1;
static const char *kmsg_prog = "init";
static int log_to_stdout;

void kmsg_open(const char *prog)
{
	if (prog)
		kmsg_prog = prog;
	kmsg_fd = open(KMSG_PATH, O_WRONLY | O_CLOEXEC);
}

void kmsg_open_stdout(const char *prog)
{
	if (prog)
		kmsg_prog = prog;
	log_to_stdout = 1;
}

void kmsg_log(int level, const char *fmt, ...)
{
	char buf[512];
	va_list ap;
	int len;

	if (log_to_stdout)
		len = snprintf(buf, sizeof(buf), "%s: ", kmsg_prog);
	else if (kmsg_fd < 0)
		return;
	else
		len = snprintf(buf, sizeof(buf), "<%d>%s: ", level, kmsg_prog);

	if (len < 0 || len >= (int)sizeof(buf))
		return;

	va_start(ap, fmt);
	vsnprintf(buf + len, sizeof(buf) - len, fmt, ap);
	va_end(ap);

	buf[sizeof(buf) - 1] = '\0';

	if (log_to_stdout) {
		fputs(buf, stdout);
		fflush(stdout);
	} else {
		write(kmsg_fd, buf, strlen(buf));
	}
}

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

static int load_module_file(const char *path);

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

	if (!g_walk_found)
		return -1;

	snprintf(pathbuf, pathbuf_size, "%s", g_walk_result);
	return 0;
}

int load_one_module(const char *modname, const char *release)
{
	char relpath[RELPATH_MAX];
	char abspath[512];

	if (find_relpath_for_modname(modname, release,
				     relpath, sizeof(relpath)) == 0)
		return load_relpath_recursive(relpath, release);

	if (find_module_file_walk(modname, release,
				  abspath, sizeof(abspath)) == 0)
		return load_module_file(abspath);

	return -1;
}
