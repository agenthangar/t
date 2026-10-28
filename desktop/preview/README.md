# Session manager browser preview

Open `index.html` directly in a browser, or serve this directory with a static HTTP server. The preview is self-contained: no build, CDN, API, credentials, or network calls are required. It works independently of the desktop app and its production bridge.

```sh
python3 -m http.server 8766 --bind 127.0.0.1 --directory desktop/preview
```

Then open <http://127.0.0.1:8766> on the computer running the server. The command runs from the repository root and serves only `desktop/preview`; keep the server scoped to that static directory.

For access from another laptop or a phone signed into the same tailnet, inspect the existing Serve configuration and choose an unused port before adding the private HTTPS proxy:

```sh
tailscale serve status
tailscale serve --bg --https=8766 http://127.0.0.1:8766
```

Open the private HTTPS URL printed by Tailscale on the other device. If 8766 is already in use, choose another unused port and adjust both examples. Preserve existing Serve routes; do not use Funnel or reset the Serve configuration.

To remove only this preview's HTTPS listener:

```sh
tailscale serve --https=8766 off
```

Background Serve configuration survives restarts, but the local Python HTTP server must be restarted after reboot. The standalone `index.html` can always be opened offline without either server.

All identities, paths, issues, activity, and sessions are synthetic. New session, restart, close, and terminal attachment only change browser memory and reset on reload. No real terminal or native app is launched. A detached terminal does not close its session. The unreachable buildbox sample is unverified, excluded from active counts, and cannot be selected or changed.

The preview demonstrates a session manager that can inform both desktop and future Swift iOS views. It does not implement the shared backend or an iOS app. Native clients should consume the shared session contract rather than this demo's embedded sample data.

Manual checks: search and repository/host/status filtering; both views and sorting; selection separate from task inspection; new task whitespace validation; close/restart confirmation naming targets; unreachable actions disabled; one simulated terminal attachment and detach; light/dark theme; keyboard dialog dismissal; layouts at 320, 736, and 1024 pixels.
