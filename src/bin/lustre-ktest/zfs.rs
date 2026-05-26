use std::fs::OpenOptions;
use std::os::unix::io::AsRawFd;

const ZFS_DEV: &str = "/dev/zfs";

// ZFS ioctl numbers (raw, not _IOWR-encoded)
const ZFS_IOC_POOL_CREATE: u64 = 0x5a00;
const ZFS_IOC_POOL_DESTROY: u64 = 0x5a01;
const ZFS_IOC_CREATE: u64 = 0x5a17;

// NVList data_type_t constants from sys/nvpair.h
const DT_INT32: i32 = 5;
const DT_UINT64: i32 = 8;
const DT_STRING: i32 = 9;
const DT_NVLIST: i32 = 19;
const DT_NVLIST_ARRAY: i32 = 20;

// zfs_cmd_t field offsets and total size.
// Layout: char[4096] + 5 u64/i32 pairs + 8192 char + 256 char + u64 + u64 + u64 + ...
// Total: (MAXPATHLEN*3) + MAXNAMELEN + 1200 = 4096*3 + 256 + 1200 = 13744 bytes.
const ZFS_CMD_SIZE: usize = 13744;
const ZC_NAME_OFF: usize = 0; // char zc_name[4096]
const ZC_NVLIST_SRC_OFF: usize = 4096; // uint64_t zc_nvlist_src
const ZC_NVLIST_SRC_SIZE_OFF: usize = 4104; // uint64_t zc_nvlist_src_size
                                            // Skip zc_nvlist_dst(8), zc_nvlist_dst_size(8), zc_nvlist_dst_filled(4), zc_pad2(4),
                                            //      zc_history(8), zc_value[8192], zc_string[256], zc_guid(8)
const ZC_NVLIST_CONF_OFF: usize = 12600; // uint64_t zc_nvlist_conf
const ZC_NVLIST_CONF_SIZE_OFF: usize = 12608; // uint64_t zc_nvlist_conf_size

// Align x up to the next 8-byte boundary (ZFS NV_ALIGN).
fn nv_align(x: usize) -> usize {
    (x + 7) & !7
}

/// Builder for a native-encoded ZFS NVList.
///
/// The ZFS native (little-endian) packed format is:
///   [4 bytes header: 0,1,0,0]
///   [4 bytes nvl_version = 0]
///   [4 bytes nvl_nvflag = 1 (NV_UNIQUE_NAME)]
///   [pairs...]
///   [4 zero bytes terminator]
///
/// Each pair:
///   [4] nvp_size  = nv_align(16 + name_sz) + nv_align(value_sz)
///   [2] nvp_name_sz = strlen(name)+1
///   [2] nvp_reserve = 0
///   [4] nvp_value_elem = 1 (or N for arrays)
///   [4] nvp_type
///   [name_sz bytes] name + null
///   [padding to 8-byte align from pair start]
///   [value bytes, padded to 8 bytes]
///
/// For DATA_TYPE_NVLIST the value is a 24-byte nvlist_t struct (priv=0),
/// and the child's pairs + 4-byte terminator follow immediately after
/// nvp_size bytes (they are NOT included in nvp_size).
pub struct NvList {
    pairs: Vec<u8>,
}

impl NvList {
    pub fn new() -> Self {
        NvList { pairs: Vec::new() }
    }

    fn write_header(&mut self, name: &str, data_type: i32, elem_count: i32, value_sz: usize) {
        let nb = name.as_bytes();
        let name_sz = nb.len() + 1;
        let val_off = nv_align(16 + name_sz);
        let nvp_size = val_off + nv_align(value_sz);
        let p = &mut self.pairs;
        p.extend_from_slice(&(nvp_size as i32).to_le_bytes());
        p.extend_from_slice(&(name_sz as i16).to_le_bytes());
        p.extend_from_slice(&0i16.to_le_bytes());
        p.extend_from_slice(&elem_count.to_le_bytes());
        p.extend_from_slice(&data_type.to_le_bytes());
        p.extend_from_slice(nb);
        p.push(0u8);
        // padding between name and value
        let name_end = 16 + name_sz;
        p.extend(std::iter::repeat(0u8).take(val_off - name_end));
    }

    pub fn add_uint64(&mut self, name: &str, val: u64) {
        self.write_header(name, DT_UINT64, 1, 8);
        self.pairs.extend_from_slice(&val.to_le_bytes());
    }

    pub fn add_int32(&mut self, name: &str, val: i32) {
        self.write_header(name, DT_INT32, 1, 4);
        self.pairs.extend_from_slice(&val.to_le_bytes());
        // pad to 8 bytes
        self.pairs.extend_from_slice(&[0u8; 4]);
    }

    pub fn add_string(&mut self, name: &str, val: &str) {
        let vb = val.as_bytes();
        let str_sz = vb.len() + 1;
        self.write_header(name, DT_STRING, 1, str_sz);
        self.pairs.extend_from_slice(vb);
        self.pairs.push(0u8);
        let pad = nv_align(str_sz) - str_sz;
        self.pairs.extend(std::iter::repeat(0u8).take(pad));
    }

