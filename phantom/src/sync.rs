//! Workspace mirror for a phantom box that doesn't share volumes with the chamber
//! (e.g. one running on a vendor's machine, reached over the network).
//!
//! The box keeps its own copy of the workspace so the agent's file tools (Read, Edit,
//! Grep, ...) work locally and fast. The copy is kept in step at command boundaries,
//! which is when an agent's view has to be right:
//!
//!   start            pull everything (the simplest possible prefetch)
//!   each command     push the box's edits -> run in the chamber -> pull the chamber's changes
//!   agent exits      push once more (its last edits)
//!
//! Change detection is local and cheap: a scan of (mtime, size) per entry against the
//! state after the previous sync. Pulls send only entries changed since the previous
//! pull plus the full list of paths (for deletions). All of it travels as two Chamber
//! API methods, sync_pull / sync_push, so nothing beyond the protocol connects the box
//! and the chamber.

use crate::Worker;
use base64::Engine;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::UNIX_EPOCH;

type Scan = HashMap<String, (u128, u64)>;   // relative path (dirs end in '/') -> (mtime ns, size)

pub struct Mirror {
    root: PathBuf,
    state: Mutex<(Scan, f64)>,               // (scan after last sync, `since` for the next pull)
}

impl Mirror {
    pub fn new(root: &str) -> Self {
        Mirror { root: PathBuf::from(root), state: Mutex::new((Scan::new(), 0.0)) }
    }

    /// Run `exec` between a push and a pull, holding the lock so concurrent tool calls
    /// see a consistent workspace.
    pub fn around(&self, worker: &Worker, exec: impl FnOnce() -> Value) -> Value {
        let guard = SyncGuard::new(self);
        if let Err(e) = guard.push(worker) {
            eprintln!("phantom sync: push failed: {e}");
        }
        let resp = exec();
        if let Err(e) = guard.pull(worker) {
            eprintln!("phantom sync: pull failed: {e}");
        }
        resp
    }

    pub fn pull(&self, worker: &Worker) -> Result<(), String> {
        SyncGuard::new(self).pull(worker)
    }

    pub fn push(&self, worker: &Worker) -> Result<(), String> {
        SyncGuard::new(self).push(worker)
    }
}

/// Serializes syncs (one at a time across shell threads) for its lifetime.
struct SyncGuard<'a> {
    m: &'a Mirror,
    _lock: std::sync::MutexGuard<'static, ()>,
}

static SYNC_LOCK: Mutex<()> = Mutex::new(());

impl<'a> SyncGuard<'a> {
    fn new(m: &'a Mirror) -> Self {
        SyncGuard { m, _lock: SYNC_LOCK.lock().unwrap_or_else(|e| e.into_inner()) }
    }

    fn pull(&self, worker: &Worker) -> Result<(), String> {
        let since = self.m.state.lock().unwrap().1;
        let resp = worker.request("sync_pull", json!({"since": since}));
        if let Some(e) = resp.get("error") {
            return Err(e.to_string());
        }
        let data = base64::engine::general_purpose::STANDARD
            .decode(resp.get("tar_b64").and_then(Value::as_str).unwrap_or(""))
            .map_err(|e| e.to_string())?;
        fs::create_dir_all(&self.m.root).map_err(|e| e.to_string())?;
        let mut archive = tar::Archive::new(Cursor::new(data));
        archive.set_preserve_mtime(true);
        archive.set_overwrite(true);
        archive.unpack(&self.m.root).map_err(|e| e.to_string())?;   // tar refuses paths escaping root

        // Delete what the chamber no longer has.
        let remote: HashSet<String> = resp.get("paths").and_then(Value::as_array).into_iter().flatten()
            .filter_map(Value::as_str).map(|p| p.trim_end_matches('/').to_string()).collect();
        let mut gone: Vec<String> = scan(&self.m.root).into_keys()
            .filter(|k| !remote.contains(k.trim_end_matches('/'))).collect();
        gone.sort_by_key(|k| std::cmp::Reverse(k.len()));               // children before parents
        for rel in gone {
            let p = self.m.root.join(rel.trim_end_matches('/'));
            let _ = if rel.ends_with('/') { fs::remove_dir_all(&p) } else { fs::remove_file(&p) };
        }
        let now = resp.get("now").and_then(Value::as_f64).unwrap_or(since);
        *self.m.state.lock().unwrap() = (scan(&self.m.root), now);
        Ok(())
    }

    fn push(&self, worker: &Worker) -> Result<(), String> {
        let current = scan(&self.m.root);
        let previous = self.m.state.lock().unwrap().0.clone();
        let changed: Vec<&String> = current.iter()
            .filter(|(k, v)| previous.get(*k) != Some(v) && !(k.ends_with('/') && previous.contains_key(*k)))
            .map(|(k, _)| k).collect();
        let deletes: Vec<String> = previous.keys().filter(|k| !current.contains_key(*k))
            .map(|k| k.trim_end_matches('/').to_string()).collect();
        if changed.is_empty() && deletes.is_empty() {
            return Ok(());
        }
        let mut builder = tar::Builder::new(Vec::new());
        builder.follow_symlinks(false);
        for rel in &changed {
            let path = self.m.root.join(rel.trim_end_matches('/'));
            let name = rel.trim_end_matches('/');
            let r = if rel.ends_with('/') { builder.append_dir(name, &path) } else { builder.append_path_with_name(&path, name) };
            if let Err(e) = r {
                eprintln!("phantom sync: skip {name}: {e}");
            }
        }
        let data = builder.into_inner().map_err(|e| e.to_string())?;
        let resp = worker.request("sync_push", json!({
            "tar_b64": base64::engine::general_purpose::STANDARD.encode(&data), "deletes": deletes}));
        if let Some(e) = resp.get("error") {
            return Err(e.to_string());
        }
        self.m.state.lock().unwrap().0 = current;
        Ok(())
    }
}

/// Every entry under root (not following symlinks): files and links by (mtime, size),
/// directories with a trailing '/' (their mtime changes with their contents, so it's ignored).
fn scan(root: &Path) -> Scan {
    let mut out = Scan::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = fs::read_dir(&dir) else { continue };
        for e in entries.flatten() {
            let path = e.path();
            let Ok(meta) = fs::symlink_metadata(&path) else { continue };
            let Ok(rel) = path.strip_prefix(root) else { continue };
            let rel = rel.to_string_lossy().to_string();
            if meta.is_dir() {
                out.insert(format!("{rel}/"), (0, 0));
                stack.push(path);
            } else {
                let mtime = meta.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                    .map(|d| d.as_nanos()).unwrap_or(0);
                out.insert(rel, (mtime, meta.len()));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_sees_files_dirs_and_links() {
        let d = std::env::temp_dir().join(format!("phantom-scan-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(d.join("sub")).unwrap();
        fs::write(d.join("sub/a.txt"), "x").unwrap();
        std::os::unix::fs::symlink("sub/a.txt", d.join("link")).unwrap();
        let s = scan(&d);
        assert!(s.contains_key("sub/") && s.contains_key("sub/a.txt") && s.contains_key("link"));
        assert_eq!(s["sub/a.txt"].1, 1);
        fs::remove_dir_all(&d).unwrap();
    }
}
