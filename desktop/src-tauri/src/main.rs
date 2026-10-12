#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use base64::{engine::general_purpose::STANDARD, Engine};
use portable_pty::{native_pty_system, ChildKiller, CommandBuilder, MasterPty, PtySize};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    time::Duration,
};
use tauri::{ipc::Channel, Emitter, Manager, State};
const METHODS: &[&str] = &[
    "initialize",
    "workspace.snapshot",
    "sessions.attach",
    "sessions.open",
    "sessions.resume",
    "sessions.close",
    "sessions.restart",
    "issues.list",
    "issues.status",
    "issues.start",
    "operations.prepare",
    "operations.apply",
    "operations.started",
    "operations.finished",
    "operations.reconcile",
    "config.get",
    "config.set",
    "repositories.register",
    "hosts.change",
    "sessions.list",
    "sessions.search",
    "sessions.detail",
    "repositories.list",
    "hosts.list",
    "agents.status",
    "system.diagnostics",
    "mcp.call",
    "mcp.capabilities",
    "attachments.validate",
];
type Reply = Result<Value, String>;
struct Bridge {
    input: Mutex<ChildStdin>,
    child: Mutex<Child>,
    pending: Arc<Mutex<HashMap<u64, mpsc::Sender<Reply>>>>,
    next: Mutex<u64>,
}
fn runtime_path(root: Option<&std::path::Path>, python: Option<&std::path::Path>) -> String {
    let mut entries = Vec::new();
    if let Some(root) = root {
        entries.push(root.join("bin"));
    }
    if let Some(python) = python.and_then(|p| p.parent()) {
        entries.push(python.to_owned());
    }
    if let Some(home) = std::env::var_os("HOME") {
        entries.push(PathBuf::from(&home).join(".local/bin"));
        entries.push(PathBuf::from(home).join("bin"));
    }
    entries.extend(
        [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        .map(PathBuf::from),
    );
    std::env::join_paths(entries)
        .unwrap()
        .to_string_lossy()
        .into_owned()
}
impl Bridge {
    fn spawn(
        root: PathBuf,
        python: PathBuf,
        app: Option<tauri::AppHandle>,
    ) -> Result<Self, String> {
        let path = runtime_path(Some(&root), Some(&python));
        let mut cmd = Command::new(python);
        cmd.env("PATH", path);
        cmd.arg(root.join("libexec/t_bridge.py"))
            .env("T_DESKTOP_ROOT", &root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        // Coverage instrumentation must never cross into a bundled helper.
        for (key, _) in std::env::vars().filter(|(k, _)| k.starts_with("COV_CORE_")) {
            cmd.env_remove(key);
        }
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("Cannot start workspace helper: {e}"))?;
        let input = child.stdin.take().ok_or("Missing helper stdin")?;
        let output = child.stdout.take().ok_or("Missing helper stdout")?;
        let pending: Arc<Mutex<HashMap<u64, mpsc::Sender<Reply>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let routes = pending.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let Ok(line) = line else { break };
                if line.len() > 8 * 1024 * 1024 {
                    break;
                }
                let Ok(v) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if v["version"] != 1 {
                    continue;
                }
                if v.get("event").is_some() {
                    if let Some(app) = app.as_ref() {
                        let _ = app.emit(
                            "workspace-progress",
                            json!({"id":v["id"],"operationId":v["operationId"],"stage":v["stage"]}),
                        );
                    }
                    continue;
                }
                if let Some(id) = v["id"].as_u64() {
                    if let Some(tx) = routes.lock().unwrap().remove(&id) {
                        let result = if let Some(e) = v.get("error") {
                            Err(e["message"]
                                .as_str()
                                .unwrap_or("Helper request failed")
                                .to_owned())
                        } else {
                            Ok(v["result"].clone())
                        };
                        let _ = tx.send(result);
                    }
                }
            }
            for (_, tx) in routes.lock().unwrap().drain() {
                let _=tx.send(Err("Workspace helper disconnected. Reconnect and inspect operation state before retrying.".into()));
            }
        });
        Ok(Self {
            input: Mutex::new(input),
            child: Mutex::new(child),
            pending,
            next: Mutex::new(0),
        })
    }
    fn request(&self, method: &str, params: Value) -> Reply {
        if !METHODS.contains(&method) {
            return Err("Unsupported workspace method".into());
        }
        let id = {
            let mut n = self.next.lock().unwrap();
            *n += 1;
            *n
        };
        let (tx, rx) = mpsc::channel();
        self.pending.lock().unwrap().insert(id, tx);
        let request = json!({"version":1,"id":id,"method":method,"params":params});
        let written = {
            let mut input = self.input.lock().unwrap();
            writeln!(input, "{request}").and_then(|_| input.flush())
        };
        if let Err(e) = written {
            self.pending.lock().unwrap().remove(&id);
            return Err(format!("Helper disconnected: {e}"));
        }
        let response = rx.recv_timeout(Duration::from_secs(120)).map_err(|_| {
            "Helper timed out; this operation was not retried. Reconcile before retrying."
                .to_owned()
        });
        self.pending.lock().unwrap().remove(&id);
        response?
    }
}
impl Drop for Bridge {
    fn drop(&mut self) {
        let child = self.child.get_mut().unwrap();
        let _ = child.kill();
        let _ = child.wait();
    }
}
struct Attachment {
    descriptor: Value,
    operation: Option<String>,
}
struct Terminal {
    master: Box<dyn MasterPty + Send>,
    writer: Arc<Mutex<Box<dyn Write + Send>>>,
    killer: Box<dyn ChildKiller + Send + Sync>,
    ack: mpsc::SyncSender<()>,
}
#[derive(Clone, Serialize)]
struct TerminalEvent {
    kind: String,
    data: Option<String>,
    exit_code: Option<u32>,
}
#[derive(Clone, Default)]
struct Workspace {
    bridge: Arc<Mutex<Option<Arc<Bridge>>>>,
    attachments: Arc<Mutex<HashMap<String, Attachment>>>,
    terminals: Arc<Mutex<HashMap<String, Terminal>>>,
    sequence: Arc<Mutex<u64>>,
}
fn roots(app: &tauri::AppHandle) -> Result<(PathBuf, PathBuf), String> {
    if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let python = std::env::var_os("T_DESKTOP_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("python3"));
        return Ok((root, python));
    }
    let root = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("runtime/core");
    let python = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("runtime/python/bin/python3");
    Ok((root, python))
}
#[tauri::command]
async fn helper_request(
    app: tauri::AppHandle,
    state: State<'_, Workspace>,
    method: String,
    params: Value,
) -> Reply {
    if !METHODS.contains(&method.as_str()) {
        return Err("Unsupported workspace method".into());
    }
    let holder = state.bridge.clone();
    let (root, python) = roots(&app)?;
    let mut result = tauri::async_runtime::spawn_blocking(move || {
        let bridge = {
            let mut holder = holder.lock().unwrap();
            if holder.is_none() {
                let bridge = Arc::new(Bridge::spawn(root, python, Some(app.clone()))?);
                let hello = bridge.request("initialize", json!({"protocolVersion":1}))?;
                if hello["protocolVersion"] != 1 {
                    return Err("Incompatible workspace helper protocol".into());
                }
                *holder = Some(bridge)
            }
            holder.as_ref().unwrap().clone()
        };
        bridge.request(&method, params)
    })
    .await
    .map_err(|e| e.to_string())??;
    let executable = can_execute_attachment(&result);
    let operation = if result["state"] == "ready" {
        result["operationId"].as_str().map(str::to_owned)
    } else {
        None
    };
    tokenize(&mut result, &state, executable, operation);

    Ok(result)
}
fn can_execute_attachment(result: &Value) -> bool {
    if result["method"] == "terminal" {
        result["state"] == "ready"
    } else {
        result.get("state").is_none()
            || matches!(result["state"].as_str(), Some("completed" | "partial"))
    }
}
fn tokenize(value: &mut Value, state: &Workspace, executable: bool, operation: Option<String>) {
    if value["kind"] == "terminal" && value.get("argv").is_some() {
        if executable {
            let token = {
                let mut seq = state.sequence.lock().unwrap();
                *seq += 1;
                format!("attachment-{seq}")
            };
            state.attachments.lock().unwrap().insert(
                token.clone(),
                Attachment {
                    descriptor: value.clone(),
                    operation,
                },
            );
            *value = json!({"token":token,"kind":"terminal"});
        } else {
            *value = json!({"kind":"terminal","pending":true});
        }
        return;
    }
    match value {
        Value::Object(map) => {
            for v in map.values_mut() {
                tokenize(v, state, executable, operation.clone())
            }
        }
        Value::Array(list) => {
            for v in list {
                tokenize(v, state, executable, operation.clone())
            }
        }
        _ => {}
    }
}
#[tauri::command]
async fn helper_reconnect(state: State<'_, Workspace>) -> Result<(), String> {
    let bridge = state.bridge.lock().unwrap().take();
    state.attachments.lock().unwrap().clear();
    tauri::async_runtime::spawn_blocking(move || {
        if let Some(bridge) = bridge {
            let mut child = bridge.child.lock().unwrap();
            let _ = child.kill();
            let _ = child.wait();
        }
    })
    .await
    .map_err(|e| e.to_string())?;
    Ok(())
}
fn command_from_descriptor(v: &Value) -> Result<CommandBuilder, String> {
    if v["kind"] != "terminal" {
        return Err("Invalid terminal attachment".into());
    }
    let argv = v["argv"].as_array().ok_or("Missing terminal argv")?;
    let executable = argv
        .first()
        .and_then(Value::as_str)
        .ok_or("Missing executable")?;
    let mut cmd = CommandBuilder::new(executable);
    for arg in &argv[1..] {
        cmd.arg(arg.as_str().ok_or("Invalid argv")?);
    }
    if let Some(cwd) = v["cwd"].as_str() {
        cmd.cwd(cwd);
    }
    if let Some(env) = v["env"].as_object() {
        for (key, value) in env {
            cmd.env(key, value.as_str().ok_or("Invalid environment")?);
        }
    }
    cmd.env("TERM", "xterm-256color");
    cmd.env("PATH", runtime_path(None, None));
    for (key, _) in std::env::vars().filter(|(k, _)| k.starts_with("COV_CORE_")) {
        cmd.env_remove(key);
    }
    Ok(cmd)
}
#[tauri::command]
async fn pty_open(
    state: State<'_, Workspace>,
    token: String,
    cols: u16,
    rows: u16,
    on_event: Channel<TerminalEvent>,
) -> Result<String, String> {
    let workspace = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        open_terminal(workspace, token, cols, rows, on_event)
    })
    .await
    .map_err(|e| e.to_string())?
}
fn open_terminal(
    state: Workspace,
    token: String,
    cols: u16,
    rows: u16,
    on_event: Channel<TerminalEvent>,
) -> Result<String, String> {
    let attachment = state
        .attachments
        .lock()
        .unwrap()
        .remove(&token)
        .ok_or("Attachment expired; request a fresh attachment")?;
    if let Some(validation) = attachment.descriptor.get("validation") {
        let bridge = state
            .bridge
            .lock()
            .unwrap()
            .as_ref()
            .cloned()
            .ok_or("Helper disconnected; request a fresh attachment")?;
        let checked = bridge.request("attachments.validate", json!({"validation":validation}))?;
        if checked["valid"] != true {
            return Err("Stale attachment; select the current session and reconnect".into());
        }
    }
    let cmd = command_from_descriptor(&attachment.descriptor)?;
    let pair = native_pty_system()
        .openpty(PtySize {
            cols: cols.clamp(2, 500),
            rows: rows.clamp(2, 300),
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| e.to_string())?;
    let mut reader = pair.master.try_clone_reader().map_err(|e| e.to_string())?;
    let writer = pair.master.take_writer().map_err(|e| e.to_string())?;
    let operation = attachment.operation.clone();
    let bridge = state.bridge.lock().unwrap().as_ref().cloned();
    if let Some(operation) = operation.as_ref() {
        let bridge = bridge.as_ref().ok_or("Helper disconnected")?;
        if let Err(e) = bridge.request("operations.started", json!({"operationId":operation})) {
            return Err(e);
        }
    }
    let mut child = pair.slave.spawn_command(cmd).map_err(|e| {
        if let (Some(id), Some(bridge)) = (&operation, &bridge) {
            let _ = bridge.request(
                "operations.finished",
                json!({"operationId":id,"exitCode":-1}),
            );
        }
        e.to_string()
    })?;
    drop(pair.slave);
    let killer = child.clone_killer();
    let (ack_tx, ack_rx) = mpsc::sync_channel(1);
    let id = token;
    state.terminals.lock().unwrap().insert(
        id.clone(),
        Terminal {
            master: pair.master,
            writer: Arc::new(Mutex::new(writer)),
            killer,
            ack: ack_tx,
        },
    );
    let entries = state.terminals.clone();
    let terminal_id = id.clone();
    std::thread::spawn(move || {
        let mut buf = [0u8; 16384];
        loop {
            match reader.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    if on_event
                        .send(TerminalEvent {
                            kind: "data".into(),
                            data: Some(STANDARD.encode(&buf[..n])),
                            exit_code: None,
                        })
                        .is_err()
                    {
                        break;
                    }
                    if ack_rx.recv_timeout(Duration::from_secs(30)).is_err() {
                        break;
                    }
                }
            }
        }
        if let Some(mut terminal) = entries.lock().unwrap().remove(&terminal_id) {
            let _ = terminal.killer.kill();
        }
        let status = child.wait().ok().map(|s| s.exit_code());
        if let (Some(id), Some(bridge)) = (&operation, &bridge) {
            let _ = bridge.request(
                "operations.finished",
                json!({"operationId":id,"exitCode":status.unwrap_or(1)}),
            );
        }
        let _ = on_event.send(TerminalEvent {
            kind: "exit".into(),
            data: None,
            exit_code: status,
        });
    });
    Ok(id)
}
fn write_terminal(state: &Workspace, id: &str, data: &str) -> Result<(), String> {
    if data.len() > 64 * 1024 {
        return Err("Terminal input exceeds limit".into());
    }
    let writer = state
        .terminals
        .lock()
        .unwrap()
        .get(id)
        .ok_or("Terminal detached")?
        .writer
        .clone();
    let result = writer
        .lock()
        .unwrap()
        .write_all(data.as_bytes())
        .map_err(|e| e.to_string());
    result
}
#[tauri::command]
async fn pty_write(state: State<'_, Workspace>, id: String, data: String) -> Result<(), String> {
    let workspace = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || write_terminal(&workspace, &id, &data))
        .await
        .map_err(|e| e.to_string())?
}
#[tauri::command]
fn pty_ack(state: State<'_, Workspace>, id: String) -> Result<(), String> {
    if let Some(t) = state.terminals.lock().unwrap().get(&id) {
        let _ = t.ack.try_send(());
    }
    Ok(())
}
#[tauri::command]
fn pty_resize(state: State<'_, Workspace>, id: String, cols: u16, rows: u16) -> Result<(), String> {
    state
        .terminals
        .lock()
        .unwrap()
        .get(&id)
        .ok_or("Terminal detached")?
        .master
        .resize(PtySize {
            cols: cols.clamp(2, 500),
            rows: rows.clamp(2, 300),
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|e| e.to_string())
}
#[tauri::command]
fn pty_close(state: State<'_, Workspace>, id: String) -> Result<(), String> {
    if let Some(mut terminal) = state.terminals.lock().unwrap().remove(&id) {
        terminal.killer.kill().map_err(|e| e.to_string())?;
    }
    Ok(())
}
fn main() {
    tauri::Builder::default()
        .manage(Workspace::default())
        .invoke_handler(tauri::generate_handler![
            helper_request,
            helper_reconnect,
            pty_open,
            pty_write,
            pty_ack,
            pty_resize,
            pty_close
        ])
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed) {
                let state = window.state::<Workspace>();
                for (_, mut terminal) in state.terminals.lock().unwrap().drain() {
                    let _ = terminal.killer.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("Unable to start t Workspace");
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invalid_descriptors_fail() {
        assert!(command_from_descriptor(&json!({"kind":"shell","argv":["sh"]})).is_err());
        assert!(command_from_descriptor(&json!({"kind":"terminal","argv":["sh",3]})).is_err());
    }
    #[test]
    fn method_allowlist_is_private() {
        assert!(!METHODS.contains(&"shell.exec"));
        assert!(METHODS.contains(&"sessions.attach"));
    }
    #[test]
    fn completed_terminal_records_cannot_mint_replay_tokens() {
        let workspace = Workspace::default();
        let mut record = json!({"method":"terminal","state":"completed","attachment":{"kind":"terminal","argv":["zsh","-lic","t repo create x"]}});
        let executable = can_execute_attachment(&record);
        assert!(!executable);
        tokenize(&mut record, &workspace, executable, None);
        assert!(record["attachment"].get("argv").is_none());
        assert!(record["attachment"].get("token").is_none());
        assert!(workspace.attachments.lock().unwrap().is_empty());
    }
    #[test]
    fn nested_multi_issue_descriptors_are_opaque() {
        let workspace = Workspace::default();
        let mut record = json!({"method":"issues.start","state":"completed","result":{"attachments":[{"kind":"terminal","argv":["tmux","attach-session"]},{"kind":"terminal","argv":["ssh","fixture"]}]}});
        tokenize(&mut record, &workspace, true, None);
        assert!(record.to_string().find("argv").is_none());
        assert_eq!(workspace.attachments.lock().unwrap().len(), 2);
    }
    #[test]
    fn finder_path_contains_standard_external_tool_locations() {
        let path = runtime_path(None, None);
        assert!(path.contains("/opt/homebrew/bin"));
        assert!(path.contains("/usr/bin"));
    }
    fn fixture_terminal(
        state: &Workspace,
        id: &str,
        argv: Value,
        channel: Channel<TerminalEvent>,
    ) -> String {
        state.attachments.lock().unwrap().insert(
            id.into(),
            Attachment {
                descriptor: json!({"kind":"terminal","argv":argv}),
                operation: None,
            },
        );
        open_terminal(state.clone(), id.into(), 80, 24, channel).unwrap()
    }
    #[test]
    fn blocking_paste_does_not_lock_other_terminals() {
        let state = Workspace::default();
        let first = fixture_terminal(
            &state,
            "paste",
            json!(["/bin/sleep", "30"]),
            Channel::new(|_| Ok(())),
        );
        let (tx, rx) = mpsc::channel();
        let workspace = state.clone();
        let id = first.clone();
        std::thread::spawn(move || {
            let _ = write_terminal(&workspace, &id, &"x".repeat(65536));
            let _ = tx.send(());
        });
        std::thread::sleep(Duration::from_millis(30));
        let (events, event_rx) = mpsc::channel();
        let second = fixture_terminal(
            &state,
            "other",
            json!(["/bin/sh", "-c", "printf '\\342\\230\\203'"]),
            Channel::new(move |body| {
                events.send(body).unwrap();
                Ok(())
            }),
        );
        let body = event_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        let tauri::ipc::InvokeResponseBody::Json(text) = body else {
            panic!("Expected JSON channel frame")
        };
        let value: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(
            STANDARD.decode(value["data"].as_str().unwrap()).unwrap(),
            "☃".as_bytes()
        );
        let _ = state
            .terminals
            .lock()
            .unwrap()
            .get(&second)
            .unwrap()
            .ack
            .try_send(());
        if let Some(mut terminal) = state.terminals.lock().unwrap().remove(&first) {
            terminal.killer.kill().unwrap();
        }
        rx.recv_timeout(Duration::from_secs(3)).unwrap();
    }
    #[test]
    fn lost_renderer_channel_detaches_client() {
        let state = Workspace::default();
        let (tx, rx) = mpsc::channel();
        let id = fixture_terminal(
            &state,
            "lost",
            json!(["/bin/sh", "-c", "printf ready; exec sleep 30"]),
            Channel::new(move |_| {
                let _ = tx.send(());
                Err(std::io::Error::other("Fixture renderer closed").into())
            }),
        );
        rx.recv_timeout(Duration::from_secs(3)).unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while state.terminals.lock().unwrap().contains_key(&id)
            && std::time::Instant::now() < deadline
        {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(!state.terminals.lock().unwrap().contains_key(&id));
    }

    #[test]
    fn bridge_routes_concurrent_replies_and_does_not_consume_progress() {
        let root = std::env::temp_dir().join(format!("t-bridge-test-{}", std::process::id()));
        std::fs::create_dir_all(root.join("libexec")).unwrap();
        std::fs::write(root.join("libexec/t_bridge.py"),"import json,sys,threading,time\nlock=threading.Lock()\ndef reply(v):\n time.sleep(.02 if v['method']=='config.get' else .04)\n with lock:\n  print(json.dumps({'version':1,'id':v['id'],'event':'progress','stage':'start'}),flush=True)\n  print(json.dumps({'version':1,'id':v['id'],'result':{'method':v['method']}}),flush=True)\nfor line in sys.stdin:\n threading.Thread(target=reply,args=(json.loads(line),)).start()\n").unwrap();
        let python = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources/python/bin/python3");
        let bridge = Arc::new(Bridge::spawn(root.clone(), python, None).unwrap());
        let other = bridge.clone();
        let request =
            std::thread::spawn(move || other.request("workspace.snapshot", json!({})).unwrap());
        assert_eq!(
            bridge.request("config.get", json!({})).unwrap()["method"],
            "config.get"
        );
        assert_eq!(request.join().unwrap()["method"], "workspace.snapshot");
        drop(bridge);
        std::fs::remove_dir_all(root).unwrap();
    }
}
