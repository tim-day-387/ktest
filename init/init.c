// SPDX-License-Identifier: GPL-2.0
#define _GNU_SOURCE

/*
 * init - ktest initramfs /init, runs in two modes selected by /proc/cmdline:
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
 *   cc -Wall -static -o init init.c init-common.c
 */

#include "init-common.h"

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
		"iwlwifi",
		"iwlmvm",
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
 * ZFS pool import
 *
 * The Lustre root image is a ZFS pool that was created on the host against an
 * image *file* (`zpool create lustre_pool <file>`); at boot those same bytes
 * are a block device (e.g. /dev/vda).  There is no zpool(8) binary in the
 * initramfs, so we import the pool by talking to /dev/zfs directly, the same
 * way libzfs does:
 *
 *   1. Read the vdev label (an XDR-packed config nvlist) from the front of the
 *      device.
 *   2. Transcode it to a native-encoded nvlist, rewriting the single leaf
 *      vdev's "path" to the runtime device and its "type" from "file" to
 *      "disk" (the pool was created on a file; it is now a disk).
 *   3. Issue ZFS_IOC_POOL_IMPORT with that config and the on-disk pool guid.
 *
 * The native nvlist encoder mirrors src/bin/lustre-ktest/zfs.rs.  Both that
 * code and this assume a little-endian host (all ktest target arches are LE).
 */

#define ZFS_DEV			"/dev/zfs"
#define ZFS_IOC_POOL_IMPORT	0x5a02	/* raw zfs_ioc_t, not _IOWR-encoded */

/* zfs_cmd_t field offsets (MAXPATHLEN=4096, MAXNAMELEN=256). */
#define ZFS_CMD_SIZE		13744
#define ZC_NAME_OFF		0
#define ZC_GUID_OFF		12592
#define ZC_NVLIST_CONF_OFF	12600
#define ZC_NVLIST_CONF_SIZE_OFF	12608

/* ZFS on-disk vdev label geometry (see sys/vdev_impl.h). */
#define VDEV_LABEL_SIZE		(256 << 10)	/* one label */
#define VDEV_PHYS_OFFSET	(16 << 10)	/* skip vl_pad1 + vl_be */
#define VDEV_PHYS_NVLIST_SIZE	(112 << 10)	/* vl_vdev_phys */

/* nvpair data_type_t values (sys/nvpair.h). */
#define DT_BOOLEAN	1
#define DT_INT64	7
#define DT_UINT64	8
#define DT_STRING	9
#define DT_UINT64_ARRAY	16
#define DT_HRTIME	18
#define DT_NVLIST	19
#define DT_NVLIST_ARRAY	20

#define NV_ENCODE_XDR	1	/* nvs_header_t.nvh_encoding */

/*
 * Growable byte buffer used to assemble a native-encoded nvlist.  Once b->err
 * is set (allocation failure) all further appends are dropped and the caller
 * checks the flag at the end.
 */
struct nvbuf {
	unsigned char *data;
	size_t len, cap;
	int err;
};

static void nvb_put(struct nvbuf *b, const void *p, size_t n)
{
	if (b->err)
		return;
	if (b->len + n > b->cap) {
		size_t ncap = b->cap ? b->cap * 2 : 256;
		unsigned char *nd;

		while (ncap < b->len + n)
			ncap *= 2;
		nd = realloc(b->data, ncap);
		if (!nd) {
			b->err = 1;
			return;
		}
		b->data = nd;
		b->cap = ncap;
	}
	memcpy(b->data + b->len, p, n);
	b->len += n;
}

static void nvb_u16(struct nvbuf *b, uint16_t v) { nvb_put(b, &v, 2); }
static void nvb_u32(struct nvbuf *b, uint32_t v) { nvb_put(b, &v, 4); }
static void nvb_u64(struct nvbuf *b, uint64_t v) { nvb_put(b, &v, 8); }

static void nvb_zero(struct nvbuf *b, size_t n)
{
	static const unsigned char z[8] = { 0 };

	while (n) {
		size_t c = n < sizeof(z) ? n : sizeof(z);

		nvb_put(b, z, c);
		n -= c;
	}
}

