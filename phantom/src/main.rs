//! phantom: the phantom box binary (DESIGN.md §5.1, "phantom" placement).
//!
//! One static binary, two roles, picked by how it is invoked:
//!
//! * `phantom run --output <file> [--connect HOST:PORT --token T --sync] -- <agent cmd...>`:
//!   the **runner**, the box's main process. It speaks vals-agent/1 (newline-delimited
//!   JSON-RPC) with the worker's Chamber API, over stdin/stdout or, with `--connect`,
//!   over TCP (so the box can run anywhere that reaches the worker). It starts the agent
//!   CLI with the instruction on its stdin and serves a unix socket inside the box for
//!   phantom shells. With `--sync` the box keeps its own copy of the workspace, kept in
//!   step with the chamber at command boundaries (see sync.rs). When the agent exits it
//!   sends the agent's output as an artifact and reports `finish`.
//!
//! * as `sh` / `bash` (installed over /bin/sh and /bin/bash): the **phantom shell**.
//!   It forwards the command to the runner, which sends it to the worker's Chamber
//!   API as an `exec`, so the command runs in the chamber. It prints the output and
//!   exits with the command's exit code. It never forwards its environment, so the
//!   box's secrets never reach the chamber.

mod sync;

use base64::Engine;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

const SOCKET: &str = "/run/phantom/phantom.sock";
const REAL_BASH: &str = "/usr/lib/phantom/bash.real";
const PROTOCOL: &str = "vals-agent/1";
const CWD_MARKER: &str = "__VALS_CWD__";

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let argv0 = Path::new(&args[0]).file_name().and_then(|s| s.to_str()).unwrap_or("");
    match argv0 {
        "sh" | "bash" | "dash" | "ash" => std::process::exit(shell(argv0, &args[1..])),
        _ if args.get(1).map(String::as_str) == Some("run") => std::process::exit(run(&args[2..])),
        _ => {
            eprintln!("usage: phantom run --output <file> -- <agent command...>  (or invoke as sh/bash)");
            std::process::exit(2)
        }
    }
}

// ============================================================== phantom shell

/// Returns the exit code to use.
fn shell(argv0: &str, args: &[String]) -> i32 {
    let Some(cmd) = command_from_args(argv0, args) else {
        eprintln!("phantom shell: interactive shells are not supported");
        return 2;
    };
    // Claude Code's shell snapshot introspects the shell's own aliases/functions and
    // uses bash-only syntax. It describes this box's shell, not the task, so it runs here.
    if cmd.contains("SNAPSHOT_FILE=") && Path::new(REAL_BASH).exists() {
        let err = Command::new(REAL_BASH).args(args).exec();
        eprintln!("phantom shell: cannot exec {REAL_BASH}: {err}");
        return 126;
    }
    let cwd = std::env::current_dir().map(|p| p.display().to_string()).unwrap_or_else(|_| "/".into());
    let (forward, cwd_file) = forwardable(&cmd);
    let req = json!({"cmd": forward, "cwd": cwd});
    match relay(&req) {
        Ok(resp) => {
            if let Some(err) = resp.get("error") {
                eprintln!("phantom shell: {}", err.get("message").and_then(Value::as_str).unwrap_or("error"));
                return 126;
            }
            let raw = resp.get("output").and_then(Value::as_str).unwrap_or("");
            let (out, new_cwd) = split_cwd(raw);
            if let (Some(file), Some(dir)) = (cwd_file, new_cwd) {
                // The agent CLI reads its cwd file from *this* box's /tmp.
                let _ = fs::write(&file, dir);
            }
            let _ = std::io::stdout().write_all(out.as_bytes());
            let _ = std::io::stdout().flush();
            if resp.get("timed_out").and_then(Value::as_bool).unwrap_or(false) {
                return 124;
            }
            let rc = resp.get("returncode").and_then(Value::as_i64).unwrap_or(1);
            if rc < 0 { 1 } else { (rc & 0xff) as i32 }
        }
        Err(e) => {
            eprintln!("phantom shell: chamber unreachable: {e}");
            127
        }
    }
}

