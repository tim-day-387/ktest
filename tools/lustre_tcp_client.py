#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Userspace Lustre client: LNet over TCP (socklnd) plus PtlRPC.

Runs the mount flow of a kernel client against a server reachable over
TCP: connect to the MGS, learn the filesystem's MDTs and OSTs from its
client config log, connect to them, and disconnect in reverse order.
The options exercise individual pieces on top of that: config logs and
the nodemap database on the MGS, the namespace and file data through
the MDTs and OSTs, and a read-only FUSE mount of the whole thing.

With several MDTs (DNE) the namespace is followed the way LMV does it:
the FLD of MDT0000 says which MDT a FID lives on, and the LMV EA of a
striped directory which stripe a name hashes to.

The LNet acceptor only accepts connections from privileged ports, so
this has to run as root.

Example:
  lustre_tcp_client.py --server 10.0.0.1
  lustre_tcp_client.py --server 10.0.0.1 --show-path /
  lustre_tcp_client.py --server 10.0.0.1 --fsname lustre --config-log
  lustre_tcp_client.py --server 10.0.0.1 --fsname lustre --nodemap
  lustre_tcp_client.py --server 10.0.0.1 --fsname lustre --fuse /mnt/l
"""

import argparse
import dataclasses
import errno
import logging
import os
import re
import socket
import stat
import struct
import sys
import threading
import time
import uuid
from typing import Optional

log = logging.getLogger("lustre")


class WireStruct:
    """A little-endian wire struct with named members.

    pack() takes the members by keyword and zeroes the rest; unpack()
    returns a dict and accepts a short buffer, reading what is missing
    as zeros, since older servers send shorter versions of some structs.
    """

    def __init__(self, fmt, names):
        self.struct = struct.Struct(fmt)
        self.names = names
        self.size = self.struct.size
        # zero value per member: b"" for 's' members, 0 otherwise
        self.defaults = []
        for count, code in re.findall(r"(\d*)([a-zA-Z?])", fmt):
            if code == "s":
                self.defaults.append(b"")
            else:
                self.defaults.extend([0] * (int(count) if count else 1))
        assert len(self.defaults) == len(names), (fmt, names)

    def pack(self, **kw):
        """Pack from keyword members, everything else zero."""
        vals = [kw.pop(n, d) for n, d in zip(self.names, self.defaults)]
        if kw:
            raise TypeError(f"unknown members: {', '.join(kw)}")
        return self.struct.pack(*vals)

    def unpack(self, data):
        """Unpack the start of data into a dict keyed by member name."""
        data = bytes(data[: self.size]).ljust(self.size, b"\0")
        return dict(zip(self.names, self.struct.unpack(data)))


# ---------------------------------------------------------------------------
# LNet wire constants (lnet-idl.h, lnet-types.h, nidstr.h, socklnd-idl.h)
# ---------------------------------------------------------------------------
LNET_PROTO_ACCEPTOR_MAGIC = 0xACCE7100
LNET_PROTO_ACCEPTOR_VERSION = 1
LNET_PROTO_MAGIC = 0x45726963
LNET_PROTO_PING_MAGIC = 0x70696E67
LNET_PID_LUSTRE = 12345
LNET_ACCEPTOR_PORT = 988
LNET_RESERVED_PORTAL = 0
LNET_PROTO_PING_MATCHBITS = 0x8000000000000000
LNET_WIRE_HANDLE_COOKIE_NONE = 0xFFFFFFFFFFFFFFFF
LNET_NID_ANY = 0xFFFFFFFFFFFFFFFF
LNET_NI_STATUS_UP = 0x15AAC0DE
LNET_PING_FEAT_NI_STATUS = 1 << 1

LNET_MSG_ACK = 0
LNET_MSG_PUT = 1
LNET_MSG_GET = 2
LNET_MSG_REPLY = 3
LNET_MSG_HELLO = 4
LNET_MSG_NAMES = {
    LNET_MSG_ACK: "ACK",
    LNET_MSG_PUT: "PUT",
    LNET_MSG_GET: "GET",
    LNET_MSG_REPLY: "REPLY",
    LNET_MSG_HELLO: "HELLO",
}

SOCKLND = 2
LOLND = 9

KSOCK_PROTO_V3 = 3
KSOCK_MSG_NOOP = 0xC0
KSOCK_MSG_LNET = 0xC1
SOCKLND_CONN_ANY = 0
SOCKLND_CONN_CONTROL = 1
SOCKLND_CONN_BULK_IN = 2
SOCKLND_CONN_BULK_OUT = 3

RESERVED_PORTS = range(1023, 511, -1)
DEFAULT_TIMEOUT = 30.0  # seconds to wait for a reply

# struct lnet_acceptor_connreq { u32 magic; u32 version; u64 nid; }
ACCEPTOR_CONNREQ = struct.Struct("<IIQ")
# struct ksock_hello_msg_nid4 (no kshm_ips, kshm_nips is always 0)
KSOCK_HELLO_NID4 = struct.Struct("<IIQQIIQQII")
# struct ksock_msg_hdr { u32 type; u32 csum; u64 zc_cookies[2]; }
KSOCK_MSG_HDR = struct.Struct("<IIQQ")
# struct _lnet_hdr_nid4 common part; the 40 byte union follows.
LNET_HDR_COMMON = struct.Struct("<QQIIII")
LNET_HDR_UNION_LEN = 40
LNET_HDR_LEN = LNET_HDR_COMMON.size + LNET_HDR_UNION_LEN
# union lnet_cmd_hdr members, little endian on the wire.
# put: ack_wmd, match_bits, hdr_data, ptl_index, offset
LNET_PUT = struct.Struct("<QQQQII")
# get: return_wmd, match_bits, ptl_index, src_offset, sink_length
LNET_GET = struct.Struct("<QQQIII")
# reply: dst_wmd
LNET_REPLY = struct.Struct("<QQ")
# ack: dst_wmd, match_bits, mlength
LNET_ACK = struct.Struct("<QQQI")
# struct lnet_ping_info header and struct lnet_ni_status
LNET_PING_INFO = struct.Struct("<IIII")
LNET_NI_STATUS = struct.Struct("<QII")

# ---------------------------------------------------------------------------
# Lustre / PtlRPC wire constants (lustre_idl.h, lustre_user.h, lustre_ver.h)
# ---------------------------------------------------------------------------
LUSTRE_MSG_MAGIC_V2 = 0x0BD00BD3
# struct lustre_msg_v2 up to lm_buflens[]: bufcount, secflvr, magic,
# repsize, cksum, flags, mbits, padding
LUSTRE_MSG_HDR = struct.Struct("<8I")
PTL_RPC_MSG_REQUEST = 4711
PTL_RPC_MSG_ERR = 4712
PTL_RPC_MSG_REPLY = 4713

PTLRPC_MSG_VERSION = 0x00000003
LUSTRE_OBD_VERSION = 0x00010000
LUSTRE_MDS_VERSION = 0x00020000
LUSTRE_OST_VERSION = 0x00030000
LUSTRE_LOG_VERSION = 0x00050000
LUSTRE_MGS_VERSION = 0x00060000

MSGHDR_AT_SUPPORT = 0x1
MSGHDR_CKSUM_INCOMPAT18 = 0x2

MSG_CONNECT_RECOVERING = 0x1
MSG_CONNECT_RECONNECT = 0x2
MSG_CONNECT_REPLAYABLE = 0x4
MSG_CONNECT_INITIAL = 0x20

# opcodes
OST_GETATTR = 1
OST_READ = 3
OST_CONNECT = 8
OST_DISCONNECT = 9
OST_STATFS = 13
MDS_GETATTR = 33
MDS_GETATTR_NAME = 34
MDS_READPAGE = 37
MDS_CONNECT = 38
MDS_DISCONNECT = 39
MDS_GET_ROOT = 40
MDS_STATFS = 41
MGS_CONNECT = 250
MGS_DISCONNECT = 251
MGS_CONFIG_READ = 256
OBD_PING = 400
LLOG_ORIGIN_HANDLE_CREATE = 501
LLOG_ORIGIN_HANDLE_NEXT_BLOCK = 502
LLOG_ORIGIN_HANDLE_READ_HEADER = 503

MGS_CFG_T_RECOVER = 2
MGS_CFG_T_NODEMAP = 4
LUSTRE_NODEMAP_NAME = "nodemap"
II_END_OFF = 0xFFFFFFFFFFFFFFFE
LIP_MAGIC = 0x8A6D6B6C
NM_TYPE_SHIFT = 28
NM_TYPE_MASK = 0x0FFFFFFF
NODEMAP_IDX_NAMES = {
    0: "empty",
    1: "cluster",
    2: "range",
    3: "uidmap",
    4: "gidmap",
    5: "projidmap",
    6: "nidmask",
    15: "global",
}
NODEMAP_CLUSTER_REC = 0
NODEMAP_CLUSTER_ROLES = 1
NODEMAP_CLUSTER_OFFSET = 2
NODEMAP_CLUSTER_CAPS = 3
NODEMAP_CLUSTER_VERSION = 4
NODEMAP_FILESET = 512
NODEMAP_FILESET_SUBID_RANGE = 256
NM_FLAG_NAMES = (
    (0x1, "allow_root"),
    (0x2, "trust_client_ids"),
    (0x4, "deny_unknown"),
    (0x8, "map_uid"),
    (0x10, "map_gid"),
    (0x20, "audit"),
    (0x40, "forbid_encrypt"),
    (0x80, "map_projid"),
)
NM_FLAG2_NAMES = (
    (0x1, "readonly_mount"),
    (0x2, "deny_mount"),
    (0x4, "fileset_iam"),
    (0x8, "gss_identify"),
    (0x10, "no_trust_client_perms"),
)
NODEMAP_VERSION_POLICY_NAMES = {0: "allow", 1: "hard_block"}
PING_INTERVAL = 30  # seconds between OBD_PINGs; eviction is at 6 missed
FID_SEQ_OST_MDT0 = 0
FID_SEQ_LLOG = 0xA
FID_SEQ_LOV_DEFAULT = 0xFFFFFFFFFFFFFFFF
FID_SEQ_NORMAL = 0x200000400  # below it: the root and the like, on MDT0000

# DNE: the FID location database and striped directories (lustre_idl.h,
# lustre_user.h)
FLD_QUERY = 900
FLD_LOOKUP = 2  # the fld_op in front of the range, for servers before 2.6
FLD_REQUEST_PORTAL = 29
LU_SEQ_RANGE_MDT = 0x0
LU_SEQ_RANGE = struct.Struct("<QQII")  # lsr_start, lsr_end, lsr_index, lsr_flags
LMV_MAGIC_V1 = 0x0CD20CD0  # the master object; stripes carry LMV_MAGIC_STRIPE
LMV_HASH_TYPE_MASK = 0xFFFF
LMV_HASH_TYPE_ALL_CHARS = 1
LMV_HASH_TYPE_FNV_1A_64 = 2
LMV_HASH_TYPE_CRUSH = 3
LMV_HASH_TYPE_CRUSH2 = 4
LMV_CRUSH_PG_COUNT = 4096
LMV_MAX_STRIPE_COUNT = 2000
# struct lmv_mds_md_v1 up to lmv_stripe_fids[]: magic, stripe_count,
# master_mdt_index, hash_type, layout_version, migrate_offset,
# migrate_hash, padding2, padding3, pool_name[16]
LMV_MDS_MD_V1 = struct.Struct("<8IQ16s")
LDD_F_SV_TYPE_MDT = 1
LDD_F_SV_TYPE_OST = 2
LDD_F_SV_TYPE_MGS = 4
NIDTBL_PAGES = 4  # units asked for per MGS_CONFIG_READ
FSNAME_PROBE_LOGS = 64  # logids searched for a client log without --fsname

MGS_REQUEST_PORTAL = 26
MDS_REQUEST_PORTAL = 12
MDS_READPAGE_PORTAL = 23
OST_IO_PORTAL = 6
OST_REQUEST_PORTAL = 28
OST_BULK_PORTAL = 8
MDS_BULK_PORTAL = 14
MGS_BULK_PORTAL = 33
BULK_PORTALS = (OST_BULK_PORTAL, MDS_BULK_PORTAL, MGS_BULK_PORTAL)

OBD_CONNECT_INDEX = 0x2
OBD_CONNECT_VERSION = 0x20
OBD_CONNECT_IBITS = 0x1000
OBD_CONNECT_ATTRFID = 0x4000
OBD_CONNECT_AT = 0x1000000
OBD_CONNECT_FID = 0x40000000
OBD_CONNECT_FULL20 = 0x1000000000
OBD_CONNECT_64BITHASH = 0x4000000000
OBD_CONNECT_LVB_TYPE = 0x400000000000  # what the MDT takes for a DNE client
OBD_CONNECT_DIR_STRIPE = 0x400000000000000  # striped directories
OBD_CONNECT_FLAGS2 = 0x8000000000000000
MDS_INODELOCK_FULL = 0x7F

OBD_TIMEOUT_DEFAULT = 100
LUSTRE_VERSION_DEFAULT = (2, 17, 58, 0)  # claimed in ocd_version
UUID_MAX = 40
LUSTRE_JOBID_SIZE = 32

# mdt_body valid bits and directory entry attributes (lustre_idl.h)
OBD_MD_FLID = 0x1
OBD_MD_FLATIME = 0x2
OBD_MD_FLMTIME = 0x4
OBD_MD_FLCTIME = 0x8
OBD_MD_FLSIZE = 0x10
OBD_MD_FLBLOCKS = 0x20
OBD_MD_FLMODE = 0x80
OBD_MD_FLTYPE = 0x100
OBD_MD_FLUID = 0x200
OBD_MD_FLGID = 0x400
OBD_MD_FLFLAGS = 0x800
OBD_MD_FLNLINK = 0x2000
OBD_MD_FLRDEV = 0x10000
OBD_MD_FLEASIZE = 0x20000
OBD_MD_LINKNAME = 0x40000
OBD_MD_FLGROUP = 0x1000000
OBD_MD_FLDIREA = 0x10000000  # a directory's EA
OBD_MD_MDS = 0x100000000  # reply: the object lives on another MDT
OBD_MD_MEA = 0x400000000  # the LMV EA of a striped directory
OBD_FL_SRVLOCK = 0x800  # obdo flag: server takes the DLM lock
OBD_BRW_SRVLOCK = 0x200  # niobuf flag: same, marks the IO lockless
OBD_MD_GETATTR = (
    OBD_MD_FLID
    | OBD_MD_FLATIME
    | OBD_MD_FLMTIME
    | OBD_MD_FLCTIME
    | OBD_MD_FLSIZE
    | OBD_MD_FLBLOCKS
    | OBD_MD_FLMODE
    | OBD_MD_FLTYPE
    | OBD_MD_FLUID
    | OBD_MD_FLGID
    | OBD_MD_FLNLINK
    | OBD_MD_FLRDEV
)

# layouts (lustre_user.h)
LOV_MAGIC_V1 = 0x0BD10BD0
LOV_MAGIC_V3 = 0x0BD30BD0
LOV_MAGIC_COMP_V1 = 0x0BD60BD0
LOV_PATTERN_MDT = 0x100
LOV_PATTERN_F_RELEASED = 0x80000000
LUSTRE_EOF = 0xFFFFFFFFFFFFFFFF  # extent end of the last component
LCME_FL_STALE = 0x1
LCME_FL_INIT = 0x10
LOV_EA_BUFSIZE = 8192  # mb_eadatasize asked for; MDT sizes reply
READ_CHUNK = 1 << 20  # bytes per OST_READ, the default max brw
LUDA_FID = 0x1
LUDA_TYPE = 0x2
LDF_EMPTY = 0x1
LU_PAGE_SHIFT = 12
LU_PAGE_SIZE = 1 << LU_PAGE_SHIFT
PATH_MAX = 4096
MDS_DIR_END_OFF = 0xFFFFFFFFFFFFFFFE
READDIR_BYTES = 8 * LU_PAGE_SIZE  # pages asked for per MDS_READPAGE

# llog (lustre_idl.h) and config records (lustre_cfg.h)
LLOG_CONFIG_ORIG_CTXT = 0
LLOG_F_IS_PLAIN = 0x4
LLOG_MIN_CHUNK_SIZE = 8192
LLOG_OP_MAGIC = 0x10600000
LLOG_OP_MASK = 0xFFF00000
OBD_CFG_REC = LLOG_OP_MAGIC | 0x20000
LUSTRE_CFG_VERSION = 0x1CF60001
LCFG_MARKER = 0x00CF010
LCFG_MOUNTOPT = 0x00CF007
LCFG_LOV_ADD_OBD = 0x00CF00D  # add_osc: device, ost uuid, index, gen
LCFG_LOV_DEL_OBD = 0x00CF00E
LCFG_LOV_ADD_INA = 0x00CE013  # add_osc_inactive, same buffers
LCFG_ADD_MDC = 0x00CF014  # add_mdc: device, mdt uuid, index, gen
LCFG_DEL_MDC = 0x00CF015
LCFG_SET_PARAM = 0x00CE032
LCFG_NODEMAP_BASE = 0x00CE040  # LCFG_NODEMAP_*: this under the mask
LCFG_NODEMAP_MASK = 0xFFFF0
CM_START = 0x1
CM_END = 0x2
CM_SKIP = 0x4
CM_FLAG_NAMES = (
    (0x01, "START"),
    (0x02, "END"),
    (0x04, "SKIP"),
    (0x08, "UPGRADE146"),
    (0x10, "EXCLUDE"),
)

# lcfg_data_table[] in lustre_cfg.h: command -> (event name, buffer labels)
LCFG_NAMES = {
    0x00CF001: ("attach", ("type", "UUID")),
    0x00CF002: ("detach", ()),
    0x00CF003: ("setup", ("UUID", "node", "options", "failout")),
    0x00CF004: ("cleanup", ()),
    0x00CF005: ("add_uuid", ("node", "nid")),
    0x00CF006: ("del_uuid", ()),
    0x00CF007: ("new_profile", ("name", "lov", "lmv")),
    0x00CF008: ("del_mountopt", ()),
    0x00CF009: ("set_timeout", ("parameter",)),
    0x00CF00A: ("set_upcall", ()),
    0x00CF00B: ("add_conn", ("node",)),
    0x00CF00C: ("del_conn", ()),
    0x00CF00D: ("add_osc", ("ost", "index", "gen", "UUID")),
    0x00CF00E: ("del_osc", ()),
    0x00CF00F: ("conf_param", ("parameter", "value")),
    0x00CF010: ("marker", ()),
    0x00CE011: ("log_start", ()),
    0x00CE012: ("log_end", ()),
    0x00CE013: ("add_osc_inactive", ()),
    0x00CF014: ("add_mdc", ("mdt", "index", "gen", "UUID")),
    0x00CF015: ("del_mdc", ()),
    0x00CE016: ("security", ("parameter",)),
    0x00CE020: ("new_pool", ("fsname", "pool")),
    0x00CE021: ("add_pool", ("fsname", "pool", "ost")),
    0x00CE022: ("remove_pool", ("fsname", "pool", "ost")),
    0x00CE023: ("del_pool", ("fsname", "pool")),
    0x00CE030: ("set_ldlm_timeout", ("parameter",)),
    0x00CF031: ("pre_cleanup", ()),
    0x00CE032: ("set_param", ("parameter", "value")),
}


def obd_ocd_version(major, minor, patch, fix):
    """Pack a Lustre version into its OBD_OCD_VERSION() u32 form."""
    return (major << 24) | (minor << 16) | (patch << 8) | fix


def ocd_version_str(v):
    """Dotted text form of an ocd_version u32."""
    return f"{(v >> 24) & 255}.{(v >> 16) & 255}.{(v >> 8) & 255}.{v & 255}"


# struct ptlrpc_body_v3, 184 bytes
# pb_status carries a negative errno, so unpack it signed
PTLRPC_BODY = WireStruct(
    f"<QIIIiQHHIQQIIIIIIQ4QQQQII{LUSTRE_JOBID_SIZE}s",
    (
        "pb_handle",
        "pb_type",
        "pb_version",
        "pb_opc",
        "pb_status",
        "pb_last_xid",
        "pb_tag",
        "pb_padding0",
        "pb_projid",
        "pb_last_committed",
        "pb_transno",
        "pb_flags",
        "pb_op_flags",
        "pb_conn_cnt",
        "pb_timeout",
        "pb_service_time",
        "pb_limit",
        "pb_slv",
        "pb_pre_versions0",
        "pb_pre_versions1",
        "pb_pre_versions2",
        "pb_pre_versions3",
        "pb_mbits",
        "pb_padding64_0",
        "pb_padding64_1",
        "pb_uid",
        "pb_gid",
        "pb_jobid",
    ),
)
assert PTLRPC_BODY.size == 184

# struct obd_connect_data, 192 bytes
OBD_CONNECT_DATA = WireStruct(
    "<QIIIIQBBHIQIIIIQHBBIQQ12Q",
    (
        "ocd_connect_flags",
        "ocd_version",
        "ocd_grant",
        "ocd_index",
        "ocd_brw_size",
        "ocd_ibits_known",
        "ocd_grant_blkbits",
        "ocd_grant_inobits",
        "ocd_grant_tax_kb",
        "ocd_grant_max_blks",
        "ocd_transno",
        "ocd_group",
        "ocd_cksum_types",
        "ocd_max_easize",
        "ocd_instance",
        "ocd_maxbytes",
        "ocd_maxmodrpcs",
        "ocd_conn_policy",
        "padding0",
        "padding1",
        "ocd_connect_flags2",
        "ocd_compr_type",
    )
    + tuple(f"padding{i:X}" for i in range(4, 16)),
)
assert OBD_CONNECT_DATA.size == 192

# struct obd_statfs, 144 bytes
OBD_STATFS = WireStruct(
    "<6Q40sIIQIIII6I",
    (
        "os_type",
        "os_blocks",
        "os_bfree",
        "os_bavail",
        "os_files",
        "os_ffree",
        "os_fsid",
        "os_bsize",
        "os_namelen",
        "os_maxbytes",
        "os_state",
        "os_fprecreated",
        "os_granted",
        "os_failure_domain",
    )
    + tuple(f"os_spare{i}" for i in range(4, 10)),
)
assert OBD_STATFS.size == 144

# struct mdt_body, 216 bytes
MDT_BODY = WireStruct(
    "<16s16sQQQQQQQQQ18I5Q",
    (
        "mb_fid1",
        "mb_fid2",
        "mb_open_handle",
        "mb_valid",
        "mb_size",
        "mb_mtime",
        "mb_atime",
        "mb_ctime",
        "mb_blocks",
        "mb_version",
        "mb_t_state",
        "mb_fsuid",
        "mb_fsgid",
        "mb_capability",
        "mb_mode",
        "mb_uid",
        "mb_gid",
        "mb_flags",
        "mb_rdev",
        "mb_nlink",
        "mb_layout_gen",
        "mb_suppgid",
        "mb_eadatasize",
        "mb_aclsize",
        "mb_max_mdsize",
        "mb_unused3",
        "mb_uid_h",
        "mb_gid_h",
        "mb_projid",
        "mb_dom_size",
        "mb_dom_blocks",
        "mb_btime",
        "mb_xattr_absent",
        "mb_padding_10",
    ),
)
assert MDT_BODY.size == 216

# struct lu_fid { u64 f_seq; u32 f_oid; u32 f_ver; }
LU_FID = struct.Struct("<QII")
LMV_EA_BUFSIZE = LMV_MDS_MD_V1.size + LMV_MAX_STRIPE_COUNT * LU_FID.size
# struct lu_dirpage header and struct lu_dirent header; entries carry
# lde_name right after the header, then a u16 luda_type when LUDA_TYPE
LU_DIRPAGE = struct.Struct("<QQII")  # hash_start, hash_end, flags, pad
LU_DIRENT = struct.Struct("<QIIQHHI")  # fid, hash, reclen, namelen, attrs

# struct mgs_config_body (80), mgs_config_res (16), mgs_nidtbl_entry (24)
MGS_CONFIG_BODY = WireStruct(
    "<64sQHBBI",
    (
        "mcb_name",
        "mcb_offset",
        "mcb_type",
        "mcb_rec_nid_size",
        "mcb_bits",
        "mcb_units",
    ),
)
MGS_CONFIG_RES = struct.Struct("<QQ")
MGS_NIDTBL_ENTRY = struct.Struct("<QIIIBBBB")
LNET_NID16 = struct.Struct("<BB2s4s12s")  # nid_size, nid_type, num, addr[0]
assert MGS_CONFIG_BODY.size == 80 and MGS_NIDTBL_ENTRY.size == 24

# struct obdo (208), obd_ioobj (24), niobuf_remote (16)
OBDO = WireStruct(
    "<Q16sQQQQQQQIIIIIIIIQIIQ28sIIIQIIQQ",
    (
        "o_valid",
        "o_oi",
        "o_parent_seq",
        "o_size",
        "o_mtime",
        "o_atime",
        "o_ctime",
        "o_blocks",
        "o_grant",
        "o_blksize",
        "o_mode",
        "o_uid",
        "o_gid",
        "o_flags",
        "o_nlink",
        "o_parent_oid",
        "o_misc",
        "o_ioepoch",
        "o_stripe_idx",
        "o_parent_ver",
        "o_handle",
        "o_layout",
        "o_layout_version",
        "o_uid_h",
        "o_gid_h",
        "o_data_version",
        "o_projid",
        "o_padding_4",
        "o_padding_5",
        "o_padding_6",
    ),
)
assert OBDO.size == 208
OBD_IOOBJ = struct.Struct("<16sII")  # ioo_oid, ioo_max_brw, ioo_bufcnt
NIOBUF_REMOTE = struct.Struct("<QII")  # rnb_offset, rnb_len, rnb_flags
# struct lov_mds_md_v1 header (32), lov_ost_data_v1 (24), composite header
# and entry (32 / 48): only the fields we read
LOV_MDS_MD = struct.Struct("<II16sIHH")  # magic, pattern, oi, ssize, scount, gen
LOV_OST_DATA = struct.Struct("<16sII")  # l_ost_oi, l_ost_gen, l_ost_idx
# struct lov_comp_md_v1 is 32 bytes: 14 of padding follow the fields we read
LOV_COMP_MD = struct.Struct("<IIIHHH14x")  # magic, size, gen, flags, entries, mirrors
LOV_COMP_ENTRY = struct.Struct("<IIQQII")  # id, flags, start, end, offset, size
LOV_COMP_ENTRY_SIZE = 48  # sizeof(struct lov_comp_md_entry_v1)
assert LOV_COMP_MD.size == 32

# struct lu_idxpage header and the nodemap key / record union (8 + 32)
LU_IDXPAGE = struct.Struct("<IHHQ")
NODEMAP_KEY = struct.Struct("<II")
NODEMAP_REC_SIZE = 32
# name, flags, flags2, pad, squash projid/uid/gid
NODEMAP_CLUSTER = struct.Struct("<17sBBBIII")
NODEMAP_ROLES = struct.Struct("<QQQQ")  # roles, privs, roles_raise
NODEMAP_OFFSET = struct.Struct("<8I")  # start/limit uid, gid, projid
NODEMAP_CAPS = struct.Struct("<QB")  # caps, type
NODEMAP_VERSION = struct.Struct("<B31s")  # policy, glob
NODEMAP_RANGE = struct.Struct("<QQ")  # start nid, end nid (nid4)
NODEMAP_FILESET_HDR = struct.Struct("<B")
NODEMAP_FILESET_FRAG = struct.Struct("<28sH")
assert NODEMAP_CLUSTER.size == NODEMAP_REC_SIZE

# struct llogd_body, 48 bytes: llog_logid (ost_id + ogen) + indices
LLOGD_BODY = WireStruct(
    "<16sIIIIIIQ",
    (
        "lgd_oi",
        "lgd_ogen",
        "lgd_ctxt_idx",
        "lgd_llh_flags",
        "lgd_index",
        "lgd_saved_index",
        "lgd_len",
        "lgd_cur_offset",
    ),
)
assert LLOGD_BODY.size == 48

# struct llog_rec_hdr and llog_rec_tail
LLOG_REC_HDR = struct.Struct("<IIII")  # lrh_len, lrh_index, lrh_type, lrh_id
LLOG_REC_TAIL = struct.Struct("<II")  # lrt_len, lrt_index
# fixed part of struct llog_log_hdr; the bitmap lives at llh_bitmap_offset
LLOG_LOG_HDR = WireStruct(
    "<IIIIqIIIII40s",
    (
        "lrh_len",
        "lrh_index",
        "lrh_type",
        "lrh_id",
        "llh_timestamp",
        "llh_count",
        "llh_bitmap_offset",
        "llh_size",
        "llh_flags",
        "llh_cat_idx",
        "llh_tgtuuid",
    ),
)

# struct lustre_cfg header, 32 bytes, followed by lcfg_buflens[]
LUSTRE_CFG = WireStruct(
    "<IIIIQII",
    (
        "lcfg_version",
        "lcfg_command",
        "lcfg_num",
        "lcfg_flags",
        "lcfg_nid",
        "lcfg_nal",
        "lcfg_bufcount",
    ),
)

# struct cfg_marker, 160 bytes
CFG_MARKER = WireStruct(
    "<IIIIqq64s64s",
    (
        "cm_step",
        "cm_flags",
        "cm_vers",
        "cm_padding",
        "cm_createtime",
        "cm_canceltime",
        "cm_tgtname",
        "cm_comment",
    ),
)
assert CFG_MARKER.size == 160


def round8(n):
    """Round a length up to a multiple of 8."""
    return (n + 7) & ~7


ENOTSUPP = 524  # kernel only, so not in the errno module


def pad8(b):
    """Pad bytes with NULs to a multiple of 8."""
    return b.ljust(round8(len(b)), b"\0")


def errname(status):
    """Symbolic name of a negative errno status, or the number as text."""
    if status == -ENOTSUPP:
        return "ENOTSUPP"  # kernel only; what an MDT has for a non-DNE client
    try:
        return errno.errorcode[-status]
    except KeyError:
        return str(status)


# ---------------------------------------------------------------------------
# NIDs (nid4 form: net type / number in the high 32 bits, IPv4 in the low)
# ---------------------------------------------------------------------------
def mknid(ip, net_type=SOCKLND, net_num=0):
    """Build a nid4 from a dotted IPv4 address and an LNet network."""
    addr = struct.unpack("!I", socket.inet_aton(ip))[0]
    return (((net_type << 16) | net_num) << 32) | addr


LNET_NID_LO_0 = mknid("0.0.0.0", LOLND, 0)


def nidstr(nid):
    """A nid4 in its usual text form, like libcfs_nid2str()."""
    if nid == LNET_NID_ANY:
        return "<?>"
    net, addr = nid >> 32, nid & 0xFFFFFFFF
    ntype, nnum = net >> 16, net & 0xFFFF
    if ntype == LOLND:
        return f"{addr}@lo"
    name = {SOCKLND: "tcp"}.get(ntype, f"net{ntype}")
    if nnum:
        name += str(nnum)
    return f"{socket.inet_ntoa(struct.pack('!I', addr))}@{name}"


def nid16str(data, offset=0):
    """A struct lnet_nid (size, type, be16 num, be32 addr) as text."""
    size, lnd, num, addr, _ = LNET_NID16.unpack_from(data, offset)
    if size:
        return f"<{size + 8}-byte nid>"
    net = (lnd << 16) | int.from_bytes(num, "big")
    return nidstr((net << 32) | int.from_bytes(addr, "big"))


# ---------------------------------------------------------------------------
# LNet message header
# ---------------------------------------------------------------------------
class LNetMsg:
    """An lnet_hdr_nid4 plus payload."""

    def __init__(
        self, type_, dest_nid, src_nid, dest_pid, src_pid, payload=b"", **fields
    ):
        self.type = type_
        self.dest_nid = dest_nid
        self.src_nid = src_nid
        self.dest_pid = dest_pid
        self.src_pid = src_pid
        self.payload = payload
        self.payload_len = len(payload)  # parse_hdr(): what is still to read
        self.fields = fields

    def pack_hdr(self):
        """Wire form of the header; only PUT, REPLY and ACK can be sent."""
        common = LNET_HDR_COMMON.pack(
            self.dest_nid,
            self.src_nid,
            self.dest_pid,
            self.src_pid,
            self.type,
            len(self.payload),
        )
        f = self.fields
        if self.type == LNET_MSG_PUT:
            u = LNET_PUT.pack(
                f["ack_wmd_if"],
                f["ack_wmd_obj"],
                f["match_bits"],
                f.get("hdr_data", 0),
                f["ptl_index"],
                f.get("offset", 0),
            )
        elif self.type == LNET_MSG_REPLY:
            u = LNET_REPLY.pack(f["dst_wmd_if"], f["dst_wmd_obj"])
        elif self.type == LNET_MSG_ACK:
            u = LNET_ACK.pack(
                f["dst_wmd_if"], f["dst_wmd_obj"], f["match_bits"], f["mlength"]
            )
        else:
            raise ValueError(f"cannot pack LNet msg type {self.type}")
        return common + u.ljust(LNET_HDR_UNION_LEN, b"\0")

    @classmethod
    def parse_hdr(cls, data):
        """Build a message from a wire header; the payload is read later."""
        dest_nid, src_nid, dest_pid, src_pid, type_, payload_len = (
            LNET_HDR_COMMON.unpack_from(data)
        )
        u = data[LNET_HDR_COMMON.size : LNET_HDR_LEN]
        fields = {}
        if type_ == LNET_MSG_PUT:
            (
                fields["ack_wmd_if"],
                fields["ack_wmd_obj"],
                fields["match_bits"],
                fields["hdr_data"],
                fields["ptl_index"],
                fields["offset"],
            ) = LNET_PUT.unpack_from(u)
        elif type_ == LNET_MSG_GET:
            (
                fields["return_wmd_if"],
                fields["return_wmd_obj"],
                fields["match_bits"],
                fields["ptl_index"],
                fields["src_offset"],
                fields["sink_length"],
            ) = LNET_GET.unpack_from(u)
        elif type_ == LNET_MSG_REPLY:
            fields["dst_wmd_if"], fields["dst_wmd_obj"] = LNET_REPLY.unpack_from(u)
        msg = cls(type_, dest_nid, src_nid, dest_pid, src_pid, b"", **fields)
        msg.payload_len = payload_len
        return msg

    def __str__(self):
        name = LNET_MSG_NAMES.get(self.type, self.type)
        src = f"{nidstr(self.src_nid)}:{self.src_pid}"
        dest = f"{nidstr(self.dest_nid)}:{self.dest_pid}"
        text = f"{name} {src} -> {dest} len {len(self.payload)}"
        if self.type in (LNET_MSG_PUT, LNET_MSG_GET):
            portal, mbits = self.fields["ptl_index"], self.fields["match_bits"]
            text += f" portal {portal} match {mbits:#x}"
        return text


# ---------------------------------------------------------------------------
# socklnd: TCP transport, HELLO handshake and ksock_msg framing
# ---------------------------------------------------------------------------
class SockLND:
    """One socklnd connection to a server: all the LNet this client has."""

    def __init__(self, server_ip, port=LNET_ACCEPTOR_PORT, timeout=DEFAULT_TIMEOUT):
        self.server_ip = server_ip
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.nid = None
        self.pid = LNET_PID_LUSTRE
        self.peer_nid = mknid(server_ip)
        self.peer_pid = LNET_PID_LUSTRE
        self.incarnation = time.time_ns()
        self.peer_incarnation = None
        self.expected = set()  # match_bits somebody is waiting for
        self.pending = {}  # match_bits -> payload of PUTs we have not consumed
        self.bulk = {}  # match_bits -> data landed on a bulk portal
        self.rx_bytes = 0  # total received, to tell a clean timeout apart

    # -- raw socket helpers -------------------------------------------------
    def _connect_from_reserved_port(self):
        """Connect from a free port in 512-1023, as the acceptor demands."""
        last = None
        for port in RESERVED_PORTS:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("", port))
            except PermissionError:
                s.close()
                raise ConnectionError(
                    "cannot bind a privileged source port (512-1023): the "
                    "LNet acceptor on the server refuses connections from "
                    "unprivileged ports (accept=secure); run as root"
                ) from None
            except OSError as e:
                s.close()
                if e.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
                    last = e
                    continue
                raise
            try:
                s.connect((self.server_ip, self.port))
            except OSError as e:
                s.close()
                # the port is free here but the 4-tuple is not (TIME_WAIT):
                # lnet_connect() moves on to the next port as well
                if e.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
                    last = e
                    continue
                raise
            log.debug("connected %s:%d -> %s:%d", *s.getsockname(), *s.getpeername())
            return s
        raise ConnectionError(f"no free reserved port: {last}")

    def _send(self, data):
        """Send all of data."""
        if self.sock is None:
            raise ConnectionError("not connected")
        self.sock.sendall(data)

    def _recv(self, n):
        """Receive exactly n bytes."""
        if self.sock is None:
            raise ConnectionError("not connected")
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("peer closed the connection")
            self.rx_bytes += len(chunk)
            buf += chunk
        return bytes(buf)

    # -- connection setup ---------------------------------------------------
    def connect(self):
        """Open the connection: acceptor connreq, then the HELLO exchange."""
        self.sock = self._connect_from_reserved_port()
        self.nid = mknid(self.sock.getsockname()[0])

        # lnet_acceptor(): every connection starts with a connreq naming
        # the NID we want to reach on the server.
        self._send(
            ACCEPTOR_CONNREQ.pack(
                LNET_PROTO_ACCEPTOR_MAGIC, LNET_PROTO_ACCEPTOR_VERSION, self.peer_nid
            )
        )

        # ksocknal_send_hello_v2() format, used by protocol V2 and V3.
        # The passive side types its connection as the inverse of what we
        # claim (ksocknal_invert_type). A kernel client opens CONTROL,
        # BULK_IN and BULK_OUT connections; we open a single CONTROL one.
        # It must not be ANY: ksocknal_match_tx_v3() never selects an ANY
        # connection for sending, so the server could never reply.
        hello = KSOCK_HELLO_NID4.pack(
            LNET_PROTO_MAGIC,
            KSOCK_PROTO_V3,
            self.nid,
            self.peer_nid,
            self.pid,
            self.peer_pid,
            self.incarnation,
            0,
            SOCKLND_CONN_CONTROL,
            0,
        )
        self._send(hello)

        # The passive side answers with its own HELLO.
        data = self._recv(KSOCK_HELLO_NID4.size)
        (
            magic,
            version,
            src_nid,
            _dst_nid,
            src_pid,
            _dst_pid,
            src_inc,
            _dst_inc,
            _ctype,
            nips,
        ) = KSOCK_HELLO_NID4.unpack(data)
        if magic != LNET_PROTO_MAGIC:
            raise ConnectionError(f"bad HELLO magic {magic:#x}")
        if version != KSOCK_PROTO_V3:
            raise ConnectionError(
                f"server wants socklnd protocol V{version}, this "
                "client only speaks V3"
            )
        if nips:
            self._recv(4 * nips)
        if src_nid != self.peer_nid:
            log.warning(
                "server claims NID %s, expected %s",
                nidstr(src_nid),
                nidstr(self.peer_nid),
            )
        self.peer_nid = src_nid
        self.peer_pid = src_pid
        self.peer_incarnation = src_inc
        log.info(
            "LNet: %s:%d <-> %s:%d, socklnd V%d, server incarnation %d",
            nidstr(self.nid),
            self.pid,
            nidstr(self.peer_nid),
            self.peer_pid,
            version,
            src_inc,
        )

    def close(self):
        """Shut the connection down; safe to call more than once."""
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    # -- sending ------------------------------------------------------------
    def _send_lnet(self, msg):
        """Send one LNet message framed as a KSOCK_MSG_LNET."""
        hdr = KSOCK_MSG_HDR.pack(KSOCK_MSG_LNET, 0, 0, 0)
        log.debug("send %s", msg)
        self._send(hdr + msg.pack_hdr() + msg.payload)

    def send_noop(self, zc_cookie):
        """KSOCK_MSG_NOOP; with a cookie in [1] it is a zero-copy ACK."""
        self._send(KSOCK_MSG_HDR.pack(KSOCK_MSG_NOOP, 0, 0, zc_cookie))

    def put(self, portal, match_bits, payload, hdr_data=0):
        """LNetPut() with LNET_NOACK_REQ, like ptl_send_buf()."""
        msg = LNetMsg(
            LNET_MSG_PUT,
            self.peer_nid,
            self.nid,
            self.peer_pid,
            self.pid,
            payload,
            ack_wmd_if=LNET_WIRE_HANDLE_COOKIE_NONE,
            ack_wmd_obj=LNET_WIRE_HANDLE_COOKIE_NONE,
            match_bits=match_bits,
            hdr_data=hdr_data,
            ptl_index=portal,
            offset=0,
        )
        self._send_lnet(msg)

    def _ack_put(self, put):
        """LNET_MSG_ACK for a PUT sent with LNET_ACK_REQ (bulk transfers).

        lnet_finalize() builds it from the PUT's ack_wmd and match bits;
        the server's bulk completes on the ACK event, so without it the
        MDS would never send the READPAGE reply.
        """
        msg = LNetMsg(
            LNET_MSG_ACK,
            put.src_nid,
            self.nid,
            put.src_pid,
            self.pid,
            b"",
            dst_wmd_if=put.fields["ack_wmd_if"],
            dst_wmd_obj=put.fields["ack_wmd_obj"],
            match_bits=put.fields["match_bits"],
            mlength=len(put.payload),
        )
        self._send_lnet(msg)

    def _reply_to_get(self, get, payload):
        """LNET_MSG_REPLY to a GET, trimmed to its sink length."""
        payload = payload[: get.fields["sink_length"]]
        msg = LNetMsg(
            LNET_MSG_REPLY,
            get.src_nid,
            self.nid,
            get.src_pid,
            self.pid,
            payload,
            dst_wmd_if=get.fields["return_wmd_if"],
            dst_wmd_obj=get.fields["return_wmd_obj"],
        )
        self._send_lnet(msg)

    def _ping_info(self):
        """lnet_ping_info describing this node: 0@lo first, then our NID."""
        nis = [(LNET_NID_LO_0, LNET_NI_STATUS_UP, 0), (self.nid, LNET_NI_STATUS_UP, 0)]
        return LNET_PING_INFO.pack(
            LNET_PROTO_PING_MAGIC, LNET_PING_FEAT_NI_STATUS, self.pid, len(nis)
        ) + b"".join(LNET_NI_STATUS.pack(*ni) for ni in nis)

    # -- receiving ----------------------------------------------------------
    def recv_msg(self):
        """Read one ksock_msg. Returns an LNetMsg, or None for a NOOP.

        A timeout before the first byte is harmless and is passed on. One
        in the middle of a message leaves the stream out of step, so the
        connection is closed rather than parsed from a random offset.
        """
        start = self.rx_bytes
        try:
            return self._recv_msg()
        except socket.timeout:
            if self.rx_bytes == start:
                raise
            self.close()
            raise ConnectionError(
                "timed out in the middle of a message, connection closed"
            ) from None

    def _recv_msg(self):
        """recv_msg() without the timeout handling."""
        ktype, _csum, zc0, zc1 = KSOCK_MSG_HDR.unpack(self._recv(KSOCK_MSG_HDR.size))
        if ktype == KSOCK_MSG_NOOP:
            # keepalive pings and ZC-ACKs for things we never sent
            log.debug("recv NOOP zc %d/%d", zc0, zc1)
            return None
        if ktype != KSOCK_MSG_LNET:
            raise ConnectionError(f"unknown ksock_msg type {ktype:#x}")
        msg = LNetMsg.parse_hdr(self._recv(LNET_HDR_LEN))
        msg.payload = self._recv(msg.payload_len)
        log.debug("recv %s", msg)
        if zc0:
            # sender asked for a zero-copy ACK once we have the payload
            self.send_noop(zc0)
        return msg

    def dispatch(self, msg):
        """Handle unsolicited traffic; stash PUTs for the RPC layer."""
        if msg.type == LNET_MSG_PUT:
            mbits = msg.fields["match_bits"]
            if mbits not in self.expected:
                # the reply or bulk of a request we gave up on
                log.debug("dropping PUT for xid %#x, nobody waits for it", mbits)
            elif msg.fields["ptl_index"] in BULK_PORTALS:
                self.bulk.setdefault(mbits, bytearray()).extend(msg.payload)
            else:
                self.pending[mbits] = msg.payload
            if (
                msg.fields["ack_wmd_if"] != LNET_WIRE_HANDLE_COOKIE_NONE
                or msg.fields["ack_wmd_obj"] != LNET_WIRE_HANDLE_COOKIE_NONE
            ):
                self._ack_put(msg)
        elif (
            msg.type == LNET_MSG_GET
            and msg.fields["ptl_index"] == LNET_RESERVED_PORTAL
            and msg.fields["match_bits"] == LNET_PROTO_PING_MATCHBITS
        ):
            log.info("LNet: answering ping from %s", nidstr(msg.src_nid))
            self._reply_to_get(msg, self._ping_info())
        else:
            log.warning("ignoring unexpected %s", msg)

    def wait_put(self, match_bits, timeout=None):
        """Block until a PUT with these match bits arrives (an RPC reply)."""
        timeout = timeout or self.timeout
        deadline = time.monotonic() + timeout
        self.expected.add(match_bits)
        try:
            while match_bits not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no reply for xid {match_bits:#x}")
                self.sock.settimeout(remaining)
                try:
                    msg = self.recv_msg()
                except socket.timeout:
                    raise TimeoutError(
                        f"no reply for xid {match_bits:#x} within {timeout:.0f}s"
                    ) from None
                if msg is not None:
                    self.dispatch(msg)
        except BaseException:
            self.bulk.pop(match_bits, None)
            raise
        finally:
            self.expected.discard(match_bits)
            # the shrinking per-read timeout must not outlive this wait
            if self.sock:
                self.sock.settimeout(self.timeout)
        return self.pending.pop(match_bits)

    def idle(self, seconds):
        """Serve unsolicited traffic (LNet pings, keepalives) for a while."""
        deadline = time.monotonic() + seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.sock.settimeout(remaining)
                try:
                    msg = self.recv_msg()
                except socket.timeout:
                    return
                if msg is not None:
                    self.dispatch(msg)
        finally:
            if self.sock:
                self.sock.settimeout(self.timeout)

    def take_bulk(self, match_bits):
        """Data a server bulk PUT deposited for this request, if any."""
        return bytes(self.bulk.pop(match_bits, b""))


# ---------------------------------------------------------------------------
# PtlRPC: lustre_msg_v2 framing and the request/reply cycle
# ---------------------------------------------------------------------------
def lustre_msg_pack(
    bufs, flags=MSGHDR_AT_SUPPORT | MSGHDR_CKSUM_INCOMPAT18, repsize=8192
):
    """lustre_init_msg_v2(): 32 byte header, buflens, 8-aligned buffers."""
    hdr = LUSTRE_MSG_HDR.pack(
        len(bufs), 0, LUSTRE_MSG_MAGIC_V2, repsize, 0, flags, 0, 0
    )
    hdr += struct.pack(f"<{len(bufs)}I", *[len(b) for b in bufs])
    return pad8(hdr) + b"".join(pad8(b) for b in bufs)


class ProtocolError(ValueError):
    """The server sent something this client cannot make sense of."""


class MsgBufs(list):
    """The buffers of a lustre_msg; a missing one is a ProtocolError.

    Servers leave buffers out of a reply rather than send them empty,
    so the code after an RPC indexes the ones it needs and relies on
    this to report a reply that came back short.
    """

    def __getitem__(self, i):
        try:
            return super().__getitem__(i)
        except IndexError:
            raise ProtocolError(
                f"lustre_msg has {len(self)} buffers, no buffer {i}"
            ) from None


def lustre_msg_unpack(data):
    """Inverse of lustre_msg_pack(); returns the buffers as a MsgBufs."""
    if len(data) < LUSTRE_MSG_HDR.size:
        raise ProtocolError(f"lustre_msg too short ({len(data)} bytes)")
    bufcount, _, magic = LUSTRE_MSG_HDR.unpack_from(data)[:3]
    if magic != LUSTRE_MSG_MAGIC_V2:
        raise ProtocolError(f"bad lustre_msg magic {magic:#x}")
    if len(data) < LUSTRE_MSG_HDR.size + 4 * bufcount:
        raise ProtocolError("lustre_msg truncated in its buffer lengths")
    lens = struct.unpack_from(f"<{bufcount}I", data, LUSTRE_MSG_HDR.size)
    off = round8(LUSTRE_MSG_HDR.size + 4 * bufcount)
    bufs = MsgBufs()
    for n in lens:
        if off + n > len(data):
            raise ProtocolError(f"lustre_msg truncated in buffer {len(bufs)}")
        bufs.append(data[off : off + n])
        off += round8(n)
    return bufs


class RpcError(Exception):
    """A reply with a negative pb_status or of type PTL_RPC_MSG_ERR."""

    def __init__(self, opc, status):
        super().__init__(f"opcode {opc} failed: {errname(status)} ({status})")
        self.status = status


@dataclasses.dataclass
class Target:
    """One import: what a kernel client calls an obd_import to a target."""

    name: str
    portal: int
    connect_opc: int
    disconnect_opc: int
    ocd_flags: int
    statfs_opc: Optional[int] = None  # None: no statfs (the MGS)
    statfs_version: Optional[int] = None
    ocd_extra: dict = dataclasses.field(default_factory=dict)
    handle: int = 0
    conn_cnt: int = 1
    connected: bool = False
    server_ocd: Optional[dict] = None

    @property
    def index(self):
        """The target's index; only OSTs carry one in their connect data."""
        return self.ocd_extra.get("ocd_index")


