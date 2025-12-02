use crate::macros::TestCall;
use colored::Colorize;
use regex::Regex;
use std::ffi::CString;
use std::fs;
use std::path::Path;
use syscalls::{Sysno, syscall};

// Umount flags from linux/fs.h
const MNT_FORCE: u32 = 0x00000001; // Force unmounting
const MNT_DETACH: u32 = 0x00000002; // Lazy unmount

#[derive(Debug, Clone)]
pub struct UmountOpts {
    pub mount_point: String,
    pub force: bool,
    pub lazy: bool,
    pub verbose: bool,
    pub fake: bool,
}

impl Default for UmountOpts {
    fn default() -> Self {
        Self {
            mount_point: String::new(),
            force: false,
            lazy: false,
            verbose: false,
            fake: false,
        }
    }
}

pub fn check_is_mounted(mount_point: &str) -> Result<bool, String> {
    let mtab_path = "/proc/mounts";
    let contents = fs::read_to_string(mtab_path)
        .map_err(|e| format!("Failed to read {}: {}", mtab_path, e))?;

    for line in contents.lines() {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() >= 3 {
            let mount_dir = fields[1];
            let mount_type = fields[2];

            if mount_dir == mount_point && (mount_type == "lustre" || mount_type == "lustre_tgt") {
                return Ok(true);
            }
        }
    }

    Ok(false)
}

pub fn umount_lustre_with_opts(opts: &UmountOpts) -> Result<(), String> {
    // Check if mount point exists
    if !Path::new(&opts.mount_point).exists() {
        return Err(format!("Mount point {} does not exist", opts.mount_point));
    }

    // Check if actually mounted
    if !check_is_mounted(&opts.mount_point)? {
        if opts.verbose {
            println!("{} is not mounted", opts.mount_point);
        }
        return Ok(());
    }

    if opts.fake {
        // Fake umount - just print what would be done
        println!(
            "Would unmount {} (force: {}, lazy: {})",
            opts.mount_point, opts.force, opts.lazy
        );
        return Ok(());
    }

    let c_mount_point =
        CString::new(opts.mount_point.as_str()).map_err(|_| "Invalid mount point path")?;

    // Build umount flags
    let mut flags = 0u32;
    if opts.force {
        flags |= MNT_FORCE;
    }
    if opts.lazy {
        flags |= MNT_DETACH;
    }

    if opts.verbose {
        let flag_desc = match (opts.force, opts.lazy) {
            (true, true) => " (force + lazy)",
            (true, false) => " (force)",
            (false, true) => " (lazy)",
            (false, false) => "",
        };
        println!("Unmounting {}{}", opts.mount_point, flag_desc);
    }

    let result = unsafe {
        syscall!(
            Sysno::umount2,
            c_mount_point.as_ptr() as usize,
            flags as usize
        )
    };

    match result {
        Ok(_) => {
            if opts.verbose {
                println!("Successfully unmounted {}", opts.mount_point);
            }
            Ok(())
        }
        Err(errno) => {
            // Provide more detailed error messages like the C implementation
            let error_msg = match errno.into_raw() {
                16 => "Device or resource busy".to_string(),  // EBUSY
                2 => "No such file or directory".to_string(), // ENOENT
                22 => "Invalid argument".to_string(),         // EINVAL
                1 => "Operation not permitted".to_string(),   // EPERM
                _ => format!("Umount failed with errno {}", errno),
            };

            Err(format!(
                "Failed to unmount {}: {}",
                opts.mount_point, error_msg
            ))
        }
    }
}

pub fn umount_lustre(mount_point: &str, force: bool) -> Result<(), String> {
    let opts = UmountOpts {
        mount_point: mount_point.to_string(),
        force,
        ..Default::default()
    };

    umount_lustre_with_opts(&opts)
}

pub fn umount_lustre_force_lazy(mount_point: &str) -> Result<(), String> {
    let opts = UmountOpts {
        mount_point: mount_point.to_string(),
        force: true,
        lazy: true,
        ..Default::default()
    };

    umount_lustre_with_opts(&opts)
}

#[derive(Debug, Clone)]
pub struct LustreMountPoint {
    pub path: String,
    pub service_type: String,
    pub index: String,
}

pub fn discover_lustre_mounts() -> Result<Vec<LustreMountPoint>, String> {
    let mnt_dir = Path::new("/mnt");
    if !mnt_dir.exists() {
        return Ok(Vec::new());
    }

    let pattern = Regex::new(r"^lustre-[a-zA-Z0-9]{6}-(OST|MDT|MGS)(\d{4})$")
        .map_err(|e| format!("Failed to compile regex: {}", e))?;

    let mut mounts = Vec::new();

    let entries =
        fs::read_dir(mnt_dir).map_err(|e| format!("Failed to read /mnt directory: {}", e))?;

    for entry in entries {
        let entry = entry.map_err(|e| format!("Failed to read directory entry: {}", e))?;
        let dir_name = entry.file_name();
        let dir_name_str = dir_name.to_string_lossy();

        if let Some(captures) = pattern.captures(&dir_name_str) {
            let service_type = captures.get(1).unwrap().as_str().to_string();
            let index = captures.get(2).unwrap().as_str().to_string();
            let path = format!("/mnt/{}", dir_name_str);

            if check_is_mounted(&path).unwrap_or(false) {
                mounts.push(LustreMountPoint {
                    path,
                    service_type,
                    index,
                });
            }
        }
    }

    Ok(mounts)
}

pub fn umount_all() {
    boldln!("Unmount of client...");
    umount_lustre("/mnt/lustre", false).test_call().ok();

    let mounts = match discover_lustre_mounts() {
        Ok(mounts) => mounts,
        Err(e) => {
            println!("Error discovering lustre mounts: {}", e);
            return;
        }
    };

    let mut ost_mounts: Vec<_> = mounts.iter().filter(|m| m.service_type == "OST").collect();
    ost_mounts.sort_by(|a, b| a.index.cmp(&b.index));

    for mount in ost_mounts {
        boldln!("Force + lazy unmount of OST{}...", mount.index);
        umount_lustre_force_lazy(&mount.path).test_call().ok();

        if let Err(e) = fs::remove_dir(&mount.path) {
            println!("Warning: Failed to remove directory {}: {}", mount.path, e);
        } else {
            println!("Removed directory: {}", mount.path);
        }
    }

    let mut mdt_mounts: Vec<_> = mounts.iter().filter(|m| m.service_type == "MDT").collect();
    mdt_mounts.sort_by(|a, b| b.index.cmp(&a.index));

    for mount in mdt_mounts {
        boldln!("Force + lazy unmount of MDT{}...", mount.index);
        umount_lustre_force_lazy(&mount.path).test_call().ok();

        if let Err(e) = fs::remove_dir(&mount.path) {
            println!("Warning: Failed to remove directory {}: {}", mount.path, e);
        } else {
            println!("Removed directory: {}", mount.path);
        }
    }

    let mut mgs_mounts: Vec<_> = mounts.iter().filter(|m| m.service_type == "MGS").collect();
    mgs_mounts.sort_by(|a, b| a.index.cmp(&b.index));

    for mount in mgs_mounts {
        boldln!("Force + lazy unmount of MGS{}...", mount.index);
        umount_lustre_force_lazy(&mount.path).test_call().ok();

        if let Err(e) = fs::remove_dir(&mount.path) {
            println!("Warning: Failed to remove directory {}: {}", mount.path, e);
        } else {
            println!("Removed directory: {}", mount.path);
        }
    }
}
