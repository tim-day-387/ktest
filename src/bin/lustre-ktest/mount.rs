use crate::macros::{FSNAME, LNET_INTF, TestCall};
use colored::Colorize;
use rand::distributions::Alphanumeric;
use rand::{Rng, thread_rng};
use std::ffi::CString;
use std::fs;
use std::path::Path;
use syscalls::{Sysno, syscall};

#[derive(Debug, Clone)]
pub struct MountOpts {
    pub device: String,
    pub mount_point: String,
    pub fstype: String,
    pub options: Vec<String>,
    pub force: bool,
    pub verbose: bool,
    pub retry: u32,
    pub fake: bool,
}

impl Default for MountOpts {
    fn default() -> Self {
        Self {
            device: String::new(),
            mount_point: String::new(),
            fstype: "lustre".to_string(),
            options: Vec::new(),
            force: false,
            verbose: false,
            retry: 0,
            fake: false,
        }
    }
}

pub fn check_mtab_entry(
    spec1: &str,
    spec2: &str,
    mount_point: Option<&str>,
) -> Result<bool, String> {
    let mtab_path = "/proc/mounts";
    let contents = fs::read_to_string(mtab_path)
        .map_err(|e| format!("Failed to read {}: {}", mtab_path, e))?;

    for line in contents.lines() {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() >= 3 {
            let mount_dev = fields[0];
            let mount_dir = fields[1];
            let mount_type = fields[2];

            if (compare_lustre_sources(mount_dev, spec1)
                || compare_lustre_sources(mount_dev, spec2))
                && mount_point.map_or(true, |mp| mount_dir == mp)
                && mount_type == "lustre"
            {
                return Ok(true);
            }
        }
    }

    Ok(false)
}

fn compare_lustre_sources(src1: &str, src2: &str) -> bool {
    let fs1 = src1.find(":/").map(|i| &src1[i + 2..]).unwrap_or(src1);
    let fs2 = src2.find(":/").map(|i| &src2[i + 2..]).unwrap_or(src2);
    fs1 == fs2
}

pub fn parse_options(options_str: &str) -> Vec<String> {
    if options_str.is_empty() {
        return Vec::new();
    }

    options_str
        .split(',')
        .map(|s| s.trim().to_string())
        .collect()
}

pub fn convert_hostnames(source: &str) -> Result<String, String> {
    // For now, return source as-is. In full implementation,
    // this would resolve hostnames to IP addresses
    Ok(source.to_string())
}

pub fn generate_random_mount_point(service_type: &str, index: &str) -> String {
    let random_suffix: String = thread_rng()
        .sample_iter(&Alphanumeric)
        .take(6)
        .map(char::from)
        .collect();

    format!("/mnt/lustre-{}-{}{}", random_suffix, service_type, index)
}

pub fn get_service_name(fsname: &str, service_type: &str, index: u32) -> String {
    format!("{}-{}{:04X}", fsname, service_type, index)
}

pub fn append_option(options: &mut Vec<String>, key: &str, value: Option<&str>) {
    match value {
        Some(val) => options.push(format!("{}={}", key, val)),
        None => options.push(key.to_string()),
    }
}

pub fn mount_lustre_client(
    mgs_target: &str,
    mount_point: &str,
    options: Option<&str>,
) -> Result<(), String> {
    let mut mop = MountOpts::default();
    mop.mount_point = mount_point.to_string();

    // Parse MGS target (format: mgsnid:filesystem[/subdir])
    let converted_source = convert_hostnames(mgs_target)?;
    mop.device = converted_source;

    if let Some(opts) = options {
        mop.options = parse_options(opts);
    }

    // Add device option (required for Lustre)
    append_option(&mut mop.options, "device", Some(&mop.device));

    mount_lustre_internal(&mop)
}