/// `sh -c CMD`, `bash -c -l CMD`, `bash -lc CMD`; anything else (a script file) is
/// forwarded as a quoted command line.
fn command_from_args(argv0: &str, args: &[String]) -> Option<String> {
    let mut saw_c = false;
    for a in args {
        if !saw_c {
            if a.starts_with('-') && !a.starts_with("--") && a[1..].contains('c') {
                saw_c = true;
            }
            continue;
        }
        if a.starts_with('-') && a.len() <= 3 {
            continue; // e.g. the `-l` in `bash -c -l CMD`
        }
        return Some(a.clone());
    }
    if saw_c || args.is_empty() {
        return None;
    }
    let quoted: Vec<String> = args.iter().map(|a| shell_quote(a)).collect();
    Some(format!("{} {}", argv0, quoted.join(" ")))
}

/// Claude Code wraps each command as
///   source <snapshot> ... && { \builtin unalias ...; } ... && eval '<CMD>' < /dev/null && pwd -P >| <cwd file>
/// The prefix is bash-only (and refers to a snapshot of *this* box's shell), so only
/// the POSIX tail is sent: it runs under any sh. The cwd file lives in the box, not the
/// chamber, so instead of writing it there the command reports its final cwd after a
/// marker in its output; the phantom shell strips that and writes the file locally.
/// Returns (command to forward, cwd file to write).
fn forwardable(cmd: &str) -> (String, Option<String>) {
    let mut body = cmd;
    if cmd.starts_with("source ") {
        if let Some(i) = cmd.find("eval '") {
            body = &cmd[i..];
        }
    }
    if let Some(j) = body.rfind(" && pwd -P >| ") {
        let file = body[j + " && pwd -P >| ".len()..].trim().to_string();
        let core = &body[..j];
        let fwd = format!("{core}; __vals_rc=$?; [ $__vals_rc -eq 0 ] && printf '\\n{CWD_MARKER}%s\\n' \"$(pwd -P)\"; exit $__vals_rc");
        return (fwd, Some(file));
    }
    (body.to_string(), None)
}

/// Split the cwd report off a command's output: (output for the agent, final cwd).
fn split_cwd(out: &str) -> (&str, Option<&str>) {
    let marker = format!("\n{CWD_MARKER}");
    match out.rfind(&marker) {
        Some(k) => (&out[..k], Some(out[k + marker.len()..].trim_end_matches('\n'))),
        None => (out, None),
    }
}

fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

fn relay(req: &Value) -> std::io::Result<Value> {
    let mut stream = UnixStream::connect(std::env::var("PHANTOM_SOCKET").unwrap_or_else(|_| SOCKET.into()))?;
    stream.write_all(format!("{req}\n").as_bytes())?;
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line)?;
    serde_json::from_str(&line).map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))
}

// ============================================================== runner

type Pending = Arc<Mutex<HashMap<u64, Sender<Value>>>>;

struct Worker {
    out: Mutex<Box<dyn Write + Send>>,
    pending: Pending,
    next_id: AtomicU64,
}

impl Worker {
    fn send(&self, msg: &Value) {
        let mut out = self.out.lock().unwrap();
        let _ = out.write_all(format!("{msg}\n").as_bytes());
        let _ = out.flush();
    }

    /// Send a request and block until its response arrives (any thread may call this).
    fn request(&self, method: &str, params: Value) -> Value {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = channel();
        self.pending.lock().unwrap().insert(id, tx);
        self.send(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}));
        rx.recv().unwrap_or_else(|_| json!({"error": {"code": -32000, "message": "worker gone"}}))
    }
}

/// Connect to the worker's Chamber API: stdio (local box) or TCP with a token (anywhere).
fn connect(addr: Option<&str>, token: &str) -> std::io::Result<(Box<dyn BufRead + Send>, Box<dyn Write + Send>)> {
    let Some(addr) = addr else {
        return Ok((Box::new(BufReader::new(std::io::stdin())), Box::new(std::io::stdout())));
    };
    let mut last = None;
    for _ in 0..60 {   // the worker may still be setting up; retry for ~30 s
        match std::net::TcpStream::connect(addr) {
            Ok(mut s) => {
                s.write_all(format!("{}\n", json!({"token": token})).as_bytes())?;
                let mut reader = BufReader::new(s.try_clone()?);
                let mut line = String::new();
                reader.read_line(&mut line)?;
                if !line.contains("\"ok\"") {
                    return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied, "Chamber API refused the token"));
                }
                return Ok((Box::new(reader), Box::new(s)));
            }
            Err(e) => { last = Some(e); thread::sleep(Duration::from_millis(500)); }
        }
    }
    Err(last.unwrap_or_else(|| std::io::Error::other("connect failed")))
}