@dataclasses.dataclass
class Component:
    """One instantiated layout component: an extent and its striping."""

    start: int
    end: int
    stripe_size: int
    stripe_count: int
    pattern: int
    objects: list  # (ost_id bytes, OST index) per stripe
    mirror: int = 0
    flags: int = 0


@dataclasses.dataclass
class FuseNode:
    """What the FUSE layer caches per path."""

    fid: bytes
    attrs: dict  # mdt_body
    size: int
    comps: Optional[list] = None  # None: no readable layout
    sizes: dict = dataclasses.field(default_factory=dict)  # by object id


class LustreClient:
    """PtlRPC client: the RPCs of a mount, of the namespace and of file IO."""

    def __init__(self, lnd, cluuid=None, version=LUSTRE_VERSION_DEFAULT):
        self.lnd = lnd
        self.cluuid = cluuid or str(uuid.uuid4())
        self.version = obd_ocd_version(*version)
        # ptlrpc_init_xid() seeds from the wall clock so xids are unique
        # across client restarts.
        self.xid = int(time.time()) << 24
        self.jobid = b"lustre_tcp_client.py"
        self.ost_reads = 0  # OST_READ RPCs sent, for the copy summary

    def next_xid(self):
        """The next request xid, which is also the reply's match bits."""
        self.xid += 1
        return self.xid

    def rpc(self, target, version, opc, bufs=(), op_flags=0, portal=None):
        """Send one request and return (ptlrpc_body dict, reply buffers)."""
        rpb, rbufs, _ = self._call(target, version, opc, bufs, op_flags, portal)
        return rpb, rbufs

    def rpc_bulk(self, target, version, opc, bufs=(), portal=None):
        """Like rpc() for a request the server answers with bulk data.

        The server PUTs the data on a bulk portal tagged with the request
        xid before it replies. Returns (ptlrpc_body dict, reply buffers,
        data), the data being b"" if none arrived.
        """
        return self._call(target, version, opc, bufs, 0, portal, bulk=True)

    def _call(self, target, version, opc, bufs, op_flags, portal, bulk=False):
        """The request/reply cycle behind rpc() and rpc_bulk()."""
        xid = self.next_xid()
        portal = target.portal if portal is None else portal
        pb = PTLRPC_BODY.pack(
            pb_handle=target.handle,
            pb_type=PTL_RPC_MSG_REQUEST,
            pb_version=version | PTLRPC_MSG_VERSION,
            pb_opc=opc,
            pb_op_flags=op_flags,
            pb_conn_cnt=target.conn_cnt,
            pb_timeout=OBD_TIMEOUT_DEFAULT,
            pb_uid=os.getuid(),
            pb_gid=os.getgid(),
            pb_jobid=self.jobid,
        )
        req = lustre_msg_pack([pb] + list(bufs))
        log.debug(
            "RPC x%x opc %d -> %s portal %d (%d bytes)",
            xid,
            opc,
            target.name,
            portal,
            len(req),
        )
        # Requests carry the xid as match bits, and the server replies
        # with a PUT matching the same xid (ptl_send_rpc / ptlrpc_send_reply).
        # Without OBD_CONNECT_BULK_MBITS a server bulk PUT uses the xid too.
        self.lnd.put(portal, xid, req)
        reply = self.lnd.wait_put(xid)
        rbufs = lustre_msg_unpack(reply)
        rpb = PTLRPC_BODY.unpack(rbufs[0])
        if rpb["pb_type"] not in (PTL_RPC_MSG_REPLY, PTL_RPC_MSG_ERR):
            raise ProtocolError(f"unexpected reply type {rpb['pb_type']}")
        # collect the bulk even on failure so it does not sit in the LND
        data = self.lnd.take_bulk(xid) if bulk else b""
        # a positive status is not an error: OST_READ returns the byte count
        if rpb["pb_status"] < 0 or rpb["pb_type"] == PTL_RPC_MSG_ERR:
            raise RpcError(opc, rpb["pb_status"])
        return rpb, rbufs, data

    # -- the mount side -----------------------------------------------------
    def connect(self, target):
        """*_CONNECT: RQF_CONNECT = pb, tgtuuid, cluuid, conn, connect_data."""
        ocd = OBD_CONNECT_DATA.pack(
            ocd_connect_flags=target.ocd_flags,
            ocd_version=self.version,
            **target.ocd_extra,
        )
        bufs = [
            target.name.encode() + b"\0",
            self.cluuid.encode() + b"\0",
            struct.pack("<Q", 0),  # lustre_handle conn (unused)
            ocd,
        ]
        rpb, rbufs = self.rpc(
            target,
            LUSTRE_OBD_VERSION,
            target.connect_opc,
            bufs,
            op_flags=MSG_CONNECT_INITIAL,
        )
        target.handle = rpb["pb_handle"]
        target.connected = True
        srv = OBD_CONNECT_DATA.unpack(rbufs[1])
        target.server_ocd = srv
        flags = []
        if rpb["pb_op_flags"] & MSG_CONNECT_RECONNECT:
            flags.append("reconnect")
        if rpb["pb_op_flags"] & MSG_CONNECT_RECOVERING:
            flags.append("recovering")
        if rpb["pb_op_flags"] & MSG_CONNECT_REPLAYABLE:
            flags.append("replayable")
        log.info(
            "%s: connected, export handle %#x, server %s, connect_flags %#x%s",
            target.name,
            target.handle,
            ocd_version_str(srv["ocd_version"]),
            srv["ocd_connect_flags"],
            f" [{', '.join(flags)}]" if flags else "",
        )
        return srv

    def ping(self, target):
        """OBD_PING: ptlrpc_body only."""
        self.rpc(target, LUSTRE_OBD_VERSION, OBD_PING)
        log.info("%s: OBD_PING ok", target.name)

    def statfs(self, target):
        """*_STATFS: the reply carries an obd_statfs; returns it as a dict."""
        _, rbufs = self.rpc(target, target.statfs_version, target.statfs_opc)
        st = OBD_STATFS.unpack(rbufs[1])
        bs = st["os_bsize"]
        log.info(
            "%s: statfs %s total, %s free, %s avail, %d/%d inodes free, state %#x",
            target.name,
            human(st["os_blocks"] * bs),
            human(st["os_bfree"] * bs),
            human(st["os_bavail"] * bs),
            st["os_ffree"],
            st["os_files"],
            st["os_state"],
        )
        return st

    # -- the umount side ----------------------------------------------------
    def disconnect(self, target):
        """*_DISCONNECT: ptlrpc_body only, addressed by the export handle."""
        self.rpc(target, LUSTRE_OBD_VERSION, target.disconnect_opc)
        target.connected = False
        log.info("%s: disconnected", target.name)

    # -- namespace: get_root, lookup, readdir -------------------------------
    def mdt_body(self, **kw):
        """An mdt_body carrying our credentials, like __mdc_pack_body().

        With the null security flavour the MDT takes uid/gid from the body
        (old_init_ucred), mapped through the export's nodemap.
        """
        uid, gid = os.getuid(), os.getgid()
        fields = {
            "mb_uid": uid,
            "mb_gid": gid,
            "mb_fsuid": uid,
            "mb_fsgid": gid,
            "mb_capability": 0xFFFFFFFF if uid == 0 else 0,
            "mb_suppgid": 0xFFFFFFFF,
        }
        fields.update(kw)
        return MDT_BODY.pack(**fields)

    def get_root(self, mdts):
        """MDS_GET_ROOT: pb, mdt_body, name (empty: no fileset)."""
        _, rbufs = self.rpc(
            mdts.root, LUSTRE_MDS_VERSION, MDS_GET_ROOT, [self.mdt_body(), b""]
        )
        body = MDT_BODY.unpack(rbufs[1])
        return body["mb_fid1"]

    def fld_lookup(self, mdt, seq):
        """FLD_QUERY: pb, fld_op, lu_seq_range; the MDT index of a sequence.

        What fld_client_rpc() sends: the range has lsr_start set to the
        sequence and the type to look for in lsr_flags, and comes back
        filled in. Any MDT answers for the whole filesystem.
        """
        _, rbufs = self.rpc(
            mdt,
            LUSTRE_MDS_VERSION,
            FLD_QUERY,
            [
                struct.pack("<I", FLD_LOOKUP),
                LU_SEQ_RANGE.pack(seq, 0, 0, LU_SEQ_RANGE_MDT),
            ],
            portal=FLD_REQUEST_PORTAL,
        )
        if len(rbufs) < 2 or len(rbufs[1]) < LU_SEQ_RANGE.size:
            raise ProtocolError("FLD_QUERY reply without a lu_seq_range")
        start, end, index, _ = LU_SEQ_RANGE.unpack(rbufs[1][: LU_SEQ_RANGE.size])
        log.debug("fld: seq %#x is in [%#x-%#x) on MDT%04x", seq, start, end, index)
        return index

    def dir_stripes(self, mdts, fid):
        """The stripe FIDs of a striped directory, or None if it is plain.

        MDS_GETATTR with OBD_MD_MEA has mdt_getattr_internal() put the LMV
        EA in the mdt_md buffer. Only the master object, which is what a
        lookup by name comes up with, has an lmv_mds_md_v1 with the FIDs.
        """
        cached = mdts.cached_stripes(fid)
        if cached is not False:
            return cached
        body = self.mdt_body(
            mb_fid1=fid,
            mb_valid=OBD_MD_GETATTR | OBD_MD_FLDIREA | OBD_MD_MEA,
            mb_eadatasize=LMV_EA_BUFSIZE,
        )
        _, rbufs = self.rpc(
            mdts.for_fid(fid), LUSTRE_MDS_VERSION, MDS_GETATTR, [body, b""]
        )
        attrs = MDT_BODY.unpack(rbufs[1])
        lmv = None
        if attrs["mb_valid"] & OBD_MD_MEA and len(rbufs) > 2:
            lmv = parse_lmv(rbufs[2][: attrs["mb_eadatasize"]])
        if lmv is not None:
            log.debug(
                "%s: %d stripes, hash type %#x",
                fidstr(fid),
                len(lmv.fids),
                lmv.hash_type,
            )
        mdts.remember_stripes(fid, lmv)
        return lmv

    def _getattr_name(self, mdts, parent_fid, name, layout):
        """MDS_GETATTR_NAME where the name is, MDS_GETATTR where the object is.

        In a striped directory the name hashes to one stripe, which is the
        parent to ask; the other stripes are tried after it, as that is
        where a directory being migrated or restriped still has the name.
        A reply with OBD_MD_MDS carries nothing but the FID of an object
        on another MDT, which has the attributes (lmv_intent_remote()).
        Returns (mdt_body, raw lov EA or b"").
        """
        valid = OBD_MD_GETATTR
        extra = {}
        if layout:
            valid |= OBD_MD_FLEASIZE
            extra["mb_eadatasize"] = LOV_EA_BUFSIZE
        lmv = self.dir_stripes(mdts, parent_fid)
        parents = [parent_fid] if lmv is None else lmv.parents_of(name)
        missing = None
        for pfid in parents:
            body = self.mdt_body(mb_fid1=pfid, mb_valid=valid, **extra)
            try:
                _, rbufs = self.rpc(
                    mdts.for_fid(pfid),
                    LUSTRE_MDS_VERSION,
                    MDS_GETATTR_NAME,
                    [body, b"", name.encode() + b"\0"],
                )
            except RpcError as e:
                if e.status != -errno.ENOENT:
                    raise
                missing = e
                continue
            attrs = MDT_BODY.unpack(rbufs[1])
            if pfid != parents[0]:
                log.warning(
                    "%s: not in the stripe it hashes to, found in %s",
                    name,
                    fidstr(pfid),
                )
            if attrs["mb_valid"] & OBD_MD_MDS:
                log.debug("%s: %s is on another MDT", name, fidstr(attrs["mb_fid1"]))
                return self.getattr_fid(mdts, attrs["mb_fid1"], layout=layout)
            ea = b""
            if attrs["mb_valid"] & OBD_MD_FLEASIZE and len(rbufs) > 2:
                ea = rbufs[2][: attrs["mb_eadatasize"]]
            return attrs, ea
        raise missing

    def lookup(self, mdts, parent_fid, name):
        """A name in a directory, wherever DNE has put either.

        Returns the child's mdt_body: mb_fid1 is its FID, mb_mode its type
        and permissions. The MDT takes and drops the lookup lock itself.
        """
        return self._getattr_name(mdts, parent_fid, name, False)[0]

    def getattr_layout(self, mdts, parent_fid, name):
        """lookup() asking for the layout EA as well.

        A non-zero mb_eadatasize makes mdt_getattr_internal() return the
        LOV EA in the reply's mdt_md buffer, sized by the MDT from disk.
        Returns (mdt_body, raw lov EA or b"").
        """
        return self._getattr_name(mdts, parent_fid, name, True)

    def getattr_fid(self, mdts, fid, layout=False, linkname=False):
        """MDS_GETATTR by FID: pb, mdt_body, capa.

        With layout=True the LOV EA comes back in the mdt_md buffer; with
        linkname=True the same buffer carries a symlink's target instead
        (mdt_getattr() sizes it to PATH_MAX). Returns (mdt_body, buffer).
        """
        valid = OBD_MD_GETATTR
        extra = {}
        if layout:
            valid |= OBD_MD_FLEASIZE
            extra["mb_eadatasize"] = LOV_EA_BUFSIZE
        if linkname:
            valid |= OBD_MD_LINKNAME
            extra["mb_eadatasize"] = PATH_MAX + 1
        body = self.mdt_body(mb_fid1=fid, mb_valid=valid, **extra)
        _, rbufs = self.rpc(
            mdts.for_fid(fid), LUSTRE_MDS_VERSION, MDS_GETATTR, [body, b""]
        )
        attrs = MDT_BODY.unpack(rbufs[1])
        buf = b""
        if len(rbufs) > 2 and attrs["mb_valid"] & (OBD_MD_FLEASIZE | OBD_MD_LINKNAME):
            buf = rbufs[2][: attrs["mb_eadatasize"]]
        return attrs, buf

    def read_range(self, osts, comps, sizes, offset, size):
        """Read [offset, offset+size) of a file described by its layout.

        The units of one stripe object inside a file range are contiguous
        in the object, so each object is read as one range (in READ_CHUNK
        pieces) and scattered into place. sizes caches object sizes by
        object id; holes and reads past an object's end come back as
        zeros, like a real client sees them.
        """
        out = bytearray(size)
        end = offset + size
        for comp in comps:
            lo, hi = max(offset, comp.start), min(end, comp.end)
            if lo >= hi:
                continue
            for i, (oi, idx) in enumerate(comp.objects):
                obj_lo, obj_hi = stripe_extent(comp, i, lo, hi)
                if obj_lo >= obj_hi:
                    continue
                ost = stripe_ost(osts, idx)
                if oi not in sizes:
                    sizes[oi] = self.object_size(ost, oi)
                obj_hi = min(obj_hi, sizes[oi])
                pos = obj_lo
                while pos < obj_hi:
                    want = min(READ_CHUNK, obj_hi - pos)
                    data = self.read_object(ost, oi, pos, want)[:want]
                    if not data:
                        break
                    scatter_stripe(out, offset, comp, i, pos, data)
                    pos += len(data)
        return bytes(out)

    def file_size(self, osts, comps, sizes):
        """Size of a file from its stripe objects, like lov_merge_lvb()."""
        size = 0
        for comp in comps:
            ss, sc = comp.stripe_size, comp.stripe_count
            for i, (oi, idx) in enumerate(comp.objects):
                ost = stripe_ost(osts, idx)
                if oi not in sizes:
                    sizes[oi] = self.object_size(ost, oi)
                osz = sizes[oi]
                if osz == 0:
                    continue
                # lov_stripe_size(): object offsets count from the start
                # of the file, not of the component, in every component
                last = osz - 1
                end = (last // ss) * ss * sc + i * ss + last % ss + 1
                size = max(size, min(end, comp.end))
        return size

    def resolve(self, mdts, path):
        """Walk a path from the root; returns (parent fid, last name)."""
        fid = self.get_root(mdts)
        comps = [c for c in path.split("/") if c]
        if not comps:
            raise IsADirectoryError(path)
        for comp in comps[:-1]:
            body = self.lookup(mdts, fid, comp)
            if not stat.S_ISDIR(body["mb_mode"]):
                raise NotADirectoryError(comp)
            fid = body["mb_fid1"]
        return fid, comps[-1]

    def object_size(self, ost, oi):
        """OST_GETATTR of one stripe object: its size on the OST."""
        oa = OBDO.pack(o_valid=OBD_MD_FLID | OBD_MD_FLGROUP, o_oi=oi)
        _, rbufs = self.rpc(ost, LUSTRE_OST_VERSION, OST_GETATTR, [oa, b""])
        attrs = OBDO.unpack(rbufs[1])
        if not attrs["o_valid"] & OBD_MD_FLSIZE:
            raise ProtocolError("OST_GETATTR reply without a size")
        return attrs["o_size"]

    def read_object(self, ost, oi, offset, length):
        """OST_READ of one extent of one stripe object.

        Request: ptlrpc_body, obdo (o_oi names the object), obd_ioobj,
        niobuf_remote[], capa, short_io. It goes to OST_IO_PORTAL; the
        data comes back as a bulk PUT on OST_BULK_PORTAL tagged with the
        xid, which we ACK, and the reply follows. tgt_brw_read() trims
        the bulk at end of object, so a short result means EOF.
        """
        # We hold no DLM extent lock, so ask the OST to lock for us
        # (lockless IO, as the OSC does for lockless reads): with
        # OBD_BRW_SRVLOCK the request skips the high-priority lock check
        # that otherwise answers ESTALE, and tgt_brw_lock() takes a PR lock.
        oa = OBDO.pack(
            o_valid=OBD_MD_FLID | OBD_MD_FLGROUP | OBD_MD_FLFLAGS,
            o_oi=oi,
            o_flags=OBD_FL_SRVLOCK,
        )
        ioobj = OBD_IOOBJ.pack(oi, 0, 1)  # one MD, one niobuf
        rnb = NIOBUF_REMOTE.pack(offset, length, OBD_BRW_SRVLOCK)
        rpb, _, data = self.rpc_bulk(
            ost,
            LUSTRE_OST_VERSION,
            OST_READ,
            [oa, ioobj, rnb, b"", b""],
            portal=OST_IO_PORTAL,
        )
        self.ost_reads += 1
        # tgt_brw_read() answers with the number of bytes it put in the
        # bulk; the OSD may round a short tail up to a block, so callers
        # clamp to the object size from OST_GETATTR.
        return data[: rpb["pb_status"]]

    def copy_file(self, mdts, osts, path, dest):
        """Read a file through its layout and write it locally.

        osts is anything with a get(index) returning a connected Target,
        normally an OstPool that connects OSTs as stripes need them.
        """
        parent, name = self.resolve(mdts, path)
        attrs, ea = self.getattr_layout(mdts, parent, name)
        mode = attrs["mb_mode"]
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(path)
        if not stat.S_ISREG(mode):
            raise ValueError(f"{path}: not a regular file (mode {mode:o})")
        if not ea:
            raise ValueError(f"{path}: no layout returned by the MDT")
        comps = parse_layout(ea)
        log.info(
            "%s: fid %s, layout %s",
            path,
            fidstr(attrs["mb_fid1"]),
            ", ".join(describe_component(c) for c in comps),
        )
        sizes = {}
        size = self.file_size(osts, comps, sizes)
        # a step of READ_CHUNK per stripe keeps every OST_READ as large
        # as the OST allows while bounding the memory held at a time
        step = READ_CHUNK * max(1, *(c.stripe_count for c in comps))
        reads = self.ost_reads
        with open(dest, "wb") as out:
            for off in range(0, size, step):
                n = min(step, size - off)
                out.write(self.read_range(osts, comps, sizes, off, n))
        log.info(
            "%s: copied %d bytes to %s in %d OST_READ RPCs",
            path,
            size,
            dest,
            self.ost_reads - reads,
        )
        return size

    def readdir(self, mdts, fid):
        """The entries of a directory, of all its stripes if it has them.

        The stripes hold the names; "." and ".." are taken from the master
        object, whose other entries are the stripes themselves.
        """
        lmv = self.dir_stripes(mdts, fid)
        if lmv is None:
            return self.readdir_object(mdts.for_fid(fid), fid)
        entries = [
            e
            for e in self.readdir_object(mdts.for_fid(fid), fid)
            if e[0] in (".", "..")
        ]
        for sfid in lmv.fids:
            entries.extend(
                e
                for e in self.readdir_object(mdts.for_fid(sfid), sfid)
                if e[0] not in (".", "..")
            )
        return entries

    def readdir_object(self, mdt, fid):
        """MDS_READPAGE, repeated until the last dirpage says end.

        mdc_readdir_pack(): mb_size is the hash to start from, mb_nlink the
        number of bytes wanted, mb_mode the LUDA_* attributes. The request
        goes to MDS_READPAGE_PORTAL; the pages arrive as one bulk PUT on
        MDS_BULK_PORTAL with the request xid as match bits, and the reply
        follows once our LNet ACK reaches the server.
        """
        entries = []
        hash_start = 0
        while True:
            body = self.mdt_body(
                mb_fid1=fid,
                mb_valid=OBD_MD_FLID,
                mb_size=hash_start,
                mb_nlink=READDIR_BYTES,
                mb_mode=LUDA_FID | LUDA_TYPE,
            )
            _, _, data = self.rpc_bulk(
                mdt,
                LUSTRE_MDS_VERSION,
                MDS_READPAGE,
                [body, b""],
                portal=MDS_READPAGE_PORTAL,
            )
            if not data:
                raise ProtocolError("MDS_READPAGE reply arrived without bulk")
            page_entries, done, next_hash = parse_dirpages(data)
            log.debug(
                "readdir from hash %#x: %d bytes, %d entries, %s",
                hash_start,
                len(data),
                len(page_entries),
                "end" if done else "more",
            )
            entries.extend(page_entries)
            if done:
                return entries
            hash_start = next_hash

    def show_path(self, mdts, path):
        """'ls -a' of a path: walk it by name from the root, then list."""
        fid = self.get_root(mdts)
        log.info("%s: root fid %s", mdts.name, fidstr(fid))
        body = None
        mode = stat.S_IFDIR  # of the root
        walked = "/"
        for comp in [c for c in path.split("/") if c]:
            if not stat.S_ISDIR(mode):
                raise NotADirectoryError(walked)
            body = self.lookup(mdts, fid, comp)
            fid, mode = body["mb_fid1"], body["mb_mode"]
            walked = walked.rstrip("/") + "/" + comp
            log.debug("lookup %s -> %s mode %o", walked, fidstr(fid), mode)
        if not stat.S_ISDIR(mode):
            print(f"{format_attrs(body)} {walked}")
            return 1
        entries = self.readdir(mdts, fid)
        print(f"# {walked} {fidstr(fid)}: {len(entries)} entries")
        for name, efid, ltype in sorted(entries):
            print(f"{type_char(ltype)} {fidstr(efid):<28} {name}")
        return len(entries)

    # -- config logs --------------------------------------------------------
    def llog_open(self, mgs, logname):
        """LLOG_ORIGIN_HANDLE_CREATE: pb, llogd_body, name, mdt_body.

        Returns the logid fields to put in later llogd_body requests.
        """
        body = LLOGD_BODY.pack(lgd_ctxt_idx=LLOG_CONFIG_ORIG_CTXT)
        _, rbufs = self.rpc(
            mgs,
            LUSTRE_LOG_VERSION,
            LLOG_ORIGIN_HANDLE_CREATE,
            [body, logname.encode() + b"\0", b"\0" * MDT_BODY.size],
        )
        opened = LLOGD_BODY.unpack(rbufs[1])
        logid = {"lgd_oi": opened["lgd_oi"], "lgd_ogen": opened["lgd_ogen"]}
        log.info("%s: opened, logid %s", logname, logidstr(logid))
        return logid

    def llog_open_by_id(self, mgs, seq, oid, ogen=0):
        """LLOG_ORIGIN_HANDLE_CREATE by logid.

        llog_osd_open() locates the object by the logid whenever one is
        given and only uses the name otherwise. mgs_llog_open() however
        refuses a request without a name (it parses the filesystem name
        out of it), so "params" is sent along: it is the one name the MGS
        accepts without warning and without attaching us to a filesystem.
        """
        # struct llog_logid holds an ost_id in its oi_id/oi_seq form:
        # the object number first, the sequence second
        logid = {"lgd_oi": struct.pack("<QQ", oid, seq), "lgd_ogen": ogen}
        body = LLOGD_BODY.pack(lgd_ctxt_idx=LLOG_CONFIG_ORIG_CTXT, **logid)
        self.rpc(
            mgs,
            LUSTRE_LOG_VERSION,
            LLOG_ORIGIN_HANDLE_CREATE,
            [body, b"params\0", b"\0" * MDT_BODY.size],
        )
        return logid

    def llog_header(self, mgs, logname, logid):
        """LLOG_ORIGIN_HANDLE_READ_HEADER: reply carries llog_log_hdr.

        Returns (header dict, chunk size, last index set in the bitmap).
        """
        body = LLOGD_BODY.pack(
            lgd_ctxt_idx=LLOG_CONFIG_ORIG_CTXT,
            lgd_llh_flags=LLOG_F_IS_PLAIN,
            **logid,
        )
        _, rbufs = self.rpc(
            mgs, LUSTRE_LOG_VERSION, LLOG_ORIGIN_HANDLE_READ_HEADER, [body]
        )
        raw_hdr = rbufs[1]
        hdr = LLOG_LOG_HDR.unpack(raw_hdr)
        chunk = hdr["lrh_len"] or LLOG_MIN_CHUNK_SIZE
        bitmap = raw_hdr[hdr["llh_bitmap_offset"] : chunk - LLOG_REC_TAIL.size]
        last_index = 0
        for byte_i, byte in enumerate(bitmap):
            for bit in range(8):
                if byte & (1 << bit):
                    last_index = byte_i * 8 + bit
        log.info(
            "%s: header: %d records, last index %d, chunk %d, flags %#x, target %s",
            logname,
            hdr["llh_count"] - 1,
            last_index,
            chunk,
            hdr["llh_flags"],
            hdr["llh_tgtuuid"].split(b"\0", 1)[0].decode(errors="replace"),
        )
        return hdr, chunk, last_index

    def read_nidtbl(self, mgs, fsname):
        """MGS_CONFIG_READ of the imperative recovery NID table.

        This is what the MGC asks for as "FSNAME-cliir": every registered
        MDT and OST of the filesystem with its index, instance and NIDs.
        The entries arrive as a bulk PUT on MGS_BULK_PORTAL, one or more
        4K units, each entry padded to its mne_length. Requesting an
        mcb_rec_nid_size of 0 asks for 8-byte nid4 entries.
        Returns (table version, list of target dicts).
        """
        targets = []
        last, table_version = 0, 0
        while True:
            # mgc_process_recover_nodemap_log(): ask for the entries newer
            # than the last one seen until that is the table version
            body = MGS_CONFIG_BODY.pack(
                mcb_name=(fsname + "-cliir").encode(),
                mcb_offset=last + 1,
                mcb_type=MGS_CFG_T_RECOVER,
                mcb_rec_nid_size=0,
                mcb_bits=LU_PAGE_SHIFT,
                mcb_units=NIDTBL_PAGES,
            )
            _, rbufs, data = self.rpc_bulk(
                mgs, LUSTRE_MGS_VERSION, MGS_CONFIG_READ, [body]
            )
            seen, table_version = MGS_CONFIG_RES.unpack_from(
                rbufs[1].ljust(MGS_CONFIG_RES.size, b"\0")
            )
            log.info(
                "%s: nidtbl version %d (entries up to %d), %d bytes",
                fsname,
                table_version,
                seen,
                len(data),
            )
            targets += parse_nidtbl(data, fsname)
            if seen >= table_version:
                break
            if seen <= last or not data:
                raise ProtocolError(f"NID table read stopped at version {seen}")
            last = seen
        return table_version, targets

    def list_logs(self, mgs, fsname):
        """Registered targets from the NID table, then probe log names."""
        version, targets = self.read_nidtbl(mgs, fsname)
        print(f"# {fsname}: registered targets (nidtbl version {version})")
        for t in targets:
            print(
                f"{t['kind']} {t['name']:<20} index {t['index']:<4d} "
                f"instance {t['instance']:#010x} version {t['version']:<5d} "
                f"nids {','.join(t['nids'])}"
            )
        # The MGS has no RPC that lists logs; these are the names a
        # filesystem can have. Only FSNAME-* and "params" are probed: the
        # MGS treats the part before the last '-' as a filesystem name.
        names = [f"{fsname}-client"]
        names += [t["name"] for t in targets]
        names += [f"{fsname}-sptlrpc", f"{fsname}-params", "params"]
        print("# config logs (probed by name)")
        found = 0
        for name in names:
            try:
                logid = self.llog_open(mgs, name)
                hdr, _, last_index = self.llog_header(mgs, name, logid)
            except RpcError as e:
                print(f"{name:<24} {errname(e.status).lower()}")
                continue
            found += 1
            count = max(hdr["llh_count"] - 1, 0)
            print(f"{name:<24} {count} records, last index {last_index}")
        return found

    def probe_logs(self, mgs, count):
        """Open logids [0xa:1..count:0x0] and report what each one is.

        The MGS keeps its config logs as objects in the llog FID sequence,
        so walking the object numbers finds logs whose names cannot be
        guessed. The name is only a directory entry on the MGS, so the
        identity is inferred from the records inside.
        """
        found = 0
        print(f"# config logs probed by logid [{FID_SEQ_LLOG:#x}:1..{count}:0x0]")
        for oid in range(1, count + 1):
            label = logidstr(
                {"lgd_oi": struct.pack("<QQ", oid, FID_SEQ_LLOG), "lgd_ogen": 0}
            )
            try:
                logid = self.llog_open_by_id(mgs, FID_SEQ_LLOG, oid)
            except RpcError as e:
                if e.status != -errno.ENOENT:
                    print(f"{label:<22} {errname(e.status).lower()}")
                continue
            try:
                recs = self.read_config_log(mgs, logid)
            except RpcError as e:
                print(f"{label:<22} exists, cannot read: {errname(e.status).lower()}")
                continue
            except ProtocolError as e:
                print(f"{label:<22} exists, cannot read: {e}")
                continue
            found += 1
            hints = []
            for _, rec_type, data in recs:
                if rec_type != OBD_CFG_REC:
                    hints.append(f"rec type {rec_type:#x}")
                    continue
                cfg = LUSTRE_CFG.unpack(data)
                bufs = cfg_bufs(cfg, data)
                cmd = cfg["lcfg_command"]
                if (
                    cmd == LCFG_MARKER
                    and len(bufs) > 1
                    and len(bufs[1]) >= CFG_MARKER.size
                ):
                    m = CFG_MARKER.unpack(bufs[1])
                    hints.append(cfg_string(m["cm_tgtname"]))
                elif cmd == LCFG_MOUNTOPT and len(bufs) > 1:
                    hints.append(f"profile {cfg_string(bufs[1])}")
                elif (cmd & LCFG_NODEMAP_MASK) == LCFG_NODEMAP_BASE and bufs:
                    hints.append(f"nodemap {cfg_string(bufs[0])}")
            seen = list(dict.fromkeys(hints))
            text = f"{label:<22} {len(recs)} records"
            if seen:
                text += ": " + ", ".join(seen[:8])
            if len(seen) > 8:
                text += " ..."
            print(text)
        return found

    def find_fsnames(self, mgs, count):
        """Filesystem names served by this MGS, from its config logs.

        The MGS has no RPC that names its filesystems, but it opens a
        config log by logid without being told one (llog_open_by_id()),
        and a FSNAME-client log says whose it is in its mount_option
        record. Walks logids [0xa:1..count:0x0]; returns a sorted list.
        """
        fsnames = set()
        for oid in range(1, count + 1):
            try:
                logid = self.llog_open_by_id(mgs, FID_SEQ_LLOG, oid)
                recs = self.read_config_log(mgs, logid)
            except (RpcError, ProtocolError):
                continue
            for _, rec_type, data in recs:
                if rec_type != OBD_CFG_REC:
                    continue
                cfg = LUSTRE_CFG.unpack(data)
                bufs = cfg_bufs(cfg, data)
                if cfg["lcfg_command"] != LCFG_MOUNTOPT or len(bufs) < 2:
                    continue
                profile = cfg_string(bufs[1])
                if profile.endswith("-client") and len(profile) > len("-client"):
                    fsnames.add(profile[: -len("-client")])
        return sorted(fsnames)

    def read_nodemap(self, mgs):
        """MGS_CONFIG_READ of type NODEMAP: the whole nodemap database.

        The MGS walks its nodemap index in two passes (cluster records
        first, then everything else) and returns lu_idxpage pages over
        bulk, each holding 8-byte nodemap_key + 32-byte nodemap_rec
        entries. We resend with the returned hash offset and pass until
        the offset comes back as II_END_OFF. Returns (key, rec) tuples.
        """
        entries = []
        offset, cur_pass = 0, 0
        while True:
            body = MGS_CONFIG_BODY.pack(
                mcb_name=LUSTRE_NODEMAP_NAME.encode(),
                mcb_offset=offset,
                mcb_type=MGS_CFG_T_NODEMAP,
                mcb_rec_nid_size=cur_pass,
                mcb_bits=LU_PAGE_SHIFT,
                mcb_units=NIDTBL_PAGES,
            )
            _, rbufs, data = self.rpc_bulk(
                mgs, LUSTRE_MGS_VERSION, MGS_CONFIG_READ, [body]
            )
            offset, cur_pass = MGS_CONFIG_RES.unpack_from(
                rbufs[1].ljust(MGS_CONFIG_RES.size, b"\0")
            )
            log.debug(
                "nodemap: %d bytes, next offset %#x pass %d",
                len(data),
                offset,
                cur_pass,
            )
            for page in range(0, len(data) - LU_IDXPAGE.size + 1, LU_PAGE_SIZE):
                magic, _flags, nr, _ = LU_IDXPAGE.unpack_from(data, page)
                if magic != LIP_MAGIC:
                    raise ProtocolError(f"bad lu_idxpage magic {magic:#x}")
                pos = page + LU_IDXPAGE.size
                if pos + nr * (NODEMAP_KEY.size + NODEMAP_REC_SIZE) > len(data):
                    raise ProtocolError(f"lu_idxpage truncated: {nr} entries")
                for _ in range(nr):
                    key = NODEMAP_KEY.unpack_from(data, pos)
                    pos += NODEMAP_KEY.size
                    entries.append((key, data[pos : pos + NODEMAP_REC_SIZE]))
                    pos += NODEMAP_REC_SIZE
            if offset == II_END_OFF:
                return entries
            if not data:
                raise ProtocolError("nodemap read returned no data before end")

    def read_config_log(self, mgs, logname):
        """Fetch a config llog from the MGS the way the MGC does.

        llog_client_open() sends the log name and gets back its logid;
        llog_client_read_header() returns the 8K llog_log_hdr whose bitmap
        says which record indices exist; llog_client_next_block() then
        returns one chunk at a time, each holding whole llog records.
        Returns the list of (index, type, data) records in log order.
        """
        if isinstance(logname, dict):
            logid, logname = logname, logidstr(logname)
        else:
            logid = self.llog_open(mgs, logname)
        hdr, chunk, last_index = self.llog_header(mgs, logname, logid)

        # LLOG_ORIGIN_HANDLE_NEXT_BLOCK until every index in the bitmap has
        # been seen. Mirrors the simple part of llog_process_thread(): ask
        # for the chunk holding 'index', walk its records, carry on after
        # the last one. -EBADR / -EIO from the server mean end of log.
        records = []
        index, cur_idx, cur_offset = 1, 0, chunk
        while index <= last_index:
            body = LLOGD_BODY.pack(
                lgd_ctxt_idx=LLOG_CONFIG_ORIG_CTXT,
                lgd_llh_flags=hdr["llh_flags"],
                lgd_index=index,
                lgd_saved_index=cur_idx,
                lgd_len=chunk,
                lgd_cur_offset=cur_offset,
                **logid,
            )
            try:
                _, rbufs = self.rpc(
                    mgs, LUSTRE_LOG_VERSION, LLOG_ORIGIN_HANDLE_NEXT_BLOCK, [body]
                )
            except RpcError as e:
                if e.status in (-errno.EBADR, -errno.EIO):
                    break
                raise
            nb = LLOGD_BODY.unpack(rbufs[1])
            cur_idx, cur_offset = nb["lgd_saved_index"], nb["lgd_cur_offset"]
            block = rbufs[2]
            log.debug(
                "%s: block for index %d: %d bytes, server idx %d offset %d",
                logname,
                index,
                len(block),
                cur_idx,
                cur_offset,
            )
            first = index
            off = 0
            while off + LLOG_REC_HDR.size <= len(block):
                lrh_len, lrh_index, lrh_type, _ = LLOG_REC_HDR.unpack_from(block, off)
                if (
                    lrh_len < LLOG_REC_HDR.size + LLOG_REC_TAIL.size
                    or lrh_index == 0
                    or off + lrh_len > len(block)
                ):
                    break  # zero padding at the chunk end
                if lrh_index >= index:
                    data = block[
                        off + LLOG_REC_HDR.size : off + lrh_len - LLOG_REC_TAIL.size
                    ]
                    records.append((lrh_index, lrh_type, data))
                    index = lrh_index + 1
                off += lrh_len
            if index == first:
                log.warning("%s: no record >= %d in block, stopping", logname, index)
                break
        return records


def parse_nidtbl(data, fsname):
    """The mgs_nidtbl_entry records of one MGS_CONFIG_READ bulk as dicts."""
    targets = []
    for page in range(0, len(data), LU_PAGE_SIZE):
        off = page
        end = min(page + LU_PAGE_SIZE, len(data))
        while off + MGS_NIDTBL_ENTRY.size <= end:
            (
                version,
                instance,
                index,
                length,
                ttype,
                nid_type,
                nid_size,
                nid_count,
            ) = MGS_NIDTBL_ENTRY.unpack_from(data, off)
            if length == 0 or nid_count == 0:
                break
            if off + MGS_NIDTBL_ENTRY.size + nid_count * nid_size > end or (
                nid_size < (8 if nid_type == 0 else LNET_NID16.size)
            ):
                raise ProtocolError(
                    f"NID table entry at {off}: {nid_count} NIDs of "
                    f"{nid_size} bytes do not fit"
                )
            nids = []
            for i in range(nid_count):
                at = off + MGS_NIDTBL_ENTRY.size + i * nid_size
                if nid_type == 0:
                    nids.append(nidstr(struct.unpack_from("<Q", data, at)[0]))
                else:
                    nids.append(nid16str(data, at))
            kind = {
                LDD_F_SV_TYPE_MDT: "MDT",
                LDD_F_SV_TYPE_OST: "OST",
                LDD_F_SV_TYPE_MGS: "MGS",
            }.get(ttype, f"type{ttype}")
            targets.append(
                {
                    "name": f"{fsname}-{kind}{index:04x}",
                    "kind": kind,
                    "index": index,
                    "instance": instance,
                    "version": version,
                    "nids": nids,
                }
            )
            off += length
    return targets


def cfg_string(buf):
    """A NUL-terminated wire string as text."""
    return buf.split(b"\0", 1)[0].decode(errors="replace")


def logidstr(logid):
    """A logid in the [seq:oid:ogen] form --config-log accepts."""
    oid, seq = struct.unpack("<QQ", logid["lgd_oi"])
    return f"[{seq:#x}:{oid:#x}:{logid['lgd_ogen']:#x}]"


def cfg_bufs(cfg, data):
    """The buffers of a lustre_cfg record, 8-aligned after the lengths."""
    n = cfg["lcfg_bufcount"]
    if LUSTRE_CFG.size + 4 * n > len(data):
        raise ProtocolError(f"lustre_cfg record too short for {n} buffers")
    buflens = struct.unpack_from(f"<{n}I", data, LUSTRE_CFG.size)
    off = round8(LUSTRE_CFG.size + 4 * n)
    bufs = []
    for blen in buflens:
        bufs.append(data[off : off + blen])
        off += round8(blen)
    return bufs


def flag_names(value, table):
    """A flag word as hex plus the names from a (bit, name) table."""
    names = [name for bit, name in table if value & bit]
    return f"{value:#x} ({('|'.join(names) if names else 'none')})"


def format_nodemap_entry(key, rec):
    """One nodemap index entry, decoded by key type and subtype."""
    nm_id, sub = key
    ktype, nm_id = nm_id >> NM_TYPE_SHIFT, nm_id & NM_TYPE_MASK
    kind = NODEMAP_IDX_NAMES.get(ktype, f"type{ktype}")
    head = f"nodemap {nm_id}"
    if kind == "global":
        return f"global: active={rec[0]}"
    if kind == "cluster":
        if sub == NODEMAP_CLUSTER_REC:
            name, fl, fl2, _, squash_projid, squash_uid, squash_gid = (
                NODEMAP_CLUSTER.unpack(rec)
            )
            return (
                f"{head} {cfg_string(name)}: "
                f"flags {flag_names(fl, NM_FLAG_NAMES)}, "
                f"flags2 {flag_names(fl2, NM_FLAG2_NAMES)}, "
                f"squash uid {squash_uid} gid {squash_gid} projid {squash_projid}"
            )
        if sub == NODEMAP_CLUSTER_ROLES:
            roles, privs, raise_, _ = NODEMAP_ROLES.unpack(rec)
            return (
                f"{head} roles: rbac {roles:#x}, child_raise_privs {privs:#x}, "
                f"rbac_raise {raise_:#x}"
            )
        if sub == NODEMAP_CLUSTER_OFFSET:
            o = NODEMAP_OFFSET.unpack(rec)
            return (
                f"{head} offset: uid {o[0]}+{o[1]}, gid {o[2]}+{o[3]}, "
                f"projid {o[4]}+{o[5]}"
            )
        if sub == NODEMAP_CLUSTER_CAPS:
            caps, ctype = NODEMAP_CAPS.unpack_from(rec)
            return f"{head} user caps: {caps:#x} type {ctype}"
        if sub == NODEMAP_CLUSTER_VERSION:
            policy, glob = NODEMAP_VERSION.unpack(rec)
            policy = NODEMAP_VERSION_POLICY_NAMES.get(policy, policy)
            return f"{head} version_policy: {policy}:{cfg_string(glob)}"
        if sub >= NODEMAP_FILESET:
            fs_id, frag = divmod(sub - NODEMAP_FILESET, NODEMAP_FILESET_SUBID_RANGE)
            if frag == 0:
                return f"{head} fileset {fs_id}: flags {rec[0]:#x}"
            path, frag_id = NODEMAP_FILESET_FRAG.unpack(rec)
            return f"{head} fileset {fs_id} fragment {frag_id}: {cfg_string(path)}"
        return f"{head} cluster subid {sub}: {rec.hex()}"
    if kind in ("range", "nidmask"):
        rtype, rid = sub >> NM_TYPE_SHIFT, sub & NM_TYPE_MASK
        ban = " (ban)" if rtype & 1 else ""
        if kind == "nidmask":
            return f"{head} nidmask {rid}{ban}: {nid16str(rec)}/{rec[31]}"
        start, end = NODEMAP_RANGE.unpack_from(rec)
        return f"{head} range {rid}{ban}: {nidstr(start)} - {nidstr(end)}"
    if kind in ("uidmap", "gidmap", "projidmap"):
        fs_id = struct.unpack_from("<I", rec)[0]
        return f"{head} {kind}: client {sub} -> fs {fs_id}"
    if kind == "empty":
        return "empty record"
    return f"{kind} {head} sub {sub}: {rec.hex()}"


def fidstr(fid):
    """DFID form of a FID: 16 wire bytes or a (seq, oid, ver) tuple."""
    seq, oid, ver = LU_FID.unpack(fid) if isinstance(fid, bytes) else fid
    return f"[{seq:#x}:{oid:#x}:{ver:#x}]"


def type_char(mode):
    """The 'ls -l' type character of a mode; '?' when unknown."""
    if mode is None:
        return "?"
    return {
        stat.S_IFDIR: "d",
        stat.S_IFREG: "-",
        stat.S_IFLNK: "l",
        stat.S_IFCHR: "c",
        stat.S_IFBLK: "b",
        stat.S_IFIFO: "p",
        stat.S_IFSOCK: "s",
    }.get(stat.S_IFMT(mode), "?")


def format_attrs(body):
    """An 'ls -l' style line from an mdt_body."""
    mode = body["mb_mode"]
    mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(body["mb_mtime"]))
    return (
        f"{type_char(mode)}{stat.filemode(mode)[1:]} {body['mb_nlink']:3d} "
        f"{body['mb_uid']:5d} {body['mb_gid']:5d} {body['mb_size']:10d} "
        f"{mtime} {fidstr(body['mb_fid1'])}"
    )


