# chc-voice Electron shell

The desktop window loads the local UI served by the Python backend. It is intentionally a
thin shell: audio, ASR/TTS, Keychain and settings all live in the backend.

```bash
npm install
npm start
```

`npm start` clears `ELECTRON_RUN_AS_NODE` because some host environments set it globally,
which would make Electron run as plain Node.

The shell picks a free loopback port, spawns `.venv/bin/chc-voice serve --port <port>`,
waits for `/api/health`, and stops the backend when the window closes.

Electron's cache and Preferences are kept in `~/Library/Application Support/chc-voice-electron`,
separate from the backend configuration in `~/Library/Application Support/chc-voice`.
