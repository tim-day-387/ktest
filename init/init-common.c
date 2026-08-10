// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE

/*
 * init-common - /dev/kmsg logging and Lustre tunables shared between the
 * initramfs /init and the mount.lustreroot mount helper.
 */

#include "init-common.h"

#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
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