pub fn mount_lustre_mdt_with_mgs(
    device: &str,
    mount_point: &str,
    mgs_node: &str,
    sv_name: &str,
) -> Result<(), String> {
    let mut mop = MountOpts::default();
    mop.device = device.to_string();
    mop.mount_point = mount_point.to_string();
    mop.fstype = "lustre_tgt".to_string();

    // Add required options for combined MGS/MDT creation
    append_option(&mut mop.options, "osd", Some("osd-wbcfs"));
    append_option(&mut mop.options, "mgs", None);
    append_option(&mut mop.options, "virgin", None);
    append_option(&mut mop.options, "update", None);
    append_option(
        &mut mop.options,
        "param",
        Some(&format!("mgsnode={}", mgs_node)),
    );
    append_option(&mut mop.options, "svname", Some(sv_name));
    append_option(&mut mop.options, "device", Some(device));

    mount_lustre_internal(&mop)
}

pub fn mount_lustre_ost(
    device: &str,
    mount_point: &str,
    mgs_node: &str,
    sv_name: &str,
) -> Result<(), String> {
    let mut mop = MountOpts::default();
    mop.device = device.to_string();
    mop.mount_point = mount_point.to_string();
    mop.fstype = "lustre_tgt".to_string();

    // Add required options for OST creation
    append_option(&mut mop.options, "osd", Some("osd-wbcfs"));
    append_option(&mut mop.options, "mgsnode", Some(mgs_node));
    append_option(&mut mop.options, "virgin", None);
    append_option(&mut mop.options, "update", None);
    append_option(&mut mop.options, "svname", Some(sv_name));
    append_option(&mut mop.options, "device", Some(device));

    mount_lustre_internal(&mop)
}

pub fn mount_lustre_mgs(
    device: &str,
    mount_point: &str,
    sv_name: &str,
) -> Result<(), String> {
    let mut mop = MountOpts::default();
    mop.device = device.to_string();
    mop.mount_point = mount_point.to_string();
    mop.fstype = "lustre_tgt".to_string();

    // Add required options for MDT creation
    append_option(&mut mop.options, "osd", Some("osd-wbcfs"));
    append_option(&mut mop.options, "virgin", None);
    append_option(&mut mop.options, "update", None);
    append_option(&mut mop.options, "mgs", None);
    append_option(&mut mop.options, "svname", Some(sv_name));
    append_option(&mut mop.options, "device", Some(device));

    mount_lustre_internal(&mop)
}

pub fn mount_lustre_mdt(
    device: &str,
    mount_point: &str,
    mgs_node: &str,
    sv_name: &str,
) -> Result<(), String> {
    let mut mop = MountOpts::default();
    mop.device = device.to_string();
    mop.mount_point = mount_point.to_string();
    mop.fstype = "lustre_tgt".to_string();

    // Add required options for MDT creation
    append_option(&mut mop.options, "osd", Some("osd-wbcfs"));
    append_option(&mut mop.options, "virgin", None);
    append_option(&mut mop.options, "update", None);
    append_option(&mut mop.options, "mgsnode", Some(mgs_node));
    append_option(&mut mop.options, "svname", Some(sv_name));
    append_option(&mut mop.options, "device", Some(device));

    mount_lustre_internal(&mop)
}

