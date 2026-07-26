// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

/*
 * init-common - /dev/kmsg logging and kernel module loading shared between
 * the initramfs /init and the mount.lustreroot mount helper.
 */

#include "init-common.h"

#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <glob.h>
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
 * modnames_eq - compare module path/file names for equality
 *
 * '-' and '_' are interchangeable in module names and on-disk files use
 * either spelling (nvme-core.ko provides nvme_core, osd_zfs.ko provides
 * osd_zfs), so treat them as equal the way modprobe(8) does.
 */
static int modnames_eq(const char *a, const char *b)
{
	for (; *a && *b; a++, b++) {
		if (*a == *b)
			continue;
		if ((*a == '-' || *a == '_') && (*b == '-' || *b == '_'))
			continue;
		return 0;
	}
	return *a == *b;
}

/*
 * Module parameters
 *
 * MODPARAMS_PATH (installed from ktest's conf/modparams.conf by mk_initramfs)
 * uses modprobe.d(5) "options" syntax:
 *
 *   options <module> <param>=<value> [<param>=<value> ...]
 *
 * Parameters are looked up by module file name at finit_module() time, so
 * they also apply to modules loaded as dependencies (e.g. spl via zfs).
 */

#define MODPARAMS_PATH	"/etc/modparams.conf"
#define MAX_MODPARAMS	32
#define MODNAME_MAX	64
#define PARAMS_MAX	512

static struct modparam {
	char modname[MODNAME_MAX];
	char params[PARAMS_MAX];
} g_modparams[MAX_MODPARAMS];
static int g_nmodparams;
static int g_modparams_parsed;

static void modparams_add(const char *modname, const char *params)
{
	struct modparam *mp = NULL;
	int i;

	/* Repeated "options <module>" lines concatenate, like modprobe(8) */
	for (i = 0; i < g_nmodparams; i++)
		if (modnames_eq(g_modparams[i].modname, modname))
			mp = &g_modparams[i];

	if (!mp) {
		if (g_nmodparams >= MAX_MODPARAMS) {
			kmsg_log(KMSG_ERR, "%s: too many entries, ignoring %s\n",
				 MODPARAMS_PATH, modname);
			return;
		}
		mp = &g_modparams[g_nmodparams++];
		snprintf(mp->modname, sizeof(mp->modname), "%s", modname);
	}

	if (mp->params[0])
		strncat(mp->params, " ",
			sizeof(mp->params) - strlen(mp->params) - 1);
	strncat(mp->params, params,
		sizeof(mp->params) - strlen(mp->params) - 1);
}

static void modparams_parse(void)
{
	char line[1024];
	FILE *f;

	if (g_modparams_parsed)
		return;
	g_modparams_parsed = 1;

	f = fopen(MODPARAMS_PATH, "r");
	if (!f)
		return;	/* no config bundled */

	while (fgets(line, sizeof(line), f)) {
		char *keyword, *modname, *params, *save, *nl;

		nl = strchr(line, '\n');
		if (nl)
			*nl = '\0';

		keyword = strtok_r(line, " \t", &save);
		if (!keyword || keyword[0] == '#')
			continue;
		if (strcmp(keyword, "options") != 0) {
			kmsg_log(KMSG_ERR, "%s: unsupported directive %s\n",
				 MODPARAMS_PATH, keyword);
			continue;
		}

		modname = strtok_r(NULL, " \t", &save);
		if (!modname)
			continue;

		/* Rest of the line is the parameter string */
		params = save;
		while (*params == ' ' || *params == '\t')
			params++;
		if (!*params)
			continue;

		modparams_add(modname, params);
	}

	fclose(f);
}

/*
 * params_for_module_file - look up configured parameters for a .ko path
 *
 * Derives the module name from the file name (everything before the first
 * '.') and returns its configured parameter string, or "" if none.
 */
static const char *params_for_module_file(const char *path)
{
	char modname[MODNAME_MAX];
	const char *base;
	size_t n;
	int i;

	modparams_parse();

	if (!g_nmodparams)
		return "";

	base = strrchr(path, '/');
	base = base ? base + 1 : path;
	n = strcspn(base, ".");
	if (n == 0 || n >= sizeof(modname))
		return "";
	memcpy(modname, base, n);
	modname[n] = '\0';

	for (i = 0; i < g_nmodparams; i++)
		if (modnames_eq(g_modparams[i].modname, modname))
			return g_modparams[i].params;
	return "";
}

/*
 * Lustre tunables (lctl set_param style)
 *
 * SETPARAMS_PATH (installed from ktest's conf/setparams.conf by mk_initramfs)
 * holds one <param>=<value> per line using lctl set_param syntax, glob
 * wildcards included:
 *
 *   mdt.*.identity_upcall=NONE
 *
 * A parameter name maps to a file under /sys/fs/lustre or /proc/fs/lustre
 * with '.' as the path separator, which is all lctl set_param does (debugfs
 * parameters are not covered).  Parameter files only appear as their obd
 * devices are set up, so setparams_apply() is meant to be called after every
 * mount step: entries that do not match yet are skipped and picked up by a
 * later call, and re-writing an already-set value is harmless.  Entries that
 * never matched are reported by setparams_warn_unmatched().
 */

