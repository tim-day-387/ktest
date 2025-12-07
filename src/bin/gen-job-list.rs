extern crate libc;
use std::collections::{BTreeMap, HashSet};
use std::ffi::OsStr;
use std::fs::File;
use std::io::prelude::*;
use std::path::{Path, PathBuf};
use std::process;
use ci_cgi::{CiConfig, Userrc, RcTestGroup, ciconfig_read, git_get_commit, commitdir_get_results, lockfile_exists, commit_update_results_from_fs, subtest_full_name, test_stats, users::RcBranch};
use ci_cgi::TestResultsMap;
use file_lock::{FileLock, FileOptions};
use memmap::MmapOptions;
use memoize::memoize;
use anyhow;
use chrono::Utc;
use clap::Parser;

#[memoize]
fn get_subtests(test_path: PathBuf) -> Vec<String> {
    let output = std::process::Command::new(&test_path)
        .arg("list-tests")
        .output()
        .expect(&format!("failed to execute process {:?} ", &test_path))
        .stdout;
    let output = String::from_utf8_lossy(&output);

    output
        .split_whitespace()
        .map(|i| i.to_string())
        .collect()
}

#[derive(Debug)]
pub struct TestJob {
    branch:     String,
    commit:     String,
    age:        u64,
    nice:       u64,
    duration:   u64,
    test:       String,
    subtest:    String,
}

fn testjob_weight(j: &TestJob) -> u64 {
    j.age + j.nice
}

use std::cmp::Ordering;

impl Ord for TestJob {
    fn cmp(&self, other: &Self) -> Ordering {
        testjob_weight(self).cmp(&testjob_weight(other))
            .then(self.commit.cmp(&other.commit))
            .then(self.test.cmp(&other.test))
            .then(self.duration.cmp(&other.duration))
    }
}

impl PartialOrd for TestJob {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> { Some(self.cmp(other)) }
}

impl PartialEq for TestJob {
    fn eq(&self, other: &Self) -> bool { self.cmp(other) == Ordering::Equal }
}

impl Eq for TestJob {}

fn have_result(results: &TestResultsMap, subtest: &str) -> bool {
    use ci_cgi::TestStatus;

    let r = results.get(subtest);
    if let Some(r) = r {
        let elapsed = Utc::now() - r.starttime;
        let timeout = chrono::Duration::minutes(30);

        r.status != TestStatus::Inprogress || elapsed < timeout
    } else {
        false
    }
}

fn branch_test_jobs(rc: &CiConfig,
                    durations: Option<&[u8]>,
                    repo: &git2::Repository,
                    user: &str,
                    branch: &str,
                    test_group: &RcTestGroup,
                    test_path: &Path,
                    verbose: bool) -> Vec<TestJob> {
    let test_name = &test_path.to_string_lossy();
    let full_test_path = rc.ktest.ktest_dir.join("tests").join(test_path);
    let mut ret = Vec::new();

    let subtests = get_subtests(full_test_path);

    if verbose {
        eprintln!("looking for tests to run for branch {} test {:?} subtests {:?}",
                  branch, test_path, subtests)
    }

    let userbranch = user.to_string() + "/" + branch;

    let mut walk = repo.revwalk().unwrap();
    let reference = git_get_commit(&repo, userbranch.clone());
    if reference.is_err() {
        eprintln!("branch {} not found", &userbranch);
        return ret;
    }
    let reference = reference.unwrap();

    if let Err(e) = walk.push(reference.id()) {
        eprintln!("Error walking {}: {}", &userbranch, e);
        return ret;
    }

    let mut commits_updated = HashSet::new();

    for (age, commit) in walk
            .filter_map(|i| i.ok())
            .filter_map(|i| repo.find_commit(i).ok())
            .take(test_group.max_commits as usize)
            .enumerate() {
        let commit = commit.id().to_string();

        let results = commitdir_get_results(&rc.ktest, &commit).unwrap_or(BTreeMap::new());

        if verbose { eprintln!("at commit {} age {}\nresults {:?}",
            &commit, age, results) }

        for subtest in subtests
            .iter()
            .filter(|i| {
                let full_subtest_name = subtest_full_name(&test_name, &i);

                !have_result(&results, &full_subtest_name) &&
                    !lockfile_exists(&rc.ktest, &commit, &full_subtest_name,
                                     false,
                                     &mut commits_updated)
            })
            .map(|i| i.clone()) {
                let mut nice = test_group.nice;

                let stats = test_stats(durations, test_name, &subtest);
                if let Some(ref stats) = stats {
                    if test_group.test_always_passes_nice != 0 &&
                       !stats.passed != !stats.failed  &&
                       stats.passed + stats.failed > test_group.test_always_passes_nice {
                        nice += test_group.test_always_passes_nice;
                    }

                    if test_group.test_duration_nice != 0 {
                        nice += stats.duration / test_group.test_duration_nice;
                    }
                }

                ret.push(TestJob {
                    branch:     branch.to_string(),
                    commit:     commit.clone(),
                    age:        age as u64,
                    nice,
                    duration:   stats.map_or(rc.ktest.subtest_duration_def, |s| s.duration),
                    test:       test_name.to_string(),
                    subtest,
                });
        }
    }

    for i in commits_updated.iter() {
        commit_update_results_from_fs(&rc.ktest, &i);
    }

    ret
}

