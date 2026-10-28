const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("chcVoice", {
  platform: process.platform,
  electron: process.versions.electron,
});