fn run(args: &[String]) -> i32 {
    let mut output = String::from("/tmp/vals/agent/output.jsonl");
    let (mut addr, mut token, mut sync_on) = (None::<String>, String::new(), false);
    let mut i = 0;
    while i < args.len() && args[i] != "--" {
        match args[i].as_str() {
            "--output" if i + 1 < args.len() => { output = args[i + 1].clone(); i += 1; }
            "--connect" if i + 1 < args.len() => { addr = Some(args[i + 1].clone()); i += 1; }
            "--token" if i + 1 < args.len() => { token = args[i + 1].clone(); i += 1; }
            "--sync" => sync_on = true,
            _ => {}
        }
        i += 1;
    }
    let token = std::env::var("PHANTOM_TOKEN").unwrap_or(token);   // prefer env: not visible in `ps`
    let agent_cmd: Vec<String> = args.get(i + 1..).map(|s| s.to_vec()).unwrap_or_default();
    if agent_cmd.is_empty() {
        eprintln!("phantom run: missing agent command after --");
        return 2;
    }

    let (reader, writer) = match connect(addr.as_deref(), &token) {
        Ok(rw) => rw,
        Err(e) => { eprintln!("phantom run: cannot reach the Chamber API: {e}"); return 1; }
    };
    let worker = Arc::new(Worker { out: Mutex::new(writer), pending: Arc::default(),
                                   next_id: AtomicU64::new(1) });
    let child: Arc<Mutex<Option<Child>>> = Arc::default();
    let (start_tx, start_rx) = channel::<Value>();
    {
        // Reader: responses go to whoever is waiting on that id; `start` to main; `cancel` kills the agent.
        let (pending, child) = (worker.pending.clone(), child.clone());
        thread::spawn(move || {
            for line in reader.lines() {
                let Ok(line) = line else { break };
                let Ok(msg) = serde_json::from_str::<Value>(&line) else { continue };
                if let Some(id) = msg.get("id").and_then(Value::as_u64) {
                    if let Some(tx) = pending.lock().unwrap().remove(&id) {
                        let _ = tx.send(msg.get("result").cloned()
                            .unwrap_or_else(|| json!({"error": msg.get("error").cloned().unwrap_or(Value::Null)})));
                    }
                    continue;
                }
                match msg.get("method").and_then(Value::as_str) {
                    Some("start") => { let _ = start_tx.send(msg.get("params").cloned().unwrap_or(Value::Null)); }
                    Some("cancel") => { if let Some(c) = child.lock().unwrap().as_mut() { let _ = c.kill(); } }
                    _ => {}
                }
            }
            // The worker closed the connection: nothing left to report to. Stop the agent.
            if let Some(c) = child.lock().unwrap().as_mut() { let _ = c.kill(); }
            std::process::exit(0);
        });
    }

    let hello = worker.request("hello", json!({"protocol": PROTOCOL,
        "agent": {"name": "phantom", "version": env!("CARGO_PKG_VERSION")}, "capabilities": ["exec"]}));
    if hello.get("error").is_some() {
        eprintln!("phantom run: handshake refused: {hello}");
        return 1;
    }
    let Ok(start) = start_rx.recv() else { return 1 };
    let instruction = start.get("instruction").and_then(Value::as_str).unwrap_or("").to_string();
    let config = start.get("config").cloned().unwrap_or(Value::Null);
    let model = config.get("model").and_then(Value::as_str).unwrap_or("").to_string();
    let max_turns = config.get("step_limit").and_then(Value::as_u64).unwrap_or(50).to_string();
    let cmd_timeout = config.get("command_timeout_s").and_then(Value::as_u64).unwrap_or(600);
    let workspace = start.get("workspace").and_then(Value::as_str).unwrap_or("/workspace").to_string();

    let mirror = sync_on.then(|| Arc::new(sync::Mirror::new(&workspace)));
    if let Some(m) = &mirror {
        // Initial mirror: the chamber's workspace, before the agent sees anything.
        if let Err(e) = m.pull(&worker) {
            worker.request("finish", json!({"exit_status": "agent_error", "message": format!("initial sync: {e}")}));
            return 1;
        }
    }
    if let Err(e) = serve_shells(worker.clone(), cmd_timeout, mirror.clone()) {
        worker.request("finish", json!({"exit_status": "agent_error", "message": format!("socket: {e}")}));
        return 1;
    }

    // Start the agent CLI: placeholders filled from the start config, instruction on stdin.
    let argv: Vec<String> = agent_cmd.iter()
        .map(|a| a.replace("{model}", &model).replace("{max_turns}", &max_turns)).collect();
    if let Some(dir) = Path::new(&output).parent() { let _ = fs::create_dir_all(dir); }
    let spawned = (|| -> std::io::Result<Child> {
        let out = fs::File::create(&output)?;
        let err = fs::File::create(format!("{output}.stderr"))?;
        let mut c = Command::new(&argv[0]).args(&argv[1..]).env("PHANTOM_SOCKET", SOCKET)
            .env("SHELL", "/bin/bash").current_dir(&workspace)
            .stdin(Stdio::piped()).stdout(out).stderr(err).spawn()?;
        if let Some(mut stdin) = c.stdin.take() { let _ = stdin.write_all(instruction.as_bytes()); }
        Ok(c)
    })();
    match spawned {
        Ok(c) => *child.lock().unwrap() = Some(c),
        Err(e) => {
            worker.request("finish", json!({"exit_status": "agent_error", "message": format!("spawn {}: {e}", argv[0])}));
            return 1;
        }
    }
    loop {
        if let Some(c) = child.lock().unwrap().as_mut() {
            if let Ok(Some(_)) = c.try_wait() { break; }
        }
        thread::sleep(Duration::from_millis(100));
    }

    if let Some(m) = &mirror {
        let _ = m.push(&worker);   // the agent's last file edits
    }

    // Report: the raw output as an artifact, then the parsed outcome.
    let raw = fs::read(&output).unwrap_or_default();
    worker.request("artifact", json!({"name": "output.jsonl",
        "data_b64": base64::engine::general_purpose::STANDARD.encode(&raw)}));
    if let Ok(err) = fs::read(format!("{output}.stderr")) {
        worker.request("artifact", json!({"name": "stderr.log",
            "data_b64": base64::engine::general_purpose::STANDARD.encode(&err)}));
    }
    worker.request("finish", outcome_from_stream_json(&String::from_utf8_lossy(&raw)));
    0
}

