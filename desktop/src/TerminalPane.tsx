import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import * as native from "./transport";
import "@xterm/xterm/css/xterm.css";
export function TerminalPane({
  token,
  onExit,
}: {
  token: string;
  onExit: (code?: number) => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");
  const exit = useRef(onExit);
  exit.current = onExit;
  useEffect(() => {
    let id: string | undefined;
    let disposed = false;
    const pending: string[] = [];
    const term = new Terminal({
      fontFamily: '"SFMono-Regular", Menlo, monospace',
      fontSize: 13,
      lineHeight: 1.3,
      cursorBlink: true,
      scrollback: 10000,
      theme: {
        background: "#131619",
        foreground: "#e0e4e6",
        cursor: "#93bea6",
        selectionBackground: "#355849",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(element.current!);
    fit.fit();
    const report = (e: unknown) => {
      if (!disposed) setError(String(e));
    };
    native
      .terminalOpen(token, term.cols, term.rows, (event) => {
        if (disposed) return;
        if (event.kind === "data" && event.data) {
          const bytes = Uint8Array.from(atob(event.data), (c) =>
            c.charCodeAt(0),
          );
          term.write(bytes, () => {
            if (id) void native.terminalAck(id).catch(report);
            else pending.push("ack");
          });
        } else {
          exit.current(event.exit_code);
          term.write(
            `\r\n\x1b[90mAttachment ended (${event.exit_code ?? "disconnected"}). Session history is preserved.\x1b[0m\r\n`,
          );
        }
      })
      .then((value) => {
        id = value;
        if (disposed) {
          void native.terminalClose(id);
          return;
        }
        for (const _ of pending) void native.terminalAck(id);
        term.focus();
      })
      .catch(report);
    const input = term.onData((data) => {
      if (id) void native.terminalWrite(id, data).catch(report);
    });
    const resize = new ResizeObserver(() => {
      if (
        disposed ||
        !element.current?.clientWidth ||
        !element.current?.clientHeight
      )
        return;
      fit.fit();
      if (id)
        void native.terminalResize(id, term.cols, term.rows).catch(report);
    });
    resize.observe(element.current!);
    return () => {
      disposed = true;
      resize.disconnect();
      input.dispose();
      term.dispose();
      if (id) void native.terminalClose(id).catch(() => {});
    };
  }, [token]);
  return (
    <div className="terminal-surface">
      {error && (
        <div className="terminal-error" role="alert">
          {error}
        </div>
      )}
      <div
        ref={element}
        className="terminal-mount"
        aria-label="Interactive session terminal"
      />
    </div>
  );
}
