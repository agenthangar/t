#[allow(dead_code)]
#[path = "../../src-tauri/src/main.rs"]
mod production;

fn main() {
    production::run_native_proof();
}