/// Accept phantom-shell connections; each becomes one Chamber API `exec`.
fn serve_shells(worker: Arc<Worker>, cmd_timeout: u64, mirror: Option<Arc<sync::Mirror>>) -> std::io::Result<()> {
    if let Some(dir) = Path::new(SOCKET).parent() { fs::create_dir_all(dir)?; }
    let _ = fs::remove_file(SOCKET);
    let listener = UnixListener::bind(SOCKET)?;
    thread::spawn(move || {
        for conn in listener.incoming().flatten() {
            let (worker, mirror) = (worker.clone(), mirror.clone());
            thread::spawn(move || {
                let mut line = String::new();
                let mut reader = BufReader::new(match conn.try_clone() { Ok(c) => c, Err(_) => return });
                if reader.read_line(&mut line).is_err() { return; }
                let Ok(req) = serde_json::from_str::<Value>(&line) else { return };
                let exec = || worker.request("exec", json!({"cmd": req["cmd"], "cwd": req["cwd"], "timeout_s": cmd_timeout}));
                let resp = match &mirror {
                    // push the box's edits -> run in the chamber -> pull the chamber's changes
                    Some(m) => m.around(&worker, exec),
                    None => exec(),
                };
                let mut conn = conn;
                let _ = conn.write_all(format!("{resp}\n").as_bytes());
            });
        }
    });
    Ok(())
}

