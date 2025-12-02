#[macro_use]
mod macros;
mod mount;
mod umount;

use clap::{Parser, Subcommand};
use mount::{mount_client, mount_mds_combined, mount_mds_split, mount_oss};
use umount::umount_all;

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
        } => {
            if standalone_mgs {
                mount_mds_split(mds_count);
            } else {
                mount_mds_combined(mds_count);
            }
            mount_oss(ost_count);
            mount_client();
        }
        Commands::Umount => {
            umount_all();
        }
    }
}
