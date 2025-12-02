use colored::Colorize;

pub const LNET_INTF: &str = "10.0.2.15@tcp";
pub const FSNAME: &str = "lustre";

pub trait TestCall<T, E> {
    fn test_call(self) -> Result<T, E>;
}

impl<T, E: std::fmt::Display> TestCall<T, E> for Result<T, E> {
    fn test_call(self) -> Result<T, E> {
        match &self {
            Ok(_) => println!("{}", "PASS".green()),
            Err(e) => println!("{} with {}", "FAIL".red(), e.to_string().red()),
        }
        self
    }
}

macro_rules! boldln {
    ($($arg:tt)*) => {
        println!("{}", format!($($arg)*).bold());
    };
}