/* Align up to ZFS NV_ALIGN (8-byte) boundary. */
static size_t nv_align8(size_t x) { return (x + 7) & ~(size_t)7; }

/*
 * Emit a native nvpair header: nvp_size, name_sz, reserve, value_elem, type,
 * the name (null-terminated) and padding up to the value offset.
 */
static void nv_header(struct nvbuf *b, const char *name, int type,
		      int elem, size_t value_sz)
{
	size_t name_sz = strlen(name) + 1;
	size_t val_off = nv_align8(16 + name_sz);
	size_t nvp_size = val_off + nv_align8(value_sz);

	nvb_u32(b, (uint32_t)nvp_size);
	nvb_u16(b, (uint16_t)name_sz);
	nvb_u16(b, 0);
	nvb_u32(b, (uint32_t)elem);
	nvb_u32(b, (uint32_t)type);
	nvb_put(b, name, name_sz - 1);
	nvb_zero(b, 1);
	nvb_zero(b, val_off - (16 + name_sz));
}

/* An 8-byte scalar (uint64/int64/hrtime). */
static void nv_add_scalar8(struct nvbuf *b, const char *name, int type,
			   uint64_t v)
{
	nv_header(b, name, type, 1, 8);
	nvb_u64(b, v);
}

static void nv_add_u64_array(struct nvbuf *b, const char *name,
			     const uint64_t *vals, uint32_t n)
{
	uint32_t i;

	nv_header(b, name, DT_UINT64_ARRAY, n, (size_t)n * 8);
	for (i = 0; i < n; i++)
		nvb_u64(b, vals[i]);
}

static void nv_add_string(struct nvbuf *b, const char *name, const char *val)
{
	size_t str_sz = strlen(val) + 1;

	nv_header(b, name, DT_STRING, 1, str_sz);
	nvb_put(b, val, str_sz - 1);
	nvb_zero(b, 1);
	nvb_zero(b, nv_align8(str_sz) - str_sz);
}

static void nv_add_boolean(struct nvbuf *b, const char *name)
{
	nv_header(b, name, DT_BOOLEAN, 0, 0);
}

/* The embedded 24-byte nvlist_t struct that precedes a child's pairs. */
static void nv_embed_struct(struct nvbuf *b)
{
	nvb_u32(b, 0);	/* nvl_version */
	nvb_u32(b, 1);	/* nvl_nvflag = NV_UNIQUE_NAME */
	nvb_u64(b, 0);	/* nvl_priv */
	nvb_u32(b, 0);	/* nvl_flag */
	nvb_u32(b, 0);	/* nvl_pad */
}

static void nv_add_nvlist(struct nvbuf *b, const char *name, struct nvbuf *child)
{
	nv_header(b, name, DT_NVLIST, 1, 24);
	nv_embed_struct(b);
	nvb_put(b, child->data, child->len);
	if (child->err)
		b->err = 1;
	nvb_u32(b, 0);	/* child terminator (outside nvp_size) */
}

static void nv_add_nvlist_array(struct nvbuf *b, const char *name,
				struct nvbuf *children, uint32_t n)
{
	size_t val_sz = (size_t)n * 8 + (size_t)n * 24;
	uint32_t i;

	nv_header(b, name, DT_NVLIST_ARRAY, n, val_sz);
	for (i = 0; i < n; i++)
		nvb_u64(b, 0);		/* zeroed pointer slots */
	for (i = 0; i < n; i++)
		nv_embed_struct(b);	/* nvlist_t structs */
	nvb_zero(b, nv_align8(val_sz) - val_sz);
	for (i = 0; i < n; i++) {
		nvb_put(b, children[i].data, children[i].len);
		if (children[i].err)
			b->err = 1;
		nvb_u32(b, 0);		/* per-child terminator */
	}
}

/*
 * Wrap a finished pairs buffer with the nvlist header + terminator, producing a
 * standalone native-encoded nvlist.  Returns a malloc'd buffer (caller frees)
 * and its length via @out_len, or NULL on allocation failure.
 */
