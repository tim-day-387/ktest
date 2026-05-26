#[macro_use]
mod macros;
mod mount;
mod umount;
mod zfs;

use clap::{Parser, Subcommand, ValueEnum};
use mount::{
    mount_client, mount_mds_combined, mount_mds_combined_zfs, mount_mds_split, mount_mds_split_zfs,
    mount_mgs, mount_mgs_zfs, mount_oss, mount_oss_zfs,
};
use umount::umount_all;

#[derive(Debug, Clone, Copy, PartialEq, ValueEnum)]
pub enum OsdType {
    /// In-kernel writeback cache filesystem (no block device needed)
    Wbcfs,
    /// ZFS datasets on ramdisks (pools must be pre-created by the caller)
    Zfs,
}

#[derive(Parser)]
#[command(name = "lustre-ktest")]
#[command(about = "Lustre filesystem testing utility")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Mount Lustre filesystem components
    Mount {
        /// Number of OSTs to mount
        #[arg(long, default_value_t = 2)]
        ost_count: u32,

        /// Number of MDSs to mount
        #[arg(long, default_value_t = 2)]
        mds_count: u32,

        /// Use a standalone MGS instead of combined MGS/MDT
        #[arg(long, default_value_t = false)]
        standalone_mgs: bool,

        /// OSD backend to use for Lustre targets
        #[arg(long, value_enum, default_value_t = OsdType::Wbcfs)]
        osd: OsdType,
    },
    /// Mount a standalone MGS only
    Mgs {
        /// OSD backend to use for the MGS target
        #[arg(long, value_enum, default_value_t = OsdType::Wbcfs)]
        osd: OsdType,
    },
    /// Unmount all Lustre filesystem components
    Umount,
}

fn main() {
    let cli = Cli::parse();

    match cli.command {
        Commands::Mount {
            ost_count,
            mds_count,
            standalone_mgs,
            osd,
        } => match osd {
            OsdType::Wbcfs => {
                if standalone_mgs {
                    mount_mds_split(mds_count);
                } else {
                    mount_mds_combined(mds_count);
                }
                mount_oss(ost_count);
                mount_client();
            }
            OsdType::Zfs => {
                let ost_ram_offset = if standalone_mgs {
                    mount_mds_split_zfs(mds_count)
                } else {
                    mount_mds_combined_zfs(mds_count)
                };
                mount_oss_zfs(ost_count, ost_ram_offset);
                mount_client();
            }
        },
        Commands::Mgs { osd } => match osd {
            OsdType::Wbcfs => mount_mgs(),
            OsdType::Zfs => mount_mgs_zfs(),
        },
        Commands::Umount => {
            umount_all();
        }
    }
}