def ostidstr(oi):
    """DOSTID: an ost_id is a lu_fid unless its second word says pre-FID."""
    oid, seq = struct.unpack("<QQ", oi)
    if seq in (FID_SEQ_OST_MDT0, FID_SEQ_LOV_DEFAULT):
        return f"{seq:#x}:{oid}"
    return fidstr(oi)


def parse_lov_mds_md(ea, start=0, end=LUSTRE_EOF, mirror=0, flags=0):
    """One lov_mds_md_v1/v3 blob into a Component."""
    if len(ea) < LOV_MDS_MD.size:
        raise ProtocolError(f"layout too short ({len(ea)} bytes)")
    magic, pattern, _oi, ssize, scount, _gen = LOV_MDS_MD.unpack_from(ea)
    if magic == LOV_MAGIC_V1:
        off = LOV_MDS_MD.size
    elif magic == LOV_MAGIC_V3:
        off = LOV_MDS_MD.size + 16  # lmm_pool_name
    else:
        raise ValueError(f"unsupported LOV magic {magic:#x}")
    if pattern & LOV_PATTERN_F_RELEASED:
        raise ValueError("file is HSM released, data is not on the OSTs")
    if pattern & LOV_PATTERN_MDT:
        raise ValueError("Data-on-MDT component, not supported")
    if ssize == 0:
        raise ProtocolError("layout with a stripe size of 0")
    # Short of objects, the stripes left out would read back as zeros.
    # The MDT cuts the EA to mb_eadatasize, so a very wide file ends here.
    if off + scount * LOV_OST_DATA.size > len(ea):
        raise ProtocolError(
            f"layout truncated: {scount} stripes do not fit in {len(ea)} bytes "
            f"(LOV_EA_BUFSIZE is {LOV_EA_BUFSIZE})"
        )
    objects = []
    for _ in range(scount):
        ooi, _ogen, oidx = LOV_OST_DATA.unpack_from(ea, off)
        objects.append((ooi, oidx))
        off += LOV_OST_DATA.size
    return Component(start, end, ssize, scount, pattern, objects, mirror, flags)


