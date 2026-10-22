# Coding demo fixture

This is the deliberately broken **before** state used in the featured recording.
Copy it to a disposable Git repository and commit the initial state before
opening a t task. Do not run it against a personal project.

`python3 -m unittest -v` should fail two of three tests. The CLI currently ignores
`--status open` and `--status done`.

The recorded prompt was:

> Fix the task CLI: --status open and --status done currently return every task.
> Run the existing unittest suite first to reproduce it, implement filtering
> without modifying the input list, add an empty-list regression test, and rerun
> tests. Show the real CLI output for --status open and a concise git diff. Do not
> commit.

`expected.patch` is the actual agent-produced change from this recording.
Applying it with `git apply expected.patch` yields five passing tests.

The agent was asked after terminal resume to verify the same fix and tests again.
Keep all real PTY captures outside the repository until paths, host details, and
thread identifiers have been sanitized. See `../README.md` for the workflow and current capture limitations.
