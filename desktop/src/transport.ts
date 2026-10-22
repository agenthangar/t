import { invoke, Channel } from "@tauri-apps/api/core";
export const request = <T>(method: string, params: unknown = {}): Promise<T> =>
  invoke("helper_request", { method, params });
export const reconnect = () => invoke("helper_reconnect");
export type TerminalEvent = {
  kind: "data" | "exit";
  data?: string;
  exit_code?: number;
};
export const terminalOpen = (
  token: string,
  cols: number,
  rows: number,
  onEvent: (event: TerminalEvent) => void,
) => {
  const channel = new Channel<TerminalEvent>();
  channel.onmessage = onEvent;
  return invoke<string>("pty_open", { token, cols, rows, onEvent: channel });
};
export const terminalWrite = (id: string, data: string) =>
  invoke("pty_write", { id, data });
export const terminalResize = (id: string, cols: number, rows: number) =>
  invoke("pty_resize", { id, cols, rows });
export const terminalAck = (id: string) => invoke("pty_ack", { id });
export const terminalClose = (id: string) => invoke("pty_close", { id });