def parse_layout(ea):
    """LOV EA into the list of components to read, in file order.

    Plain v1/v3 layouts are one component covering the whole file. For a
    composite (PFL/FLR) layout only instantiated components are read,
    and for mirrored files only the first mirror with no stale component.
    """
    if len(ea) < 4:
        raise ProtocolError(f"layout too short ({len(ea)} bytes)")
    magic = struct.unpack_from("<I", ea)[0]
    if magic != LOV_MAGIC_COMP_V1:
        return [parse_lov_mds_md(ea)]
    if len(ea) < LOV_COMP_MD.size:
        raise ProtocolError(f"composite layout too short ({len(ea)} bytes)")
    _magic, _size, _gen, _flags, nentries, _nmirrors = LOV_COMP_MD.unpack_from(ea)
    if LOV_COMP_MD.size + nentries * LOV_COMP_ENTRY_SIZE > len(ea):
        raise ProtocolError(f"composite layout truncated: {nentries} entries")
    comps = []
    for n in range(nentries):
        eid, eflags, start, end, off, esize = LOV_COMP_ENTRY.unpack_from(
            ea, LOV_COMP_MD.size + n * LOV_COMP_ENTRY_SIZE
        )
        if not eflags & LCME_FL_INIT:
            continue
        comps.append(
            parse_lov_mds_md(ea[off : off + esize], start, end, eid >> 16, eflags)
        )
    mirrors = sorted({c.mirror for c in comps})
    for m in mirrors:
        mine = [c for c in comps if c.mirror == m]
        if not any(c.flags & LCME_FL_STALE for c in mine):
            return sorted(mine, key=lambda c: c.start)
    raise ValueError("no mirror without stale components")