fn mount_lustre_internal(mop: &MountOpts) -> Result<(), String> {
    // Create mount point directory if it doesn't exist
    if !Path::new(&mop.mount_point).exists() {
        if let Err(e) = fs::create_dir_all(&mop.mount_point) {
            return Err(format!(
                "Failed to create mount point {}: {}",
                mop.mount_point, e
            ));
        }
        if mop.verbose {
            println!("Created mount point directory: {}", mop.mount_point);
        }
    }

    // Check if already mounted (unless force flag is set)
    if !mop.force {
        if check_mtab_entry(&mop.device, &mop.device, Some(&mop.mount_point))? {
            return Err(format!(
                "{} is already mounted on {}",
                mop.device, mop.mount_point
            ));
        }
    }

    if mop.fake {
        // Fake mount - just print what would be done
        println!(
            "Would mount {} at {} with options: {:?}",
            mop.device, mop.mount_point, mop.options
        );
        return Ok(());
    }

    // Prepare mount options string
    let options_str = mop.options.join(",");

    let c_device = CString::new(mop.device.as_str()).map_err(|_| "Invalid device path")?;
    let c_mount_point =
        CString::new(mop.mount_point.as_str()).map_err(|_| "Invalid mount point path")?;
    let c_fstype = CString::new(mop.fstype.as_str()).map_err(|_| "Invalid filesystem type")?;

    let c_options = if !options_str.is_empty() {
        Some(CString::new(options_str.clone()).map_err(|_| "Invalid mount options")?)
    } else {
        None
    };

    let options_ptr = match &c_options {
        Some(opts) => opts.as_ptr() as usize,
        None => 0,
    };

    if mop.verbose {
        println!(
            "Mounting {} at {} with fstype={} options={}",
            mop.device, mop.mount_point, mop.fstype, options_str
        );
    }

    // Retry logic
    let max_retries = if mop.retry > 0 { mop.retry } else { 1 };

    for attempt in 0..max_retries {
        let result = unsafe {
            syscall!(
                Sysno::mount,
                c_device.as_ptr() as usize,
                c_mount_point.as_ptr() as usize,
                c_fstype.as_ptr() as usize,
                0usize, // mountflags (could be extended to support MS_RDONLY, etc.)
                options_ptr
            )
        };

        match result {
            Ok(_) => {
                if mop.verbose {
                    println!("Successfully mounted {} on {}", mop.device, mop.mount_point);
                }
                return Ok(());
            }
            Err(errno) => {
                if attempt == max_retries - 1 {
                    // Last attempt failed
                    return Err(format!(
                        "Mount failed after {} attempts: {}",
                        max_retries, errno
                    ));
                } else if mop.verbose {
                    println!(
                        "Mount attempt {} failed: {}, retrying...",
                        attempt + 1,
                        errno
                    );
                }

                // Sleep before retry (exponential backoff)
                std::thread::sleep(std::time::Duration::from_secs(1 << (attempt.min(5))));
            }
        }
    }

    unreachable!()
}

fn mount_mds(start: u32, end: u32) {
    for index in start..end {
        let sv_name = get_service_name(FSNAME, "MDT", index);

        boldln!("Mounting MDT {}...", sv_name);

        let mount_point = generate_random_mount_point("MDT", &format!("{:04X}", index));

        mount_lustre_mdt("lustre-wbcfs", &mount_point, LNET_INTF, &sv_name)
            .test_call()
            .ok();
    }
}

pub fn mount_mds_combined(mds_count: u32) {
    boldln!("Mount combined MGS/MDS...");

    let mdt0_mount = generate_random_mount_point("MDT", "0000");
    mount_lustre_mdt_with_mgs("lustre-wbcfs", &mdt0_mount, LNET_INTF, "lustre-MDT0000")
        .test_call()
        .ok();

    mount_mds(1, mds_count);
}

pub fn mount_mds_split(mds_count: u32) {
    boldln!("Mounting separate MGS...");

    let mgs_mount = generate_random_mount_point("MGS", "0000");
    mount_lustre_mgs("lustre-wbcfs", &mgs_mount, "MGS0000")
        .test_call()
        .ok();

    mount_mds(0, mds_count);
}

pub fn mount_oss(end: u32) {
    for index in 0..end {
        let sv_name = get_service_name(FSNAME, "OST", index);

        boldln!("Mounting OST {}...", sv_name);

        let mount_point = generate_random_mount_point("OST", &format!("{:04X}", index));

        mount_lustre_ost("lustre-wbcfs", &mount_point, LNET_INTF, &sv_name)
            .test_call()
            .ok();
    }
}

pub fn mount_client() {
    let mgs_target = format!("{}:/lustre", LNET_INTF);
    let mount_point = "/mnt/lustre";
    let options = Some("flock,user_xattr");

    boldln!("Mounting Lustre client {}...", mgs_target);

    mount_lustre_client(&mgs_target, mount_point, options)
        .test_call()
        .ok();
}