    /// Add an embedded nvlist pair.
    ///
    /// nvp_size covers the header + name + nvlist_t struct (24 bytes).
    /// The child's pairs and 4-byte terminator follow immediately after.
    pub fn add_nvlist(&mut self, name: &str, child: NvList) {
        // sizeof(nvlist_t) = int32 + uint32 + uint64 + uint32 + int32 = 24 bytes
        self.write_header(name, DT_NVLIST, 1, 24);
        // nvlist_t: version=0, nvflag=NV_UNIQUE_NAME=1, priv=0, flag=0, pad=0
        self.pairs.extend_from_slice(&0i32.to_le_bytes());
        self.pairs.extend_from_slice(&1u32.to_le_bytes());
        self.pairs.extend_from_slice(&0u64.to_le_bytes());
        self.pairs.extend_from_slice(&0u32.to_le_bytes());
        self.pairs.extend_from_slice(&0i32.to_le_bytes());
        // child pairs + terminator (outside nvp_size)
        self.pairs.extend(child.pairs);
        self.pairs.extend_from_slice(&[0u8; 4]);
    }

    /// Add an nvlist array pair.
    ///
    /// nvp_size covers header + name + N zeroed pointers + N nvlist_t structs.
    /// Each child's pairs + 4-byte terminator follow the static block.
    pub fn add_nvlist_array(&mut self, name: &str, children: Vec<NvList>) {
        let n = children.len();
        // value = N*8 (zeroed ptr slots) + N*24 (nvlist_t structs)
        let val_sz = n * 8 + n * 24;
        self.write_header(name, DT_NVLIST_ARRAY, n as i32, val_sz);
        // N zeroed uint64_t pointer slots
        for _ in 0..n {
            self.pairs.extend_from_slice(&0u64.to_le_bytes());
        }
        // N nvlist_t structs (version=0, nvflag=1, priv=0, flag=0, pad=0)
        for _ in 0..n {
            self.pairs.extend_from_slice(&0i32.to_le_bytes());
            self.pairs.extend_from_slice(&1u32.to_le_bytes());
            self.pairs.extend_from_slice(&0u64.to_le_bytes());
            self.pairs.extend_from_slice(&0u32.to_le_bytes());
            self.pairs.extend_from_slice(&0i32.to_le_bytes());
        }
        // value padding
        let pad = nv_align(val_sz) - val_sz;
        self.pairs.extend(std::iter::repeat(0u8).take(pad));
        // each child's pairs + terminator (outside nvp_size)
        for child in children {
            self.pairs.extend(child.pairs);
            self.pairs.extend_from_slice(&[0u8; 4]);
        }
    }

    /// Produce the packed nvlist buffer (header + pairs + outer terminator).
    pub fn pack(self) -> Vec<u8> {
        let mut buf = Vec::with_capacity(16 + self.pairs.len() + 4);
        // nvs_header_t: encoding=NV_ENCODE_NATIVE=0, endian=1 (LE), reserved=0,0
        buf.extend_from_slice(&[0u8, 1u8, 0u8, 0u8]);
        // nvl_version = NV_VERSION = 0
        buf.extend_from_slice(&0i32.to_le_bytes());
        // nvl_nvflag = NV_UNIQUE_NAME = 1
        buf.extend_from_slice(&1u32.to_le_bytes());
        buf.extend(self.pairs);
        // outer terminator (nvp_size=0 signals end of list)
        buf.extend_from_slice(&[0u8; 4]);
        buf
    }
}

/// Issue a ZFS ioctl on /dev/zfs.
///
/// `src`  → zc_nvlist_src / zc_nvlist_src_size
/// `conf` → zc_nvlist_conf / zc_nvlist_conf_size
fn zfs_ioctl(ioc: u64, name: &str, src: Option<&[u8]>, conf: Option<&[u8]>) -> Result<(), String> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(ZFS_DEV)
        .map_err(|e| format!("open {}: {}", ZFS_DEV, e))?;

    let mut zc = vec![0u8; ZFS_CMD_SIZE];

    let name_bytes = name.as_bytes();
    let copy_len = name_bytes.len().min(4095);
    zc[ZC_NAME_OFF..ZC_NAME_OFF + copy_len].copy_from_slice(&name_bytes[..copy_len]);

    if let Some(data) = src {
        let ptr = data.as_ptr() as u64;
        let sz = data.len() as u64;
        zc[ZC_NVLIST_SRC_OFF..ZC_NVLIST_SRC_OFF + 8].copy_from_slice(&ptr.to_ne_bytes());
        zc[ZC_NVLIST_SRC_SIZE_OFF..ZC_NVLIST_SRC_SIZE_OFF + 8].copy_from_slice(&sz.to_ne_bytes());
    }

    if let Some(data) = conf {
        let ptr = data.as_ptr() as u64;
        let sz = data.len() as u64;
        zc[ZC_NVLIST_CONF_OFF..ZC_NVLIST_CONF_OFF + 8].copy_from_slice(&ptr.to_ne_bytes());
        zc[ZC_NVLIST_CONF_SIZE_OFF..ZC_NVLIST_CONF_SIZE_OFF + 8].copy_from_slice(&sz.to_ne_bytes());
    }

    let ret = unsafe { libc::ioctl(file.as_raw_fd(), ioc as libc::c_ulong, zc.as_ptr()) };

    if ret != 0 {
        let e = std::io::Error::last_os_error();
        Err(format!("ioctl({:#x}) {} failed: {}", ioc, name, e))
    } else {
        Ok(())
    }
}