fn user_test_jobs(rc: &CiConfig,
                  durations: Option<&[u8]>,
                  repo: &git2::Repository,
                  user: &str,
                  userconfig: &Userrc,
                  verbose: bool) -> Vec<TestJob> {
    eprintln!("DEBUG: user_test_jobs called for user: {}", user);
    eprintln!("DEBUG: User has {} branches", userconfig.branch.len());
    for (branch_name, _) in &userconfig.branch {
        eprintln!("DEBUG: User {} has branch: {}", user, branch_name);
    }
    eprintln!("DEBUG: User has {} test groups", userconfig.test_group.len());
    for (group_name, _) in &userconfig.test_group {
        eprintln!("DEBUG: User {} has test group: {}", user, group_name);
    }
    
    let mut ret = Vec::new();
    
    for (branch, branchconfig) in &userconfig.branch {
        eprintln!("DEBUG: Branch {} has {} tests", branch, branchconfig.tests.len());
        
        for test_group_name in &branchconfig.tests {
            match userconfig.test_group.get(test_group_name) {
                Some(testgroup) => {
                    eprintln!("DEBUG: Processing branch '{}' with test group '{}' containing {} tests", 
                             branch, test_group_name, testgroup.tests.len());
                    
                    for test in &testgroup.tests {
                        eprintln!("DEBUG: Running branch_test_jobs for user={}, branch={}, test={:?}", 
                                 user, branch, test);
                        let jobs = branch_test_jobs(rc, durations, repo, user, branch, testgroup, test, verbose);
                        eprintln!("DEBUG: branch_test_jobs returned {} jobs", jobs.len());
                        ret.extend(jobs);
                    }
                },
                None => {
                    eprintln!("DEBUG: Test group '{}' not found for branch '{}'", test_group_name, branch);
                }
            }
        }
    }

    eprintln!("DEBUG: user_test_jobs for {} collected {} jobs before sort", user, ret.len());

    /* sort by commit, dedup */

    ret.sort();
    ret.reverse();
    ret
}

fn rc_test_jobs(rc: &CiConfig,
                durations: Option<&[u8]>,
                repo: &git2::Repository,
                verbose: bool) -> Vec<TestJob> {
    eprintln!("DEBUG: rc_test_jobs called");
    eprintln!("DEBUG: Total users in config: {}", rc.users.len());
    
    let valid_users: Vec<_> = rc.users.iter()
        .filter(|u| u.1.is_ok())
        .map(|(user, userconfig)| (user, userconfig.as_ref().unwrap()))
        .collect();
    
    eprintln!("DEBUG: Valid users (with OK config): {}", valid_users.len());
    for (user, _) in &valid_users {
        eprintln!("DEBUG: Valid user: {}", user);
    }
    
    let mut ret: Vec<_> = valid_users.iter()
        .flat_map(|(user, userconfig)| {
            eprintln!("DEBUG: Processing user: {}", user);
            let jobs = user_test_jobs(rc, durations, repo, &user, &userconfig, verbose);
            eprintln!("DEBUG: User {} returned {} jobs", user, jobs.len());
            jobs
        })
        .collect();

    eprintln!("DEBUG: Total jobs before sort: {}", ret.len());

    /* sort by commit, dedup */

    ret.sort();
    ret.reverse();
    
    eprintln!("DEBUG: Total jobs after sort: {}", ret.len());
    ret
}