static unsigned char *nv_pack(struct nvbuf *pairs, size_t *out_len)
{
	struct nvbuf b = { 0 };
	static const unsigned char hdr[4] = { 0, 1, 0, 0 };  /* NATIVE, LE */

	nvb_put(&b, hdr, 4);
	nvb_u32(&b, 0);		/* nvl_version */
	nvb_u32(&b, 1);		/* nvl_nvflag = NV_UNIQUE_NAME */
	nvb_put(&b, pairs->data, pairs->len);
	nvb_u32(&b, 0);		/* outer terminator */

	if (b.err || pairs->err) {
		free(b.data);
		return NULL;
	}
	*out_len = b.len;
	return b.data;
}

/*
 * Minimal XDR reader over a bounded byte range.  XDR scalars are big-endian
 * and 4-byte aligned; out-of-bounds reads set x->err and return 0.
 */
struct xdr {
	const unsigned char *p;
	const unsigned char *end;
	int err;
};

static uint32_t xb32(struct xdr *x)
{
	uint32_t v;

	if (x->p + 4 > x->end) {
		x->err = 1;
		return 0;
	}
	v = ((uint32_t)x->p[0] << 24) | ((uint32_t)x->p[1] << 16) |
	    ((uint32_t)x->p[2] << 8) | (uint32_t)x->p[3];
	x->p += 4;
	return v;
}

static uint64_t xb64(struct xdr *x)
{
	uint64_t hi = xb32(x);
	uint64_t lo = xb32(x);

	return (hi << 32) | lo;
}

/*
 * xdr_nvlist - transcode one XDR-encoded nvlist into native form
 *
 * @x is positioned at the embedded nvlist's nvl_version field; on return it
 * sits just past the nvlist's terminating record (so the caller can transcode
 * successive elements of an nvlist array).  Native pairs are appended to @out.
 *
 * While transcoding the leaf vdev we rewrite "path" to @device and "type"
 * from "file" to "disk".  The pool guid (top-level "pool_guid") is captured
 * into *@pool_guid for the import ioctl.
 *
 * Returns 0 on success, -1 on a malformed stream or allocation failure.
 */