/// Create a ZFS pool with a single disk vdev.
///
/// Passes standard features in zc_nvlist_src and the vdev tree in zc_nvlist_conf.
/// Enabling features up-front matches what zpool(8) does by default and is
/// required for the properties we set on datasets (large_dnode for
/// dnodesize=auto, large_blocks for recordsize=1M).
pub fn zpool_create(pool: &str, device: &str) -> Result<(), String> {
    let mut disk = NvList::new();
    disk.add_string("type", "disk");
    disk.add_string("path", device);
    disk.add_uint64("whole_disk", 1);

    let mut nvroot = NvList::new();
    nvroot.add_string("type", "root");
    nvroot.add_nvlist_array("children", vec![disk]);

    let conf = nvroot.pack();

    let mut props = NvList::new();
    for feat in &[
        "async_destroy",
        "empty_bpobj",
        "lz4_compress",
        "multi_vdev_crash_dump",
        "spacemap_histogram",
        "enabled_txg",
        "hole_birth",
        "extensible_dataset",
        "embedded_data",
        "bookmarks",
        "filesystem_limits",
        "large_blocks",
        "large_dnode",
        "userobj_accounting",
        "project_quota",
        "device_removal",
        "obsolete_counts",
        "zpool_checkpoint",
        "allocation_classes",
        "resilver_defer",
        "log_spacemap",
        "livelist",
        "device_rebuild",
        "head_errlog",
        "zilsaxattr",
    ] {
        props.add_uint64(&format!("feature@{}", feat), 0);
    }
    let src = props.pack();

    zfs_ioctl(ZFS_IOC_POOL_CREATE, pool, Some(&src), Some(&conf))
}

/// Create a ZFS filesystem dataset.
///
/// Sets canmount=0 (off), xattr=2 (sa), dnodesize=1 (auto), compression=2 (off).
/// For OSTs also sets recordsize=1048576 (1 MiB).
/// Calls ZFS_IOC_CREATE with {type:2, props:{...}} in zc_nvlist_src.
pub fn zfs_dataset_create(dataset: &str, is_ost: bool) -> Result<(), String> {
    let mut props = NvList::new();
    props.add_uint64("canmount", 0); // ZFS_CANMOUNT_OFF
    props.add_uint64("xattr", 2); // ZFS_XATTR_SA
    props.add_uint64("dnodesize", 1); // ZFS_DNSIZE_AUTO
    props.add_uint64("compression", 2); // ZIO_COMPRESS_OFF
    if is_ost {
        props.add_uint64("recordsize", 1048576); // 1 MiB
    }

    let mut args = NvList::new();
    args.add_int32("type", 2); // LZC_DATSET_TYPE_ZFS = DMU_OST_ZFS = 2
    args.add_nvlist("props", props);

    let src = args.pack();
    zfs_ioctl(ZFS_IOC_CREATE, dataset, Some(&src), None)
}

/// Destroy a ZFS pool by name.
///
/// Returns Ok if the pool was destroyed or does not exist (ENOENT).
pub fn zpool_destroy(pool: &str) -> Result<(), String> {
    match zfs_ioctl(ZFS_IOC_POOL_DESTROY, pool, None, None) {
        Ok(()) => Ok(()),
        Err(e) if e.contains("No such file or directory") || e.contains("os error 2") => Ok(()),
        Err(e) => Err(e),
    }
}

/// Destroy all lustre-mdt*, lustre-ost*, and lustre-mgs ZFS pools.
pub fn zpool_destroy_lustre_pools() {
    // Destroy MGS pool (standalone MGS case)
    let _ = zpool_destroy("lustre-mgs");

    // Destroy MDT pools until first failure
    for i in 0..64 {
        let pool = format!("lustre-mdt{}", i);
        if zfs_ioctl(ZFS_IOC_POOL_DESTROY, &pool, None, None).is_err() {
            break;
        }
    }

    // Destroy OST pools until first failure
    for i in 0..64 {
        let pool = format!("lustre-ost{}", i);
        if zfs_ioctl(ZFS_IOC_POOL_DESTROY, &pool, None, None).is_err() {
            break;
        }
    }
}
