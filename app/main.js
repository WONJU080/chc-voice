// chc-voice Electron shell.
//
// The Python backend owns the UI, audio and keys; this process only starts it on a free
// loopback port and shows the page in a compact window.  Keeping the UI in the backend
// means the same panel works in a browser during P0 and inside Electron afterwards.

const { app, BrowserWindow, dialog, shell, nativeTheme } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
// Keep Electron's cache/Preferences out of the backend's config directory.
app.setName("chc-voice");
app.setPath("userData", path.join(app.getPath("appData"), "chc-voice-electron"));

let backend = null;
let backendPort = null;
let mainWindow = null;

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function backendCommand(port) {
  const venv = path.join(ROOT, ".venv", "bin");
  const cli = path.join(venv, "chc-voice");
  if (fs.existsSync(cli)) {
    return { command: cli, args: ["serve", "--port", String(port)], env: process.env };
  }
  const python = path.join(venv, "python");
  if (fs.existsSync(python)) {
    return {
      command: python,
      args: ["-m", "chc_voice", "serve", "--port", String(port)],
      env: { ...process.env, PYTHONPATH: path.join(ROOT, "backend", "src") },
    };
  }
  return null;
}

async function health(port) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/health`, { signal: AbortSignal.timeout(800) });
    return response.ok;
  } catch (_) {
    return false;
  }
}

async function waitForBackend(port, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await health(port)) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function startBackend() {
  const port = await freePort();
  backendPort = port;
  const spec = backendCommand(port);
  if (!spec) {
    throw new Error("找不到 .venv/bin/chc-voice 或 .venv/bin/python；请先运行 scripts/setup.sh。");
  }
  backend = spawn(spec.command, spec.args, {
    cwd: ROOT,
    env: { ...spec.env, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend.stdout.on("data", (data) => process.stdout.write(`[backend] ${data}`));
  backend.stderr.on("data", (data) => process.stderr.write(`[backend] ${data}`));
  backend.on("exit", (code, signal) => {
    if (app.isQuitting) return;
    if (mainWindow && !mainWindow.isDestroyed() && code !== 0) {
      dialog.showErrorBox("chc-voice 后端退出", `后端退出（code=${code}, signal=${signal}）。请查看日志后重试。`);
    }
    backend = null;
  });
  if (!(await waitForBackend(port))) {
    throw new Error(`后端没有在 20 秒内就绪（端口 ${port}）。`);
  }
  return port;
}

function createWindow(port) {
  mainWindow = new BrowserWindow({
    width: 460,
    height: 640,
    minWidth: 420,
    minHeight: 560,
    title: "chc-voice",
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#151618" : "#f5f4f0",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.loadURL(`http://127.0.0.1:${port}`);
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function stopBackend() {
  if (backend && !backend.killed) {
    backend.kill("SIGTERM");
    backend = null;
  }
}

app.whenReady().then(async () => {
  try {
    const port = await startBackend();
    createWindow(port);
  } catch (error) {
    dialog.showErrorBox("chc-voice 启动失败", String(error && error.message ? error.message : error));
    app.quit();
  }
  app.on("activate", () => {
    if (!mainWindow && backendPort) {
      createWindow(backendPort);
    }
  });
});

app.on("before-quit", () => {
  app.isQuitting = true;
  stopBackend();
});
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