static int xdr_nvlist(struct xdr *x, struct nvbuf *out, int depth,
		      const char *device, uint64_t *pool_guid)
{
	(void)xb32(x);	/* nvl_version */
	(void)xb32(x);	/* nvl_nvflag */

	for (;;) {
		const unsigned char *ps = x->p;
		const unsigned char *pend, *vs;
		uint32_t enc, dec, nlen, type, nelem;
		char name[128];

		enc = xb32(x);
		dec = xb32(x);
		if (x->err)
			return -1;
		if (dec == 0)		/* terminating record */
			return 0;

		pend = ps + enc;
		if (pend > x->end || pend < ps)
			return -1;

		nlen = xb32(x);
		if (nlen >= sizeof(name) || x->p + ((nlen + 3) & ~3u) > pend) {
			x->p = pend;	/* unparseable name; skip pair */
			continue;
		}
		memcpy(name, x->p, nlen);
		name[nlen] = '\0';
		x->p += (nlen + 3) & ~3u;

		type = xb32(x);
		nelem = xb32(x);
		if (x->err)
			return -1;
		vs = x->p;

		switch (type) {
		case DT_BOOLEAN:
			nv_add_boolean(out, name);
			break;
		case DT_UINT64:
		case DT_INT64:
		case DT_HRTIME: {
			struct xdr v = { vs, pend, 0 };
			uint64_t val = xb64(&v);

			nv_add_scalar8(out, name, type, val);
			if (depth == 0 && strcmp(name, "pool_guid") == 0)
				*pool_guid = val;
			break;
		}
		case DT_UINT64_ARRAY: {
			struct xdr v = { vs, pend, 0 };
			uint64_t *vals = NULL;
			uint32_t i;

			if (nelem) {
				vals = malloc((size_t)nelem * 8);
				if (!vals)
					return -1;
			}
			for (i = 0; i < nelem; i++)
				vals[i] = xb64(&v);
			nv_add_u64_array(out, name, vals, nelem);
			free(vals);
			break;
		}
		case DT_STRING: {
			struct xdr v = { vs, pend, 0 };
			uint32_t slen = xb32(&v);
			char *sval;

			if (v.p + slen > pend) {
				x->p = pend;
				continue;
			}
			sval = malloc((size_t)slen + 1);
			if (!sval)
				return -1;
			memcpy(sval, v.p, slen);
			sval[slen] = '\0';

			if (strcmp(name, "path") == 0)
				nv_add_string(out, name, device);
			else if (strcmp(name, "type") == 0 &&
				 strcmp(sval, "file") == 0)
				nv_add_string(out, name, "disk");
			else
				nv_add_string(out, name, sval);
			free(sval);
			break;
		}
		case DT_NVLIST: {
			struct xdr sub = { vs, pend, 0 };
			struct nvbuf child = { 0 };

			if (xdr_nvlist(&sub, &child, depth + 1, device,
				       pool_guid) < 0) {
				free(child.data);
				return -1;
			}
			/*
			 * The on-disk label of a single-vdev pool stores the
			 * top-level vdev directly as "vdev_tree".  The kernel
			 * import path requires the top vdev to be a synthetic
			 * "root" vdev with the real vdev(s) as children (the
			 * first vdev allocated during spa_load must be of type
			 * root, else vdev_alloc() returns EINVAL).  Wrap it the
			 * same way libzfs does in zpool import.
			 */
			if (depth == 0 && strcmp(name, "vdev_tree") == 0) {
				struct nvbuf root = { 0 };

				nv_add_string(&root, "type", "root");
				nv_add_scalar8(&root, "id", DT_UINT64, 0);
				nv_add_scalar8(&root, "guid", DT_UINT64,
					       *pool_guid);
				nv_add_nvlist_array(&root, "children", &child, 1);
				if (root.err)
					out->err = 1;
				nv_add_nvlist(out, "vdev_tree", &root);
				free(root.data);
			} else {
				nv_add_nvlist(out, name, &child);
			}
			free(child.data);
			break;
		}
		case DT_NVLIST_ARRAY: {
			struct xdr sub = { vs, pend, 0 };
			struct nvbuf *kids = NULL;
			uint32_t i;
			int bad = 0;

			if (nelem) {
				kids = calloc(nelem, sizeof(*kids));
				if (!kids)
					return -1;
			}
			for (i = 0; i < nelem; i++)
				if (xdr_nvlist(&sub, &kids[i], depth + 1,
					       device, pool_guid) < 0) {
					bad = 1;
					break;
				}
			if (!bad)
				nv_add_nvlist_array(out, name, kids, nelem);
			for (i = 0; i < nelem; i++)
				free(kids[i].data);
			free(kids);
			if (bad)
				return -1;
			break;
		}
		default:
			kmsg_log(KMSG_INFO,
				 "zfs import: skipping nvpair %s (type %u)\n",
				 name, type);
			break;
		}

		if (out->err)
			return -1;
		x->p = pend;
	}
}

/*
 * read_pool_config - read a vdev label and transcode it to an import config
 *
 * Tries the two labels at the front of @device (L0 at offset 0, L1 at one
 * label size in) and returns the first that yields a valid native config nvlist
 * with a pool guid.  Returns a malloc'd packed nvlist (caller frees) with its
 * length in *@conf_len and the pool guid in *@guid, or NULL on failure.
 */