#define SETPARAMS_PATH	"/etc/setparams.conf"
#define MAX_SETPARAMS	32
#define SETPARAM_MAX	256

static struct setparam {
	char param[SETPARAM_MAX];	/* dotted lctl name, may contain globs */
	char value[SETPARAM_MAX];
	int matched;
} g_setparams[MAX_SETPARAMS];
static int g_nsetparams;
static int g_setparams_parsed;

static void setparams_parse(void)
{
	char line[1024];
	FILE *f;

	if (g_setparams_parsed)
		return;
	g_setparams_parsed = 1;

	f = fopen(SETPARAMS_PATH, "r");
	if (!f)
		return;	/* no config bundled */

	while (fgets(line, sizeof(line), f)) {
		struct setparam *sp;
		char *tok, *save, *eq;

		tok = strtok_r(line, " \t\n", &save);
		if (!tok || tok[0] == '#')
			continue;

		eq = strchr(tok, '=');
		if (!eq || eq == tok || !eq[1]) {
			kmsg_log(KMSG_ERR, "%s: malformed line \"%s\"\n",
				 SETPARAMS_PATH, tok);
			continue;
		}

		if (g_nsetparams >= MAX_SETPARAMS) {
			kmsg_log(KMSG_ERR, "%s: too many entries, ignoring %s\n",
				 SETPARAMS_PATH, tok);
			continue;
		}

		*eq = '\0';
		sp = &g_setparams[g_nsetparams++];
		snprintf(sp->param, sizeof(sp->param), "%s", tok);
		snprintf(sp->value, sizeof(sp->value), "%s", eq + 1);
	}

	fclose(f);
}

static int setparam_write_file(const char *path, const char *value)
{
	ssize_t n;
	int fd;

	fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0) {
		kmsg_log(KMSG_ERR, "open %s: %s\n", path, strerror(errno));
		return -1;
	}

	n = write(fd, value, strlen(value));
	close(fd);

	if (n < 0) {
		kmsg_log(KMSG_ERR, "write \"%s\" to %s: %s\n",
			 value, path, strerror(errno));
		return -1;
	}
	return 0;
}

void setparams_apply(void)
{
	static const char *roots[] = { "/sys/fs/lustre", "/proc/fs/lustre" };
	int i;

	setparams_parse();

	for (i = 0; i < g_nsetparams; i++) {
		struct setparam *sp = &g_setparams[i];
		char relpath[SETPARAM_MAX];
		char pattern[SETPARAM_MAX + 32];
		size_t r, j;
		char *p;

		snprintf(relpath, sizeof(relpath), "%s", sp->param);
		for (p = relpath; *p; p++)
			if (*p == '.')
				*p = '/';

		for (r = 0; r < sizeof(roots) / sizeof(roots[0]); r++) {
			glob_t gl;

			snprintf(pattern, sizeof(pattern), "%s/%s",
				 roots[r], relpath);

			if (glob(pattern, GLOB_NOSORT, NULL, &gl) != 0)
				continue;

			for (j = 0; j < gl.gl_pathc; j++)
				if (setparam_write_file(gl.gl_pathv[j],
							sp->value) == 0)
					kmsg_log(KMSG_INFO, "set_param %s=%s (%s)\n",
						 sp->param, sp->value,
						 gl.gl_pathv[j]);

			sp->matched = 1;
			globfree(&gl);
		}
	}
}

void setparams_warn_unmatched(void)
{
	int i;

	for (i = 0; i < g_nsetparams; i++)
		if (!g_setparams[i].matched)
			kmsg_log(KMSG_ERR, "%s: %s=%s matched no parameter file\n",
				 SETPARAMS_PATH, g_setparams[i].param,
				 g_setparams[i].value);
}

/*
 * find_relpath_for_modname - locate the relative path for a module by name
 *
 * Searches modules.dep for a line whose LHS ends with /<modname>.ko,
 * matching '-' and '_' interchangeably.
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
		if ((rlen >= nlen && modnames_eq(line + rlen - nlen, needle)) ||
		    modnames_eq(line, needle + 1)) {
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
	const char *params = params_for_module_file(path);
	int fd, ret;

	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		kmsg_log(KMSG_ERR, "open %s: %s\n", path, strerror(errno));
		return -1;
	}

	if (*params)
		kmsg_log(KMSG_INFO, "loading %s with params \"%s\"\n",
			 path, params);

	ret = syscall(SYS_finit_module, fd, params, 0);
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
	if (modnames_eq(path + ftwbuf->base, g_walk_needle)) {
		snprintf(g_walk_result, sizeof(g_walk_result), "%s", path);
		g_walk_found = 1;
		return 1;
	}
	return 0;
}

/*
 * find_module_file_walk - find a .ko file by walking /lib/modules/<release>/
 *
 * Matches '-' and '_' in the module name interchangeably.
 * Used as a fallback when the module is absent from modules.dep.
 * Returns 0 and writes the absolute path into @pathbuf on success, -1 if not found.
 */
static int find_module_file_walk(const char *modname, const char *release,
				 char *pathbuf, size_t pathbuf_size)
{
	char searchdir[256];
	char needle[RELPATH_MAX];

	snprintf(needle, sizeof(needle), "%s.ko", modname);

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