/// Claude Code `--output-format stream-json`: the last `result` event carries the outcome.
fn outcome_from_stream_json(output: &str) -> Value {
    let result = output.lines().rev()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .find(|v| v.get("type").and_then(Value::as_str) == Some("result"));
    let Some(r) = result else {
        return json!({"exit_status": "agent_error", "message": "no result event in agent output"});
    };
    let n = |v: &Value, k: &str| v.get(k).and_then(Value::as_u64).unwrap_or(0);
    let usage = r.get("usage").cloned().unwrap_or(Value::Null);
    let subtype = r.get("subtype").and_then(Value::as_str).unwrap_or("");
    let is_error = r.get("is_error").and_then(Value::as_bool).unwrap_or(false);
    let status = match (subtype, is_error) {
        ("success", false) => "submitted",
        ("error_max_turns", _) => "step_limit",
        _ => "agent_error",
    };
    let full = r.get("result").and_then(Value::as_str).unwrap_or(subtype).to_string();
    let message: String = full.chars().take(2000).collect();
    let answer: String = full.chars().take(100_000).collect();   // for graders that need the final answer
    json!({
        "exit_status": status,
        "steps": n(&r, "num_turns"),
        "input_tokens": n(&usage, "input_tokens") + n(&usage, "cache_creation_input_tokens") + n(&usage, "cache_read_input_tokens"),
        "output_tokens": n(&usage, "output_tokens"),
        "cost_usd": r.get("total_cost_usd").and_then(Value::as_f64).unwrap_or(0.0),
        "message": message,
        "answer": answer,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> { v.iter().map(|x| x.to_string()).collect() }

    #[test]
    fn parses_c_forms() {
        assert_eq!(command_from_args("sh", &s(&["-c", "ls"])).as_deref(), Some("ls"));
        assert_eq!(command_from_args("bash", &s(&["-c", "-l", "ls -la"])).as_deref(), Some("ls -la"));
        assert_eq!(command_from_args("bash", &s(&["-lc", "pwd"])).as_deref(), Some("pwd"));
        assert_eq!(command_from_args("sh", &s(&["run.sh", "a b"])).as_deref(), Some("sh 'run.sh' 'a b'"));
        assert_eq!(command_from_args("sh", &s(&[])), None);
    }

    #[test]
    fn strips_claude_wrapper_and_reports_cwd_through_output() {
        let c = "source /tmp/cc/snap.sh 2>/dev/null || true && shopt -u extglob 2>/dev/null || true && { \\builtin unalias -- 'x'; } >/dev/null 2>&1 || true && eval 'echo hi' < /dev/null && pwd -P >| /tmp/claude-1-cwd";
        let (fwd, file) = forwardable(c);
        assert!(fwd.starts_with("eval 'echo hi' < /dev/null; __vals_rc=$?;"), "{fwd}");
        assert!(fwd.contains(CWD_MARKER) && fwd.ends_with("exit $__vals_rc"));
        assert_eq!(file.as_deref(), Some("/tmp/claude-1-cwd"));
        assert_eq!(forwardable("ls; eval 'x'"), ("ls; eval 'x'".to_string(), None));
        assert_eq!(split_cwd("out\n__VALS_CWD__/app/sub\n"), ("out", Some("/app/sub")));
        assert_eq!(split_cwd("plain"), ("plain", None));
    }

    #[test]
    fn maps_result_event() {
        let out = r#"{"type":"assistant"}
{"type":"result","subtype":"success","num_turns":3,"total_cost_usd":0.5,"result":"ok","usage":{"input_tokens":1,"cache_read_input_tokens":2,"output_tokens":4}}"#;
        let o = outcome_from_stream_json(out);
        assert_eq!(o["exit_status"], "submitted");
        assert_eq!(o["input_tokens"], 3);
        assert_eq!(o["steps"], 3);
        assert_eq!(o["answer"], "ok");
        assert_eq!(outcome_from_stream_json("garbage")["exit_status"], "agent_error");
    }
}