static unsigned char *read_pool_config(const char *device, size_t *conf_len,
				       uint64_t *guid)
{
	unsigned char *label, *conf = NULL;
	const off_t bases[2] = { 0, VDEV_LABEL_SIZE };
	int dfd, li;

	dfd = open(device, O_RDONLY | O_CLOEXEC);
	if (dfd < 0) {
		kmsg_log(KMSG_ERR, "zfs import: open %s: %s\n",
			 device, strerror(errno));
		return NULL;
	}

	label = malloc(VDEV_PHYS_NVLIST_SIZE);
	if (!label) {
		close(dfd);
		return NULL;
	}

	for (li = 0; li < 2 && !conf; li++) {
		ssize_t n = pread(dfd, label, VDEV_PHYS_NVLIST_SIZE,
				  bases[li] + VDEV_PHYS_OFFSET);
		struct nvbuf pairs = { 0 };
		struct xdr x;
		int rc;

		if (n < 16) {
			kmsg_log(KMSG_ERR,
				 "zfs import: label %d short read %zd: %s\n",
				 li, n, strerror(errno));
			continue;
		}
		if (label[0] != NV_ENCODE_XDR) {
			kmsg_log(KMSG_ERR,
				 "zfs import: label %d not XDR-encoded (enc=%u)\n",
				 li, label[0]);
			continue;
		}

		x.p = label + 4;	/* skip nvs_header */
		x.end = label + n;
		x.err = 0;
		*guid = 0;

		rc = xdr_nvlist(&x, &pairs, 0, device, guid);
		if (rc == 0 && !pairs.err && *guid) {
			conf = nv_pack(&pairs, conf_len);
			kmsg_log(KMSG_INFO,
				 "zfs import: label %d parsed, guid=%llu conf_len=%zu\n",
				 li, (unsigned long long)*guid,
				 conf ? *conf_len : 0);
		} else {
			kmsg_log(KMSG_ERR,
				 "zfs import: label %d parse failed (rc=%d err=%d guid=%llu)\n",
				 li, rc, pairs.err, (unsigned long long)*guid);
		}
		free(pairs.data);
	}

	free(label);
	close(dfd);
	return conf;
}

/*
 * zfs_import_pool - import the ZFS pool backing the Lustre root
 *
 * Reads @device's vdev label, fixes up the leaf path/type, and hands the
 * resulting config to ZFS_IOC_POOL_IMPORT.  Returns 0 on success, -1 on error.
 */
static int zfs_import_pool(const char *pool, const char *device)
{
	unsigned char *conf, *zc;
	size_t conf_len = 0, plen;
	uint64_t guid = 0, cptr, clen;
	int zfd, rc, saved;

	conf = read_pool_config(device, &conf_len, &guid);
	if (!conf) {
		kmsg_log(KMSG_ERR, "zfs import: no valid label on %s\n", device);
		return -1;
	}

	zfd = open(ZFS_DEV, O_RDWR | O_CLOEXEC);
	if (zfd < 0) {
		kmsg_log(KMSG_ERR, "zfs import: open %s: %s\n",
			 ZFS_DEV, strerror(errno));
		free(conf);
		return -1;
	}

	zc = calloc(1, ZFS_CMD_SIZE);
	if (!zc) {
		close(zfd);
		free(conf);
		return -1;
	}

	plen = strlen(pool);
	if (plen > 4095)
		plen = 4095;
	memcpy(zc + ZC_NAME_OFF, pool, plen);
	cptr = (uint64_t)(uintptr_t)conf;
	clen = conf_len;
	memcpy(zc + ZC_NVLIST_CONF_OFF, &cptr, 8);
	memcpy(zc + ZC_NVLIST_CONF_SIZE_OFF, &clen, 8);
	memcpy(zc + ZC_GUID_OFF, &guid, 8);
	/* zc_cookie (import flags) stays 0 = ZFS_IMPORT_NORMAL. */

	kmsg_log(KMSG_INFO, "zfs import: POOL_IMPORT %s guid=%llu conf_len=%zu\n",
		 pool, (unsigned long long)guid, conf_len);

	rc = ioctl(zfd, ZFS_IOC_POOL_IMPORT, zc);
	saved = errno;

	free(zc);
	free(conf);
	close(zfd);

	if (rc != 0) {
		kmsg_log(KMSG_ERR,
			 "zfs import: POOL_IMPORT %s failed: %s\n",
			 pool, strerror(saved));
		return -1;
	}

	kmsg_log(KMSG_INFO, "zfs import: imported pool %s (guid %llu)\n",
		 pool, (unsigned long long)guid);
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
 * modprobe(8) finds the modules at the usual path.  These submounts ride along
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
		}
		if (!mounted) {
			kmsg_log(KMSG_ERR, "no fstype matched %s\n", rootspec);
			return 1;
		}
	}

	copy_initramfs_to_newroot();

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

	f = fopen(CMDLINE_PATH, "r");
	if (!f || !fgets(cmdline, sizeof(cmdline), f)) {
		kmsg_log(KMSG_ERR, "cannot read %s\n", CMDLINE_PATH);
		if (f)
			fclose(f);
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
	kmsg_log(KMSG_ERR, "boot failed\n");
	return 1;
}
