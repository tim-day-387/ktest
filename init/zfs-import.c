// SPDX-License-Identifier: GPL-2.0-only

/*
 * ZFS pool import, shared by ktest-init (Lustre root boot) and zimport (the
 * standalone CLI).
 *
 * The Lustre root image is a ZFS pool that was created on the host against an
 * image *file* (`zpool create lustre_pool <file>`); at boot those same bytes
 * are a block device (e.g. /dev/vda).  stock zpool(8) cannot import such a
 * pool (its scan fixes the vdev path but never the type), so we import by
 * talking to /dev/zfs directly, the same way libzfs does:
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

#include "init-common.h"
#include "zfs-import.h"

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

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
 * into *@pool_guid for the import ioctl, and the pool name (top-level
 * "name") into @pool_name when non-NULL (ZFS_POOL_NAME_MAX bytes).
 *
 * Returns 0 on success, -1 on a malformed stream or allocation failure.
 */
static int xdr_nvlist(struct xdr *x, struct nvbuf *out, int depth,
		      const char *device, uint64_t *pool_guid,
		      char *pool_name)
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
			if (depth == 0 && pool_name &&
			    strcmp(name, "name") == 0) {
				strncpy(pool_name, sval, ZFS_POOL_NAME_MAX - 1);
				pool_name[ZFS_POOL_NAME_MAX - 1] = '\0';
			}
			free(sval);
			break;
		}
		case DT_NVLIST: {
			struct xdr sub = { vs, pend, 0 };
			struct nvbuf child = { 0 };

			if (xdr_nvlist(&sub, &child, depth + 1, device,
				       pool_guid, pool_name) < 0) {
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
					       device, pool_guid,
					       pool_name) < 0) {
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
 * zfs_read_pool_config - read a vdev label and transcode it to an import config
 *
 * Tries the two labels at the front of @device (L0 at offset 0, L1 at one
 * label size in) and returns the first that yields a valid native config nvlist
 * with a pool guid.  Returns a malloc'd packed nvlist (caller frees) with its
 * length in *@conf_len and the pool guid in *@guid, or NULL on failure.  The
 * pool name from the label lands in @name (ZFS_POOL_NAME_MAX bytes) when
 * non-NULL.
 */
unsigned char *zfs_read_pool_config(const char *device, size_t *conf_len,
				    uint64_t *guid, char *name)
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

		rc = xdr_nvlist(&x, &pairs, 0, device, guid, name);
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
 * zfs_import_config - hand a transcoded pool config to ZFS_IOC_POOL_IMPORT
 *
 * Returns 0 on success, -1 on error.
 */
int zfs_import_config(const char *pool, const unsigned char *conf,
		      size_t conf_len, uint64_t guid)
{
	unsigned char *zc;
	size_t plen;
	uint64_t cptr, clen;
	int zfd, rc, saved;

	zfd = open(ZFS_DEV, O_RDWR | O_CLOEXEC);
	if (zfd < 0) {
		kmsg_log(KMSG_ERR, "zfs import: open %s: %s\n",
			 ZFS_DEV, strerror(errno));
		return -1;
	}

	zc = calloc(1, ZFS_CMD_SIZE);
	if (!zc) {
		close(zfd);
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
 * zfs_import_pool - import the ZFS pool backing the Lustre root
 *
 * Reads @device's vdev label, fixes up the leaf path/type, and hands the
 * resulting config to ZFS_IOC_POOL_IMPORT.  Returns 0 on success, -1 on error.
 */
int zfs_import_pool(const char *pool, const char *device)
{
	unsigned char *conf;
	size_t conf_len = 0;
	uint64_t guid = 0;
	int rc;

	conf = zfs_read_pool_config(device, &conf_len, &guid, NULL);
	if (!conf) {
		kmsg_log(KMSG_ERR, "zfs import: no valid label on %s\n", device);
		return -1;
	}

	rc = zfs_import_config(pool, conf, conf_len, guid);
	free(conf);
	return rc;
}