fn write_test_jobs(rc: &CiConfig, jobs_in: Vec<TestJob>, verbose: bool) -> anyhow::Result<()> {
    eprintln!("Writing {} test jobs...", jobs_in.len());

    if verbose {
        eprint!("jobs: {:?}", jobs_in);
    }

    let jobs_fname      = rc.ktest.output_dir.join("jobs");
    let jobs_fname_new  = rc.ktest.output_dir.join("jobs.new");
    let mut jobs_out    = std::io::BufWriter::new(File::create(&jobs_fname_new)?);

    for job in jobs_in.iter() {
        jobs_out.write(job.branch.as_bytes())?;
        jobs_out.write(b" ")?;
        jobs_out.write(job.commit.as_bytes())?;
        jobs_out.write(b" ")?;
        jobs_out.write(job.age.to_string().as_bytes())?;
        jobs_out.write(b" ")?;
        jobs_out.write(job.test.as_bytes())?;
        jobs_out.write(b" ")?;
        jobs_out.write(job.subtest.as_bytes())?;
        jobs_out.write(b"\n")?;
    }

    jobs_out.flush()?;
    drop(jobs_out);
    std::fs::rename(jobs_fname_new, jobs_fname)?;
    Ok(())
}

fn fetch_remotes(rc: &CiConfig, repo: &git2::Repository) -> anyhow::Result<bool> {
    fn fetch_branch(rc: &CiConfig, repo: &git2::Repository,
                    user: &str, branch: &str, branchconfig: &RcBranch) -> Result<(), git2::Error> {
        let fetch = branchconfig.fetch
            .split_whitespace()
            .map(|i| OsStr::new(i));

        let status = std::process::Command::new("git")
            .arg("-C")
            .arg(&rc.ktest.linux_repo)
            .arg("fetch")
            .args(fetch)
            .status()
            .expect(&format!("failed to execute fetch"));
        if !status.success() {
            eprintln!("fetch error for {}: {}", branchconfig.fetch, status);
            return Ok(());
        }

        let fetch_head = repo.revparse_single("FETCH_HEAD")
            .map_err(|e| { eprintln!("error parsing FETCH_HEAD: {}", e); e})?
            .peel_to_commit()
            .map_err(|e| { eprintln!("error getting FETCH_HEAD: {}", e); e})?;

        repo.branch(&(user.to_string() + "/"  + branch), &fetch_head, true)?;
        Ok(())
    }

    let lockfile = rc.ktest.output_dir.join("fetch.lock");
    let metadata = std::fs::metadata(&lockfile);
    if let Ok(metadata) = metadata {
        let elapsed = metadata.modified().unwrap()
            .elapsed()
            .unwrap_or_default();

        if elapsed < std::time::Duration::from_secs(30) {
            return Ok(false);
        }
    }

    let mut filelock = FileLock::lock(lockfile, false, FileOptions::new().create(true).write(true))?;

    eprint!("Fetching remotes...");
    for (user, userconfig) in rc.users.iter().filter(|u| u.1.is_ok())
            .map(|(user, userconfig)| (user, userconfig.as_ref().unwrap())) {
        for (branch, branchconfig) in &userconfig.branch {
            fetch_branch(rc, repo, user, branch, branchconfig).ok();
        }
    }
    eprintln!(" done");

    filelock.file.write_all(b"ok")?; /* update lockfile mtime */

    /*
     * XXX: return true only if remotes actually changed
     */
    Ok(true)
}

fn update_jobs(rc: &CiConfig, args: &Args, repo: &git2::Repository) -> anyhow::Result<()> {
    if fetch_remotes(rc, repo).ok() == Some(false) && !args.force_update_jobs {
        eprintln!("remotes unchanged, skipping updating joblist");
        return Ok(());
    }

    let durations_file = File::open(rc.ktest.output_dir.join("test_durations.capnp")).ok();
    let durations_map = durations_file.map(|x| unsafe { MmapOptions::new().map(&x).ok() } ).flatten();
    let durations = durations_map.as_ref().map(|x| x.as_ref());

    let lockfile = rc.ktest.output_dir.join("jobs.lock");
    let filelock = FileLock::lock(lockfile, true, FileOptions::new().create(true).write(true))?;

    let jobs_in = rc_test_jobs(rc, durations, repo, false);
    write_test_jobs(rc, jobs_in, args.verbose)?;

    drop(filelock);

    Ok(())
}

#[derive(Parser)]
#[command(author, version, about, long_about = None)]
struct Args {
    #[arg(short, long)]
    verbose:            bool,
    force_update_jobs:  bool,
}

fn main() {
    let args = Args::parse();

    let rc = ciconfig_read();
    if let Err(e) = rc {
        eprintln!("could not read config; {}", e);
        process::exit(1);
    }
    let rc = rc.unwrap();

    let repo = git2::Repository::open(&rc.ktest.linux_repo);
    if let Err(e) = repo {
        eprintln!("Error opening {:?}: {}", rc.ktest.linux_repo, e);
        eprintln!("Please specify correct linux_repo");
        process::exit(1);
    }
    let repo = repo.unwrap();

    if let Err(e) = update_jobs(&rc, &args, &repo) {
        eprintln!("update_jobs() error: {}", e);
    }
}
