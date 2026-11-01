import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("suraksha", {
  send: (channel: string, data?: unknown) => ipcRenderer.send(channel, data ?? {}),
  on: (channel: string, cb: (data: any) => void) => ipcRenderer.on(channel, (_e, data) => cb(data)),
});