def lmv_hash_fnv1a(count, name):
    """lmv_hash_fnv1a(): FNV-1a over the name, modulo the stripe count."""
    h = 0xCBF29CE484222325
    for c in name:
        h = ((h ^ c) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h % count


def crush_hash(a, b):
    """crush_hash(): Robert Jenkins' 32-bit mix of two values."""
    m = 0xFFFFFFFF

    def mix(a, b, c):
        a = ((a - b - c) & m) ^ (c >> 13)
        b = ((b - c - a) & m) ^ ((a << 8) & m)
        c = ((c - a - b) & m) ^ (b >> 13)
        a = ((a - b - c) & m) ^ (c >> 12)
        b = ((b - c - a) & m) ^ ((a << 16) & m)
        c = ((c - a - b) & m) ^ (b >> 5)
        a = ((a - b - c) & m) ^ (c >> 3)
        b = ((b - c - a) & m) ^ ((a << 10) & m)
        c = ((c - a - b) & m) ^ (b >> 15)
        return a, b, c

    h = 1315423911 ^ a ^ b
    x, y = 231232, 1232
    a, b, h = mix(a, b, h)
    x, a, h = mix(x, a, h)
    b, y, h = mix(b, y, h)
    return h


def lmv_hash_crush(count, name):
    """lmv_hash_crush(): the stripe with the highest straw for the name.

    Without its rules for temporary and backup file names, which hash
    part of the name only; LmvLayout.parents_of() finds those anyway.
    """
    pg_id = lmv_hash_fnv1a(LMV_CRUSH_PG_COUNT, name)
    return max(range(count), key=lambda i: (crush_hash(pg_id, i), -i))


@dataclasses.dataclass
class LmvLayout:
    """A striped directory: struct lmv_mds_md_v1 of the master object."""

    hash_type: int
    fids: list  # of the stripes, in stripe order; 16 wire bytes each

    def stripe_of(self, name):
        """lmv_name_to_stripe_index() for a directory at rest, or None."""
        count = len(self.fids)
        name = name.encode()
        hash_type = self.hash_type & LMV_HASH_TYPE_MASK
        if count == 1:
            return 0
        if hash_type == LMV_HASH_TYPE_ALL_CHARS:
            return sum(name) % count
        if hash_type == LMV_HASH_TYPE_FNV_1A_64:
            return lmv_hash_fnv1a(count, name)
        if hash_type in (LMV_HASH_TYPE_CRUSH, LMV_HASH_TYPE_CRUSH2):
            return lmv_hash_crush(count, name)
        return None

    def parents_of(self, name):
        """The stripes to look a name up in, the likeliest first.

        That is where it hashes to, then the rest, which is where
        migration, restriping, an unknown hash type or the CRUSH rules
        for temporary names may have it instead.
        """
        idx = self.stripe_of(name)
        if idx is None:
            return list(self.fids)
        return [self.fids[idx]] + self.fids[:idx] + self.fids[idx + 1 :]


def parse_lmv(ea):
    """The LmvLayout of an LMV EA, or None if it is not a master's."""
    if len(ea) < LMV_MDS_MD_V1.size:
        return None
    magic, count, _, hash_type = LMV_MDS_MD_V1.unpack_from(ea)[:4]
    if magic != LMV_MAGIC_V1 or count == 0:
        return None
    fids = [
        bytes(ea[off : off + LU_FID.size])
        for off in range(LMV_MDS_MD_V1.size, len(ea) - LU_FID.size + 1, LU_FID.size)
    ][:count]
    if len(fids) != count:
        raise ProtocolError(f"LMV EA: {count} stripes, {len(fids)} FIDs")
    return LmvLayout(hash_type, fids)


def stripe_ost(osts, idx):
    """The connected Target for an OST index, or a ValueError."""
    ost = osts.get(idx)
    if ost is None or not ost.connected:
        raise ValueError(
            f"OST{idx:04x} is not in the MGS config, inactive, or did not connect"
        )
    return ost


def stripe_extent(comp, i, lo, hi):
    """Object byte range (obj_lo, obj_hi) of stripe i for file range [lo, hi).

    Stripe units go round-robin over the component's objects, so the
    units of one object inside a file range are one contiguous run of
    that object. Units are numbered from the start of the file in every
    component (lov_stripe_offset()), so a component starting at 1M with
    two 128K stripes keeps its first byte at offset 512K of object 0.
    [lo, hi) must lie inside the component's extent.
    """
    ss, sc = comp.stripe_size, comp.stripe_count
    r_lo, r_hi = lo, hi
    u_lo, u_hi = r_lo // ss, (r_hi - 1) // ss  # first and last unit
    first = u_lo + (i - u_lo) % sc  # first unit of stripe i >= u_lo
    last = u_hi - (u_hi - i) % sc  # last unit of stripe i <= u_hi
    if first > last:
        return 0, 0
    obj_lo = (first // sc) * ss + (r_lo % ss if first == u_lo else 0)
    obj_hi = (last // sc) * ss + ((r_hi - 1) % ss + 1 if last == u_hi else ss)
    return obj_lo, obj_hi


def scatter_stripe(out, offset, comp, i, obj_off, data):
    """Copy stripe i object data at obj_off into out, a file buffer at offset."""
    ss, sc = comp.stripe_size, comp.stripe_count
    pos = 0
    while pos < len(data):
        unit, in_unit = divmod(obj_off + pos, ss)
        n = min(ss - in_unit, len(data) - pos)
        foff = (unit * sc + i) * ss + in_unit
        lo = max(foff, offset, comp.start)
        hi = min(foff + n, offset + len(out), comp.end)
        if lo < hi:
            out[lo - offset : hi - offset] = data[pos + lo - foff : pos + hi - foff]
        pos += n


def describe_component(c):
    """One layout component as text: extent, striping and OSTs."""
    end = "EOF" if c.end >= (1 << 63) else c.end
    osts = ",".join(f"{idx:04x}" for _, idx in c.objects)
    return (
        f"[{c.start}, {end}) {c.stripe_count} stripes x {c.stripe_size} on OST {osts}"
    )


def parse_dirpages(data):
    """Decode lu_dirpage pages from an MDS_READPAGE bulk.

    Returns (entries, at_end, next_hash) where entries are
    (name, fid, type_mode_or_None) and next_hash is where to continue.
    """
    entries = []
    next_hash = MDS_DIR_END_OFF
    for off in range(0, len(data) - LU_DIRPAGE.size + 1, LU_PAGE_SIZE):
        _hash_start, hash_end, flags, _ = LU_DIRPAGE.unpack_from(data, off)
        pos = off + LU_DIRPAGE.size
        page_end = min(off + LU_PAGE_SIZE, len(data))
        while not (flags & LDF_EMPTY) and pos + LU_DIRENT.size <= page_end:
            seq, oid, ver, _ehash, reclen, namelen, attrs = LU_DIRENT.unpack_from(
                data, pos
            )
            # mdd_dir_page_build() gives the last entry of a page
            # lde_reclen == 0; it is still a real entry
            if namelen == 0 or pos + LU_DIRENT.size + namelen > page_end:
                break
            name = data[pos + LU_DIRENT.size : pos + LU_DIRENT.size + namelen].decode(
                errors="replace"
            )
            ltype = None
            if attrs & LUDA_TYPE:
                # lu_dirent_type_get(): luda_type sits at lde_name plus the
                # name length rounded up to 2 bytes (no NUL in between)
                toff = pos + LU_DIRENT.size + ((namelen + 1) & ~1)
                if toff + 2 <= page_end:
                    ltype = struct.unpack_from("<H", data, toff)[0]
            entries.append((name, (seq, oid, ver), ltype))
            if reclen == 0:
                break
            pos += reclen
        next_hash = hash_end
        if hash_end == MDS_DIR_END_OFF:
            return entries, True, next_hash
    return entries, False, next_hash


def format_cfg_record(index, rec_type, data):
    """One config record in the style of 'lctl llog_print' YAML output."""
    fields = [f"index: {index}"]
    if (rec_type & LLOG_OP_MASK) != LLOG_OP_MAGIC:
        fields += [f"type: {rec_type:#x}", f"len: {len(data)}"]
    elif rec_type != OBD_CFG_REC:
        fields += [f"event: llog_op_{rec_type & 0xFFFFF:#x}", f"len: {len(data)}"]
    else:
        fields += cfg_record_fields(data)
    return "- { " + ", ".join(fields) + " }"


def cfg_record_fields(data):
    """The 'key: value' fields of one lustre_cfg record, after its index."""
    cfg = LUSTRE_CFG.unpack(data)
    bufs = cfg_bufs(cfg, data)
    cmd = cfg["lcfg_command"]
    name, labels = LCFG_NAMES.get(cmd, (f"cmd_{cmd:#x}", ()))
    fields = [f"event: {name}"]
    if cfg["lcfg_version"] != LUSTRE_CFG_VERSION:
        fields.append(f"version: {cfg['lcfg_version']:#x}")
    if cfg["lcfg_flags"]:
        fields.append(f"flags: {cfg['lcfg_flags']:#08x}")
    if cfg["lcfg_num"]:
        fields.append(f"num: {cfg['lcfg_num']:#08x}")
    if cfg["lcfg_nid"]:
        fields.append(f"nid: {nidstr(cfg['lcfg_nid'])}({cfg['lcfg_nid']:#x})")
    if bufs and bufs[0]:
        fields.append(f"device: {cfg_string(bufs[0])}")
    if cmd == LCFG_MARKER and len(bufs) > 1 and len(bufs[1]) >= CFG_MARKER.size:
        m = CFG_MARKER.unpack(bufs[1])
        flags = [nm for bit, nm in CM_FLAG_NAMES if m["cm_flags"] & bit]
        fields += [
            f"flags: {m['cm_flags']:#04x} ({'|'.join(flags) or 'none'})",
            f"step: {m['cm_step']}",
            f"version: {ocd_version_str(m['cm_vers'])}",
            f"target: {cfg_string(m['cm_tgtname'])}",
            f"comment: {cfg_string(m['cm_comment'])}",
            f"createtime: {m['cm_createtime']}",
            f"canceltime: {m['cm_canceltime']}",
        ]
    elif cmd == LCFG_SET_PARAM and len(bufs) > 1 and "=" in cfg_string(bufs[1]):
        param, value = cfg_string(bufs[1]).split("=", 1)
        fields += [f"parameter: {param}", f"value: {value}"]
    else:
        for i, buf in enumerate(bufs[1:]):
            if buf:
                label = labels[i] if i < len(labels) else str(i + 1)
                fields.append(f"{label}: {cfg_string(buf)}")
    return fields


def human(n):
    """A byte count in binary units."""
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.1f} {units[i]}" if i else f"{n} B"


# ---------------------------------------------------------------------------
# Targets: what a real mount would connect to, in the order it does it
# ---------------------------------------------------------------------------
MGS_FLAGS = OBD_CONNECT_VERSION | OBD_CONNECT_AT | OBD_CONNECT_FULL20
MDT_FLAGS = (
    OBD_CONNECT_VERSION
    | OBD_CONNECT_IBITS
    | OBD_CONNECT_ATTRFID
    | OBD_CONNECT_AT
    | OBD_CONNECT_FID
    | OBD_CONNECT_FULL20
    | OBD_CONNECT_64BITHASH
    | OBD_CONNECT_LVB_TYPE
    | OBD_CONNECT_DIR_STRIPE
)
OST_FLAGS = (
    OBD_CONNECT_VERSION
    | OBD_CONNECT_INDEX
    | OBD_CONNECT_AT
    | OBD_CONNECT_FID
    | OBD_CONNECT_FULL20
    | OBD_CONNECT_64BITHASH
)


# ---------------------------------------------------------------------------
# FUSE: a read-only view served single-threaded through fusepy
# ---------------------------------------------------------------------------
def make_fuse_ops(fusepy, client, mdts, osts):
    """Build the fusepy Operations object; its class subclasses the module's."""

    class LustreFuse(fusepy.Operations):
        """Path-based read-only operations over the userspace client.

        FUSE runs us with nothreads, so requests are serialized. The one
        extra thread is a keepalive pinger: servers evict an export that
        has not sent anything for 6 ping intervals (150s by default),
        so it sends OBD_PING to every target every 30s under the mutex
        that also guards the LNet socket.

        fusepy dispatches FUSE operations by attribute name, so nothing
        here may be named like an operation ('lock', 'flock', ...).
        """

        # fusepy fixes the signatures of the operations
        # pylint: disable=unused-argument

        CACHE_SECS = 1.0
        CACHE_MAX = 4096  # entries per cache before the expired ones go

        def __init__(self):
            self._mutex = threading.Lock()
            self.osts = osts  # an OstPool
            self.root = client.get_root(mdts)
            self.fids = {}  # path -> (time, fid); expires, so renames show
            self.cache = {}  # path -> (time, FuseNode)
            self.stop = threading.Event()
            self.pinger = threading.Thread(target=self._pinger, daemon=True)
            self.pinger.start()

        def _pinger(self):
            """Keepalive thread: OBD_PING every connected target every 30s."""
            while not self.stop.wait(PING_INTERVAL):
                with self._mutex:
                    for t in mdts.connected() + self.osts.connected():
                        try:
                            client.rpc(t, LUSTRE_OBD_VERSION, OBD_PING)
                        # keep the mount alive, whatever went wrong
                        # pylint: disable-next=broad-exception-caught
                        except Exception as e:
                            log.warning("ping %s: %s", t.name, e)

        # -- helpers --------------------------------------------------------
        def _rpc_errno(self, e):
            """The errno to hand to FUSE for a failed request."""
            if isinstance(e, RpcError):
                return -e.status if e.status < 0 else errno.EIO
            return errno.EIO

        def _fid(self, path):
            """FID of a path, looked up one component at a time and cached."""
            path = "/" + path.strip("/")
            if path == "/":
                return self.root
            now = time.monotonic()
            hit = self.fids.get(path)
            if hit and now - hit[0] < self.CACHE_SECS:
                return hit[1]
            parent, name = path.rsplit("/", 1)
            pfid = self._fid(parent or "/")
            try:
                body = client.lookup(mdts, pfid, name)
            except RpcError as e:
                self.fids.pop(path, None)
                raise fusepy.FuseOSError(self._rpc_errno(e)) from None
            self._remember(self.fids, path, now, body["mb_fid1"])
            return body["mb_fid1"]

        def _remember(self, cache, path, now, value):
            """Add to a cache, dropping what has expired once it is large."""
            if len(cache) >= self.CACHE_MAX:
                for old in [
                    p for p, hit in cache.items() if now - hit[0] >= self.CACHE_SECS
                ]:
                    del cache[old]
                if len(cache) >= self.CACHE_MAX:
                    cache.clear()
            cache[path] = (now, value)

        def _node(self, path):
            """Attributes, plus layout and object sizes for regular files."""
            path = "/" + path.strip("/")
            now = time.monotonic()
            hit = self.cache.get(path)
            if hit and now - hit[0] < self.CACHE_SECS:
                return hit[1]
            fid = self._fid(path)
            try:
                attrs, ea = client.getattr_fid(mdts, fid, layout=True)
            except RpcError as e:
                self.fids.pop(path, None)
                raise fusepy.FuseOSError(self._rpc_errno(e)) from None
            node = FuseNode(fid, attrs, attrs["mb_size"])
            if stat.S_ISREG(attrs["mb_mode"]) and ea:
                try:
                    node.comps = parse_layout(ea)
                    node.size = client.file_size(self.osts, node.comps, node.sizes)
                except (RpcError, ValueError) as e:
                    log.warning("%s: %s", path, e)
                    node.comps = None
            self._remember(self.cache, path, now, node)
            return node

        # -- operations -----------------------------------------------------
        def getattr(self, path, fh=None):
            """stat(2) from the cached node."""
            with self._mutex:
                node = self._node(path)
            a = node.attrs
            return {
                "st_mode": a["mb_mode"],
                "st_nlink": a["mb_nlink"],
                "st_uid": a["mb_uid"],
                "st_gid": a["mb_gid"],
                "st_size": node.size,
                "st_blksize": LU_PAGE_SIZE,
                "st_blocks": (node.size + 511) // 512,
                "st_atime": a["mb_atime"],
                "st_mtime": a["mb_mtime"],
                "st_ctime": a["mb_ctime"],
                "st_rdev": a["mb_rdev"],
            }

        def readdir(self, path, fh):
            """All names in a directory; '.' and '..' come from the MDT."""
            with self._mutex:
                fid = self._fid(path)
                try:
                    entries = client.readdir(mdts, fid)
                except RpcError as e:
                    raise fusepy.FuseOSError(self._rpc_errno(e)) from None
            names = [name for name, _, _ in entries]
            return names or [".", ".."]

        def readlink(self, path):
            """A symlink's target."""
            with self._mutex:
                fid = self._fid(path)
                try:
                    _, target = client.getattr_fid(mdts, fid, linkname=True)
                except RpcError as e:
                    raise fusepy.FuseOSError(self._rpc_errno(e)) from None
            return cfg_string(target)

        def open(self, path, flags):
            """Refuse writes and anything that cannot be read through a layout."""
            if flags & (os.O_WRONLY | os.O_RDWR):
                raise fusepy.FuseOSError(errno.EROFS)
            with self._mutex:
                node = self._node(path)
            if not stat.S_ISREG(node.attrs["mb_mode"]):
                raise fusepy.FuseOSError(errno.EISDIR)
            if node.comps is None and node.size:
                raise fusepy.FuseOSError(errno.EOPNOTSUPP)
            return 0

        def read(self, path, size, offset, fh):
            """Read through the layout; zeros past the stripe objects' ends."""
            with self._mutex:
                node = self._node(path)
                if offset >= node.size or node.comps is None:
                    return b""
                size = min(size, node.size - offset)
                try:
                    return client.read_range(
                        self.osts, node.comps, node.sizes, offset, size
                    )
                except (RpcError, ValueError) as e:
                    log.error("read %s: %s", path, e)
                    raise fusepy.FuseOSError(self._rpc_errno(e)) from None

        def lock(self, path, fh, cmd, lock):
            """Pretend POSIX locks succeed: nothing can change the data here."""
            return 0

        def statfs(self, path):
            """statfs(2): blocks summed over the OSTs, inodes over the MDTs."""
            with self._mutex:
                try:
                    mds = [client.statfs(t) for t in mdts.all()]
                    ost = [client.statfs(t) for t in self.osts.all()]
                except RpcError as e:
                    raise fusepy.FuseOSError(self._rpc_errno(e)) from None
            bsize = LU_PAGE_SIZE
            blocks = sum(st["os_blocks"] * st["os_bsize"] for st in ost) // bsize
            bfree = sum(st["os_bfree"] * st["os_bsize"] for st in ost) // bsize
            bavail = sum(st["os_bavail"] * st["os_bsize"] for st in ost) // bsize
            return {
                "f_bsize": bsize,
                "f_frsize": bsize,
                "f_blocks": blocks,
                "f_bfree": bfree,
                "f_bavail": bavail,
                "f_files": sum(st["os_files"] for st in mds),
                "f_ffree": sum(st["os_ffree"] for st in mds),
                "f_namemax": mds[0]["os_namelen"],
            }

        def destroy(self, path):
            """Unmount: stop the pinger."""
            self.stop.set()

    return LustreFuse()


class TargetPool:
    """Targets of one kind by index, connected on first use."""

    def __init__(self, client, targets, active):
        self.client = client
        self.targets = targets  # index -> Target
        self.active = active  # indices not marked inactive

    def get(self, idx):
        """The connected Target of an index, or None if it is unusable."""
        t = self.targets.get(idx)
        if t is None:
            return None
        if not t.connected:
            if idx not in self.active:
                log.warning("%s: marked inactive in the config", t.name)
                return None
            try:
                self.client.connect(t)
            except RpcError as e:
                log.error("%s: %s", t.name, e)
                return None
        return t

    def connected(self):
        """The targets connected so far."""
        return [t for t in self.targets.values() if t.connected]

    def all(self):
        """Every active target, connecting the ones not yet connected."""
        found = []
        for idx in sorted(self.targets):
            t = self.get(idx)
            if t is not None:
                found.append(t)
        return found


class OstPool(TargetPool):
    """The filesystem's OSTs.

    read_range(), file_size() and copy_file() ask for an OST by index
    whenever a stripe lives there; the first such request connects it.
    """


class MdtPool(TargetPool):
    """The filesystem's MDTs: what LMV is to a kernel client.

    MDT0000 has the root and is connected from the start. The others are
    connected when a FID turns out to live there, which the FLD says.
    """

    STRIPES_SECS = 1.0  # how long a directory's LMV EA is trusted
    STRIPES_MAX = 4096  # cached directories before the cache is dropped

    def __init__(self, client, targets, active):
        super().__init__(client, targets, active)
        self.seqs = {}  # sequence -> MDT index; never changes
        self.stripes = {}  # directory fid -> (time, LmvLayout or None)

    @property
    def root(self):
        """MDT0000."""
        return self.targets[0]

    @property
    def name(self):
        """For messages about the namespace as a whole."""
        return self.root.name

    def for_fid(self, fid):
        """The connected Target of the MDT a FID lives on."""
        seq = LU_FID.unpack(fid)[0] if isinstance(fid, bytes) else fid[0]
        if seq < FID_SEQ_NORMAL or len(self.targets) == 1:
            idx = 0
        else:
            idx = self.seqs.get(seq)
            if idx is None:
                idx = self.seqs[seq] = self.client.fld_lookup(self.root, seq)
        t = self.get(idx)
        if t is None:
            raise OSError(
                errno.ENXIO, f"MDT{idx:04x} of {fidstr(fid)} is not available"
            )
        return t

    def cached_stripes(self, fid):
        """A fresh dir_stripes() result, or False for none."""
        hit = self.stripes.get(bytes(fid))
        if hit and time.monotonic() - hit[0] < self.STRIPES_SECS:
            return hit[1]
        return False

    def remember_stripes(self, fid, lmv):
        """Cache a dir_stripes() result."""
        if len(self.stripes) >= self.STRIPES_MAX:
            self.stripes.clear()
        self.stripes[bytes(fid)] = (time.monotonic(), lmv)


def mgs_target():
    """The Target describing the MGS."""
    return Target(
        name="MGS",
        portal=MGS_REQUEST_PORTAL,
        connect_opc=MGS_CONNECT,
        disconnect_opc=MGS_DISCONNECT,
        ocd_flags=MGS_FLAGS,
    )


def make_target(fsname, kind, index):
    """The Target of one of the filesystem's MDTs or OSTs."""
    if kind == "MDT":
        return Target(
            name=f"{fsname}-MDT{index:04x}",
            portal=MDS_REQUEST_PORTAL,
            connect_opc=MDS_CONNECT,
            disconnect_opc=MDS_DISCONNECT,
            ocd_flags=MDT_FLAGS,
            statfs_opc=MDS_STATFS,
            statfs_version=LUSTRE_MDS_VERSION,
            ocd_extra={"ocd_ibits_known": MDS_INODELOCK_FULL},
        )
    return Target(
        name=f"{fsname}-OST{index:04x}",
        portal=OST_REQUEST_PORTAL,
        connect_opc=OST_CONNECT,
        disconnect_opc=OST_DISCONNECT,
        ocd_flags=OST_FLAGS,
        statfs_opc=OST_STATFS,
        statfs_version=LUSTRE_OST_VERSION,
        ocd_extra={"ocd_index": index},
    )


def targets_from_config_log(records):
    """MDTs and OSTs named by a FSNAME-client config log.

    Follows the records the way class_config_llog_handler() does: an
    add_mdc or add_osc inside a marker flagged CM_SKIP is ignored,
    add_osc_inactive is remembered but flagged inactive, and del_osc or
    del_mdc removes the target. Returns {(kind, index): active}.
    """
    found = {}
    skipping = False
    for _, rec_type, data in records:
        if rec_type != OBD_CFG_REC:
            continue
        cfg = LUSTRE_CFG.unpack(data)
        bufs = cfg_bufs(cfg, data)
        cmd = cfg["lcfg_command"]
        if cmd == LCFG_MARKER and len(bufs) > 1 and len(bufs[1]) >= CFG_MARKER.size:
            m = CFG_MARKER.unpack(bufs[1])
            if m["cm_flags"] & CM_START:
                skipping = bool(m["cm_flags"] & CM_SKIP)
            elif m["cm_flags"] & CM_END:
                skipping = False
            continue
        if skipping or len(bufs) < 3:
            continue
        kind = {
            LCFG_ADD_MDC: "MDT",
            LCFG_DEL_MDC: "MDT",
            LCFG_LOV_ADD_OBD: "OST",
            LCFG_LOV_ADD_INA: "OST",
            LCFG_LOV_DEL_OBD: "OST",
        }.get(cmd)
        if kind is None:
            continue
        try:
            idx = int(cfg_string(bufs[2]))
        except ValueError:
            m = re.search(r"-(MDT|OST)([0-9a-f]{4})", cfg_string(bufs[1]), re.I)
            if not m:
                continue
            idx = int(m.group(2), 16)
        if cmd in (LCFG_DEL_MDC, LCFG_LOV_DEL_OBD):
            found.pop((kind, idx), None)
        else:
            found[(kind, idx)] = cmd != LCFG_LOV_ADD_INA
    return found


def discover_targets(client, mgs, fsname):
    """The filesystem's MDTs and OSTs, from the MGS.

    The FSNAME-client config log is what a real mount follows, so it is
    the primary source; the imperative recovery NID table (registered
    targets) is the fallback when that log cannot be read.
    Returns a sorted list of (kind, index, active).
    """
    logname = f"{fsname}-client"
    try:
        found = targets_from_config_log(client.read_config_log(mgs, logname))
        source = f"config log {logname}"
    except (RpcError, ValueError) as e:
        log.warning(
            "%s: cannot read %s (%s), trying the NID table", mgs.name, logname, e
        )
        found = {}
        source = None
    if not found:
        version, regs = client.read_nidtbl(mgs, fsname)
        found = {
            (t["kind"], t["index"]): True for t in regs if t["kind"] in ("MDT", "OST")
        }
        source = f"NID table version {version}"
    targets = sorted((kind, idx, active) for (kind, idx), active in found.items())
    log.info(
        "%s: %s lists %d MDTs and %d OSTs%s",
        fsname,
        source,
        sum(1 for k, _, a in targets if k == "MDT"),
        sum(1 for k, _, a in targets if k == "OST"),
        (
            ""
            if all(a for k, _, a in targets)
            else f" ({sum(1 for k, _, a in targets if not a)} inactive)"
        ),
    )
    return targets


def connect_target(client, t):
    """Connect one target and take its first RPC; False if that failed."""
    try:
        client.connect(t)
        if t.statfs_opc is None:
            client.ping(t)
        else:
            client.statfs(t)
    except RpcError as e:
        if (
            t.connect_opc in (MDS_CONNECT, OST_CONNECT)
            and e.status == -errno.EACCES
            and not t.connected
        ):
            log.error(
                "%s: connect refused with EACCES: the server's nodemap "
                "version_policy does not accept a client claiming "
                "Lustre %s (see 'lctl get_param nodemap.*.version_"
                "policy' on the server)",
                t.name,
                ocd_version_str(client.version),
            )
        else:
            log.error("%s: %s", t.name, e)
        return False
    return True


def discover_fsname(client, mgs):
    """The name of the one filesystem on the MGS, or None (and say why)."""
    log.info(
        "%s: no --fsname, looking for one in logids [%#x:1..%d:0x0]",
        mgs.name,
        FID_SEQ_LLOG,
        FSNAME_PROBE_LOGS,
    )
    fsnames = client.find_fsnames(mgs, FSNAME_PROBE_LOGS)
    if len(fsnames) == 1:
        log.info("%s: serves filesystem %s", mgs.name, fsnames[0])
        return fsnames[0]
    if fsnames:
        log.error(
            "%s: serves several filesystems (%s), choose one with --fsname",
            mgs.name,
            ", ".join(fsnames),
        )
    else:
        log.error("%s: no client config log found, give --fsname", mgs.name)
    return None


def run_mgs_commands(client, mgs, args):
    """The options served by the MGS alone; returns the exit status."""
    rc = 0
    if args.list_logs:
        try:
            client.list_logs(mgs, args.fsname)
        except (RpcError, ProtocolError) as e:
            log.error("%s: cannot list logs: %s", mgs.name, e)
            rc = 1
        sys.stdout.flush()
    if args.nodemap:
        try:
            entries = client.read_nodemap(mgs)
        except (RpcError, ValueError) as e:
            log.error("%s: cannot read nodemap config: %s", mgs.name, e)
            rc = 1
        else:
            print(f"# nodemap config from MGS {args.server}: {len(entries)} records")
            for key, rec in entries:
                print(format_nodemap_entry(key, rec))
        sys.stdout.flush()
    if args.probe_logs is not None:
        try:
            client.probe_logs(mgs, args.probe_logs)
        except (RpcError, ProtocolError) as e:
            log.error("%s: probe failed: %s", mgs.name, e)
            rc = 1
        sys.stdout.flush()
    if args.config_log is not None:
        logname = args.config_log or f"{args.fsname}-client"
        m = re.match(r"^\[(0x[0-9a-f]+):(0x[0-9a-f]+):(0x[0-9a-f]+)\]$", logname, re.I)
        try:
            if m:
                seq, oid, ogen = (int(x, 16) for x in m.groups())
                logid = client.llog_open_by_id(mgs, seq, oid, ogen)
                recs = client.read_config_log(mgs, logid)
            else:
                recs = client.read_config_log(mgs, logname)
        except (RpcError, ProtocolError) as e:
            log.error("%s: cannot read config log %s: %s", mgs.name, logname, e)
            rc = 1
        else:
            log.info("%s: %d records", logname, len(recs))
            print(f"# config log {logname} from MGS {args.server}")
            for rec in recs:
                print(format_cfg_record(*rec))
        sys.stdout.flush()
    return rc


def connect_filesystem(client, mgs, fsname, connect_all):
    """The rest of the mount: what the MGS says the filesystem is.

    Every active target is connected when connect_all is set; otherwise
    only MDT0000 is, and the pools connect the other MDTs and the OSTs as
    FIDs and stripes need them.
    Returns (targets, MdtPool or None if MDT0000 is not connected,
    OstPool, all ok).
    """
    targets = []
    pools = {"MDT": ({}, set()), "OST": ({}, set())}
    ost_targets, ost_active = pools["OST"]
    try:
        discovered = discover_targets(client, mgs, fsname)
    except (RpcError, ValueError) as e:
        log.error("%s: cannot find the targets of %s: %s", mgs.name, fsname, e)
        return targets, None, OstPool(client, ost_targets, ost_active), False
    ok = True
    for kind, index, active in discovered:
        t = make_target(fsname, kind, index)
        targets.append(t)
        pools[kind][0][index] = t
        if active:
            pools[kind][1].add(index)
        if not active:
            log.info("%s: marked inactive in the config, not connecting", t.name)
        elif connect_all or (kind == "MDT" and index == 0):
            ok = connect_target(client, t) and ok
    mdt_targets, mdt_active = pools["MDT"]
    mdts = None
    if 0 in mdt_targets and mdt_targets[0].connected:
        mdts = MdtPool(client, mdt_targets, mdt_active)
    return targets, mdts, OstPool(client, ost_targets, ost_active), ok


def run_namespace_commands(client, mdts, osts, args, fusepy):
    """The options that go through the MDT; returns the exit status."""
    rc = 0
    if args.show_path is not None:
        try:
            client.show_path(mdts, args.show_path)
        except RpcError as e:
            log.error("%s: %s: %s", mdts.name, args.show_path, e)
            rc = 1
        except NotADirectoryError as e:
            log.error("%s: %s: not a directory", mdts.name, e)
            rc = 1
        sys.stdout.flush()
    if args.copy_file is not None:
        src, dest = args.copy_file
        try:
            client.copy_file(mdts, osts, src, dest)
        except IsADirectoryError:
            log.error("copy %s: is a directory", src)
            rc = 1
        except NotADirectoryError as e:
            log.error("copy %s: %s: not a directory", src, e)
            rc = 1
        except (RpcError, ValueError, OSError) as e:
            log.error("copy %s: %s", src, e)
            rc = 1
    if args.fuse is not None:
        log.info(
            "FUSE: mounting read-only at %s (unmount with fusermount3 -u %s)",
            args.fuse,
            args.fuse,
        )
        ops = make_fuse_ops(fusepy, client, mdts, osts)
        try:
            fusepy.FUSE(
                ops,
                args.fuse,
                foreground=True,
                nothreads=True,
                ro=True,
                allow_other=True,
                default_permissions=True,
                fsname=f"lustre-tcp:{args.fsname}",
                subtype="lustre_tcp",
            )
        finally:
            ops.stop.set()
        log.info("FUSE: unmounted")
    return rc


def load_fusepy():
    """The fusepy module under either of its import names, or None."""
    # an optional dependency, wanted by --fuse alone
    # pylint: disable=import-outside-toplevel
    try:
        import fusepy
    except ImportError:
        try:
            import fuse as fusepy
        except ImportError:
            return None
    return fusepy


def server_address(text):
    """argparse type of --server: an IPv4 address, resolving a host name."""
    try:
        return socket.gethostbyname(text)
    except OSError as e:
        raise argparse.ArgumentTypeError(f"{text!r}: {e}") from None


def lustre_version(text):
    """argparse type of --version: up to four dotted numbers below 256."""
    try:
        parts = tuple(int(x) for x in text.split("."))
    except ValueError:
        parts = ()
    if not 1 <= len(parts) <= 4 or not all(0 <= p <= 255 for p in parts):
        raise argparse.ArgumentTypeError(
            f"{text!r}: expected MAJOR[.MINOR[.PATCH[.FIX]]], each 0-255"
        )
    return parts + (0,) * (4 - len(parts))


def hold(client, targets, seconds):
    """Stay mounted: serve the socket, and ping so nothing evicts us."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        client.lnd.idle(min(remaining, PING_INTERVAL))
        if deadline - time.monotonic() <= 0:
            return
        for t in targets:
            if not t.connected:
                continue
            try:
                client.rpc(t, LUSTRE_OBD_VERSION, OBD_PING)
            except RpcError as e:
                log.warning("ping %s: %s", t.name, e)


def build_parser():
    """The command line parser."""
    ap = argparse.ArgumentParser(
        description="userspace Lustre client: LNet over TCP + PtlRPC "
        "mount/umount flow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--server",
        required=True,
        type=server_address,
        help="IPv4 address or host name of the MGS node (its NID is "
        "IP@tcp); MDTs and OSTs are expected on the same node",
    )
    ap.add_argument(
        "--fsname",
        help="filesystem name (default: found in the config logs of "
        "the MGS, when it serves a single filesystem)",
    )
    ap.add_argument(
        "--port",
        type=int,
        default=LNET_ACCEPTOR_PORT,
        help="LNet acceptor port (default %(default)s)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="seconds to wait for each reply (default %(default)s)",
    )
    ap.add_argument(
        "--hold",
        type=float,
        default=0.0,
        help="stay 'mounted' this many seconds before umount",
    )
    ap.add_argument("--cluuid", help="client UUID (default: random)")
    ap.add_argument(
        "--show-path",
        metavar="PATH",
        help="list PATH (relative to the filesystem root) like "
        "ls -a, or show a file's attributes",
    )
    ap.add_argument(
        "--copy-file",
        nargs=2,
        metavar=("PATH", "DEST"),
        help="copy the file at PATH (relative to the filesystem root) to local DEST",
    )
    ap.add_argument(
        "--fuse",
        metavar="MOUNTPOINT",
        help="mount the filesystem read-only at MOUNTPOINT "
        "through FUSE until it is unmounted (fusermount3 -u) "
        "or interrupted; needs python3-fusepy",
    )
    ap.add_argument(
        "--list-logs",
        action="store_true",
        help="print the registered targets from the recovery NID "
        "table and probe the well-known config log names",
    )
    ap.add_argument(
        "--nodemap",
        action="store_true",
        help="dump the whole nodemap database (MGS_CONFIG_READ type NODEMAP)",
    )
    ap.add_argument(
        "--probe-logs",
        nargs="?",
        const=FSNAME_PROBE_LOGS,
        type=int,
        metavar="N",
        help="open config logs by logid [0xa:1..N:0x0] (default "
        f"{FSNAME_PROBE_LOGS}) to find logs whose names cannot be guessed",
    )
    ap.add_argument(
        "--config-log",
        nargs="?",
        const="",
        metavar="LOGNAME",
        help="fetch and dump a config log by name (default: "
        "FSNAME-client) or by logid as shown by "
        "--probe-logs, e.g. '[0xa:0x14:0x0]'",
    )
    ap.add_argument(
        "--version",
        type=lustre_version,
        default=LUSTRE_VERSION_DEFAULT,
        help="Lustre version to claim in obd_connect_data "
        "(ocd_version); this is what a nodemap "
        "version_policy on the server matches against, so "
        "e.g. --version 2.15.3.0 tests an allow/hard_block "
        "glob from a spoofed client",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true", help="dump every LNet message"
    )
    return ap


def main():
    """Run the mount flow and the requested options; returns the exit status."""
    ap = build_parser()
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    needs_mdt = (
        args.show_path is not None
        or args.copy_file is not None
        or args.fuse is not None
    )
    mgs_only = (
        args.list_logs
        or args.nodemap
        or args.probe_logs is not None
        or args.config_log is not None
    )
    # with nothing asked for, the point is the mount flow itself
    mount_all = not needs_mdt and not mgs_only
    fusepy = None
    if args.fuse is not None:
        fusepy = load_fusepy()
        if fusepy is None:
            ap.error("--fuse needs fusepy (apt install python3-fusepy)")
        if not os.path.isdir(args.fuse):
            ap.error(f"{args.fuse}: not a directory")

    lnd = SockLND(args.server, args.port, args.timeout)
    try:
        lnd.connect()
    except OSError as e:
        log.error("cannot reach %s:%d over LNet: %s", args.server, args.port, e)
        return 1
    client = LustreClient(lnd, args.cluuid, args.version)
    log.info(
        "client uuid %s, claiming Lustre %s",
        client.cluuid,
        ocd_version_str(client.version),
    )

    mgs = mgs_target()
    targets = [mgs]
    rc = 0
    try:
        # -- MGS first: connect, then whatever was asked of the MGS ---------
        if not connect_target(client, mgs):
            log.error("cannot connect to the MGS, giving up")
            return 1
        needs_fsname = needs_mdt or mount_all or args.list_logs or args.config_log == ""
        if args.fsname is None and needs_fsname:
            args.fsname = discover_fsname(client, mgs)
            if args.fsname is None:
                return 1
        rc |= run_mgs_commands(client, mgs, args)

        # -- the rest of the mount, then what needs the namespace -----------
        if needs_mdt or mount_all:
            fs_targets, mdts, osts, ok = connect_filesystem(
                client, mgs, args.fsname, mount_all
            )
            targets += fs_targets
            if not ok:
                rc = 1
            if needs_mdt:
                if mdts is None:
                    log.error(
                        "%s-MDT0000 is not connected; cannot use the namespace",
                        args.fsname,
                    )
                    rc = 1
                else:
                    rc |= run_namespace_commands(client, mdts, osts, args, fusepy)

        mounted = [t.name for t in targets if t.connected]
        log.info(
            "mount flow done: %d/%d targets connected (%s)",
            len(mounted),
            len(targets),
            ", ".join(mounted),
        )
        if args.hold and mounted:
            log.info("holding connections for %.0fs", args.hold)
            hold(client, targets, args.hold)
    except (OSError, ProtocolError) as e:
        # a timeout, a lost connection or a reply that makes no sense, in
        # the middle of an option
        log.error("%s: %s", args.server, e)
        rc = 1
    finally:
        # -- umount: data, metadata, then MGS, mirroring the kernel ---------
        for t in reversed(targets):
            if not t.connected:
                continue
            try:
                client.disconnect(t)
            except (RpcError, OSError, ValueError) as e:
                log.error("%s: disconnect failed: %s", t.name, e)
                rc = 1
        lnd.close()
        log.info("umount flow done")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
