//! Debug-example-only native WebView proof using production helper and PTY IPC.
use super::*;

#[tauri::command]
fn native_proof_report(app: tauri::AppHandle, result: Value) {
    println!("NATIVE_PROOF:{}", result);
    let state = app.state::<Workspace>();
    for (_, mut terminal) in state.terminals.lock().unwrap().drain() {
        let _ = terminal.killer.kill();
    }
    state.bridge.lock().unwrap().take();
    app.exit(if result["ok"] == true { 0 } else { 1 });
}

pub fn run() {
    let sandbox = std::env::var_os("T_DESKTOP_NATIVE_PROOF_SANDBOX")
        .map(PathBuf::from)
        .expect("Run through the isolated macos_ui pytest, not directly");
    assert_eq!(std::env::var_os("HOME"), Some(sandbox.clone().into_os_string()));
    assert!(sandbox.join(".native-proof-sandbox").is_file());
    let mut context = tauri::generate_context!();
    context.config_mut().build.dev_url = None;
    tauri::Builder::default()
        .manage(Workspace::default())
        .setup(|app| {
            println!("NATIVE_STAGE:window-created");
            if let Some(window) = app.get_webview_window("main") {
                println!("NATIVE_STAGE:url={:?}", window.url());
                // Bounded, idempotent fallback when the initial navigation's
                // load callback is not observed by the test harness.
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(2));
                    if let Err(error) = window.eval(include_str!("proof.js")) {
                        println!("NATIVE_PROOF:{}", json!({"ok":false,"error":error.to_string()}));
                    }
                });
            }
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_secs(35));
                println!("NATIVE_PROOF:{}", json!({"ok":false,"error":"native WebView proof timed out"}));
                handle.exit(1);
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            super::helper_request,
            super::helper_reconnect,
            super::pty_open,
            super::pty_write,
            super::pty_ack,
            super::pty_resize,
            super::pty_close,
            native_proof_report
        ])
        .on_page_load(|webview, payload| {
            println!("NATIVE_STAGE:page-{:?}", payload.event());
            if payload.event() == tauri::webview::PageLoadEvent::Finished {
                if let Err(error) = webview.eval(include_str!("proof.js")) {
                    println!("NATIVE_PROOF:{}", json!({"ok":false,"error":error.to_string()}));
                }
            }
        })
        .run(context)
        .expect("Unable to start isolated native proof");
}
