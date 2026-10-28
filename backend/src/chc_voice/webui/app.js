/* chc-voice control panel. Vanilla JS on purpose: no build step before P0 is verified. */
(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  const app = {
    state: "idle",
    mode: "zh_direct",
    running: false,
    settings: null,
    keys: {},
    modes: [],
    devices: [],
    virtualMic: null,
    defaultInput: null,
    defaultOutput: null,
    ws: null,
    voices: { zh: [], ja: [] },
    voicesLoaded: false,
    transcript: { final: "", partial: "", translation: "" },
    bannerTimer: null,
  };

  // ------------------------------------------------------------------ transport
  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let body = {};
    try { body = await response.json(); } catch (_) { /* empty */ }
    if (!response.ok) {
      const error = new Error(body.message || `HTTP ${response.status}`);
      error.payload = body;
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function connectWS() {
    if (app.ws && (app.ws.readyState === WebSocket.OPEN || app.ws.readyState === WebSocket.CONNECTING)) return;
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${location.host}/ws`);
    app.ws = ws;
    ws.onmessage = (message) => {
      try { handleEvent(JSON.parse(message.data)); } catch (error) { console.warn(error); }
    };
    ws.onclose = () => {
      app.ws = null;
      setTimeout(connectWS, 1200);
    };
    ws.onerror = () => ws.close();
  }

  function handleEvent(event) {
    switch (event.type) {
      case "snapshot":
        applySnapshot(event);
        break;
      case "state":
        app.state = event.state;
        app.running = !["idle", "error", "stopping"].includes(event.state);
        renderStatus(event.label, event.detail || "");
        break;
      case "level":
        setMeters(event.input, event.output);
        break;
      case "transcript":
        handleTranscript(event);
        break;
      case "translation":
        app.transcript.translation = event.text || "";
        renderTranscript();
        break;
      case "commit":
        app.transcript.final = event.text || "";
        app.transcript.partial = "";
        renderTranscript();
        break;
      case "error":
        showBanner(event.message || "出错", true);
        break;
      case "warning":
        showBanner(event.message || "", false);
        break;
      case "mode":
        app.mode = event.mode;
        renderModes();
        break;
      case "keys":
        app.keys = event.keys || {};
        renderKeys();
        break;
      case "segment":
        if (event.total_ms) {
          $("#stateDetail").textContent = `延迟 ${Math.round(event.total_ms)} ms`;
        }
        break;
      case "asr":
        if (event.status === "asr_connected" && event.region) {
          $("#stateDetail").textContent = `ASR 已连接（${event.region === "beijing" ? "北京" : "新加坡"}）`;
        }
        break;
      default:
        break;
    }
  }

  // ------------------------------------------------------------------ render
  function applySnapshot(snapshot) {
    app.settings = snapshot.settings || app.settings;
    app.keys = snapshot.keys || app.keys;
    app.modes = snapshot.modes || app.modes;
    app.devices = snapshot.devices || app.devices;
    app.virtualMic = snapshot.virtual_mic || app.virtualMic;
    app.defaultInput = snapshot.default_input || app.defaultInput;
    app.defaultOutput = snapshot.default_output || app.defaultOutput;
    const status = snapshot.status || {};
    app.state = status.state || app.state;
    app.mode = status.mode || app.mode;
    app.running = !!status.running;
    renderAll();
    renderStatus(status.state_label || labelFor(status.state), status.last_error ? "上次错误：" + status.last_error.message : "");
  }

  function renderAll() {
    if (app.settings) {
      applySettingsToControls(app.settings);
    }
    renderModes();
    renderKeys();
    renderDevices();
    renderVirtualMic();
    renderRunning();
    renderTranscript();
  }

  function labelFor(state) {
    return {
      idle: "就绪", starting: "启动中", listening: "聆听中", recognizing: "识别中",
      translating: "翻译中", synthesizing: "合成中", speaking: "播放中",
      stopping: "停止中", error: "错误",
    }[state] || "就绪";
  }

  function renderStatus(label, detail) {
    $("#stateLabel").textContent = label || labelFor(app.state);
    $("#stateDetail").textContent = detail || "";
    const dot = $("#stateDot");
    dot.className = `dot ${app.state}`;
    renderRunning();
  }

  function renderRunning() {
    const busy = ["starting", "stopping"].includes(app.state);
    $("#startBtn").disabled = busy || app.running;
    $("#stopBtn").disabled = !app.running && app.state !== "error";
    $("#panicBtn").classList.toggle("hidden", !app.running && app.state !== "error");
    $("#startBtn").textContent = app.state === "starting" ? "启动中…" : "开始";
  }

  function renderModes() {
    $$(".mode").forEach((button) => {
      const mode = button.dataset.mode;
      button.classList.toggle("active", mode === app.mode);
      const info = app.modes.find((item) => item.mode === mode);
      button.disabled = info ? !info.available : false;
      button.title = info && !info.available ? info.missing.join("；") : "";
    });
  }

  function renderKeys() {
    for (const service of ["asr", "tts", "llm"]) {
      const info = app.keys[service] || {};
      const element = $(`#status-${service}`);
      if (!element) continue;
      if (info.configured) {
        element.textContent = `已配置 ····${info.last4 || "…"}${info.source === "env" ? "（环境变量）" : ""}`;
        element.className = "key-status ok";
      } else {
        element.textContent = "未配置";
        element.className = "key-status";
      }
    }
  }

  function renderDevices() {
    const select = $("#inputDevice");
    const current = app.settings ? app.settings.audio.input_device : "";
    const isVirtual = (name) => /chc-voice|blackhole/i.test(name || "");
    const inputs = app.devices.filter((device) => device.inputs > 0 && !isVirtual(device.name));
    select.innerHTML = `<option value="">系统默认</option>` +
      inputs.map((device) => `<option value="${escapeHtml(device.name)}">${escapeHtml(device.name)}</option>`).join("");
    select.value = current;
    if (select.value !== current) {
      const option = document.createElement("option");
      option.value = current;
      option.textContent = current || "系统默认";
      select.appendChild(option);
      select.value = current;
    }
    $("#micName").textContent = current || app.defaultInput || "系统默认";

    const virtual = $("#virtualDevice");
    virtual.value = app.settings ? app.settings.audio.virtual_device : "chc-voice";
  }

  function renderVirtualMic() {
    const status = app.virtualMic || {};
    const ready = !!status.ready;
    const name = status.consumer_name || status.aggregate_name || "chc-voice";
    $("#vmName").textContent = name;
    const check = $("#vmCheck");
    check.textContent = ready ? "✓" : "✗";
    check.className = `check ${ready ? "ok" : "bad"}`;
    const guidance = status.guidance || status.error || "";
    const box = $("#vmGuidance");
    box.textContent = guidance;
    box.classList.toggle("hidden", !guidance || ready);
    $("#createVm").classList.toggle("hidden", !(status.blackhole && !status.aggregate));
  }

  function applySettingsToControls(settings) {
    $("#volume").value = Math.round(settings.audio.output_gain * 100);
    $("#volumeVal").textContent = `${Math.round(settings.audio.output_gain * 100)}%`;
    $("#outputGain").value = Math.round(settings.audio.output_gain * 100);
    $("#outputGainVal").textContent = `${Math.round(settings.audio.output_gain * 100)}%`;
    $("#monitorGain").value = Math.round(settings.audio.monitor_gain * 100);
    $("#monitorGainVal").textContent = `${Math.round(settings.audio.monitor_gain * 100)}%`;
    $("#micGain").value = Math.round(settings.audio.mic_gain * 100);
    $("#micGainVal").textContent = `${Math.round(settings.audio.mic_gain * 100)}%`;
    $("#monitorEnabled").checked = !!settings.audio.monitor_enabled;
    $("#monitorGain").disabled = !settings.audio.monitor_enabled;
    $("#fishModel").value = settings.voices.fish_model;
    $("#translationTone").value = settings.translate ? settings.translate.tone : "casual";
    $("#zhVoice").value = settings.voices.zh_voice_id;
    $("#jaVoice").value = settings.voices.ja_voice_id;
    $("#stableA").value = settings.advanced.zh_direct.stable_ms;
    $("#minA").value = settings.advanced.zh_direct.min_chars;
    $("#stableB").value = settings.advanced.zh_to_ja.stable_ms;
    $("#minB").value = settings.advanced.zh_to_ja.min_chars;
    $("#showTranscript").checked = !!settings.ui.show_transcript;
    $("#showTranslation").checked = !!settings.ui.show_translation;
    $("#virtualDevice").value = settings.audio.virtual_device;
    if (settings.voices.zh_voice_id && !$("#zhVoice").value) addVoiceOption("zhVoice", settings.voices.zh_voice_id, settings.voices.zh_voice_name);
    if (settings.voices.ja_voice_id && !$("#jaVoice").value) addVoiceOption("jaVoice", settings.voices.ja_voice_id, settings.voices.ja_voice_name);
  }

  function addVoiceOption(selectId, id, name) {
    if (!id) return;
    const select = $(`#${selectId}`);
    if (Array.from(select.options).some((option) => option.value === id)) return;
    const option = document.createElement("option");
    option.value = id;
    option.textContent = name ? `${name}（${id.slice(0, 6)}…）` : id;
    select.appendChild(option);
  }

  function renderTranscript() {
    const container = $("#transcript");
    if (!app.settings || !app.settings.ui.show_transcript) {
      container.innerHTML = "";
      return;
    }
    const lines = [];
    if (app.transcript.final) lines.push(`<div class="line source">${escapeHtml(app.transcript.final)}</div>`);
    if (app.transcript.partial) lines.push(`<div class="line partial">${escapeHtml(app.transcript.partial)}</div>`);
    if (app.transcript.translation && app.settings.ui.show_translation) {
      lines.push(`<div class="line translated">${escapeHtml(app.transcript.translation)}</div>`);
    }
    container.innerHTML = lines.join("");
  }

  function handleTranscript(event) {
    if (event.final) {
      app.transcript.final = event.text || "";
      app.transcript.partial = "";
    } else {
      app.transcript.partial = event.text || "";
    }
    renderTranscript();
  }

  function setMeters(input, output) {
    const toPercent = (db) => {
      if (db == null || db <= -60) return 0;
      return Math.max(0, Math.min(100, (db + 60) / 60 * 100));
    };
    $("#inMeter").style.width = `${toPercent(input)}%`;
    $("#outMeter").style.width = `${toPercent(output)}%`;
  }

  function showBanner(message, isError) {
    const banner = $("#banner");
    banner.textContent = message;
    banner.style.background = isError ? "var(--danger)" : "var(--accent)";
    banner.classList.remove("hidden");
    clearTimeout(app.bannerTimer);
    app.bannerTimer = setTimeout(() => banner.classList.add("hidden"), isError ? 9000 : 4000);
  }

  // ------------------------------------------------------------------ actions
  async function refreshAll() {
    try {
      const snapshot = await api("/api/state");
      applySnapshot(snapshot);
    } catch (error) {
      showBanner(`无法连接后端：${error.message}`, true);
    }
  }

  async function saveAudio(payload, message = "") {
    try {
      const result = await api("/api/settings/audio", { method: "PUT", body: JSON.stringify(payload) });
      app.settings = result.settings;
      if (result.restart_required && message) showBanner(message, false);
      refreshAll();
    } catch (error) {
      showBanner(error.message, true);
    }
  }

  async function saveTranslate(tone) {
    try {
      const result = await api("/api/settings/translate", { method: "PUT", body: JSON.stringify({ tone }) });
      app.settings = result.settings;
      showBanner("翻译语气已保存", false);
    } catch (error) {
      showBanner(error.message, true);
    }
  }

  async function saveVoices(payload) {
    try {
      const result = await api("/api/settings/voices", { method: "PUT", body: JSON.stringify(payload) });
      app.settings = result.settings;
      renderModes();
    } catch (error) {
      showBanner(error.message, true);
    }
  }

  async function loadVoices(force = false) {
    if (app.voicesLoaded && !force) return;
    app.voicesLoaded = true;
    for (const language of ["zh", "ja"]) {
      try {
        const result = await api(`/api/voices?language=${language}`);
        app.voices[language] = result.voices || [];
        fillVoiceSelect(language, app.voices[language], result.error);
      } catch (error) {
        fillVoiceSelect(language, [], error.message);
      }
    }
  }

  function fillVoiceSelect(language, voices, error) {
    const select = language === "zh" ? $("#zhVoice") : $("#jaVoice");
    const current = app.settings ? (language === "zh" ? app.settings.voices.zh_voice_id : app.settings.voices.ja_voice_id) : "";
    const label = language === "zh" ? "中文音色" : "日语音色";
    const options = [`<option value="">${error ? "无法加载（可手动填 ID）" : `选择${label}`}</option>`];
    for (const voice of voices) {
      options.push(`<option value="${escapeHtml(voice.id)}">${escapeHtml(voice.title)}${voice.languages ? " · " + escapeHtml(voice.languages.join("/")) : ""}</option>`);
    }
    select.innerHTML = options.join("");
    if (current) {
      if (!Array.from(select.options).some((option) => option.value === current)) {
        const option = document.createElement("option");
        option.value = current;
        option.textContent = `${language === "zh" ? app.settings.voices.zh_voice_name : app.settings.voices.ja_voice_name || current}（当前）`;
        select.appendChild(option);
      }
      select.value = current;
    }
  }

  async function saveKey(service) {
    const input = $(`#key-${service}`);
    const key = input.value.trim();
    if (!key) return;
    try {
      await api("/api/settings/keys", { method: "PUT", body: JSON.stringify({ service, key }) });
      input.value = "";
      showBanner("已保存到 Keychain", false);
      await refreshAll();
    } catch (error) {
      showBanner(error.message, true);
    }
  }

  async function deleteKey(service) {
    if (!confirm("删除这个 key？相关模式会不可用。")) return;
    try {
      await api(`/api/settings/keys/${service}`, { method: "DELETE" });
      showBanner("已删除", false);
      await refreshAll();
    } catch (error) {
      showBanner(error.message, true);
    }
  }

  async function testKey(service) {
    const button = document.querySelector(`[data-test-key="${service}"]`);
    if (button) { button.disabled = true; button.textContent = "测试中"; }
    try {
      const result = await api(`/api/settings/test/${service}`, { method: "POST", body: "{}" });
      showBanner(result.detail || "测试通过", false);
    } catch (error) {
      const detail = error.payload && error.payload.service ? `${error.payload.service}：` : "";
      showBanner(`${detail}${error.message}`, true);
    } finally {
      if (button) { button.disabled = false; button.textContent = "测试"; }
    }
  }

  async function switchMode(mode) {
    if (mode === app.mode) return;
    try {
      const result = await api("/api/settings/mode", { method: "PUT", body: JSON.stringify({ mode }) });
      app.mode = result.mode;
      renderModes();
    } catch (error) {
      const missing = error.payload && error.payload.missing ? error.payload.missing.join("；") : error.message;
      showBanner(missing, true);
    }
  }

  async function start() {
    $("#startBtn").disabled = true;
    try {
      await api("/api/start", { method: "POST", body: JSON.stringify({ mode: app.mode }) });
    } catch (error) {
      const missing = error.payload && error.payload.missing ? error.payload.missing.join("；") : error.message;
      showBanner(missing, true);
    } finally {
      refreshAll();
    }
  }

  async function stop() {
    try {
      await api("/api/stop", { method: "POST", body: "{}" });
    } catch (error) {
      showBanner(error.message, true);
    } finally {
      refreshAll();
    }
  }

  async function panic() {
    try {
      await api("/api/panic", { method: "POST", body: "{}" });
      showBanner("已紧急静音", false);
    } catch (error) {
      showBanner(error.message, true);
    } finally {
      refreshAll();
    }
  }

  async function testTone() {
    const button = $("#testBtn");
    button.disabled = true;
    button.textContent = "正在写入测试音…";
    try {
      const result = await api("/api/test-tone", { method: "POST", body: JSON.stringify({ seconds: 1.2 }) });
      showBanner(result.message, false);
    } catch (error) {
      showBanner(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "测试虚拟麦克风";
    }
  }

  async function createVirtualMic() {
    const button = $("#createVm");
    button.disabled = true;
    try {
      const result = await api("/api/virtual-mic/create", { method: "POST", body: "{}" });
      app.virtualMic = result.virtual_mic;
      renderVirtualMic();
      showBanner(result.ok ? "已创建 chc-voice" : result.virtual_mic.guidance, !result.ok);
    } catch (error) {
      showBanner(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function saveAdvanced() {
    const payload = {
      zh_direct: {
        stable_ms: Number($("#stableA").value),
        min_chars: Number($("#minA").value),
      },
      zh_to_ja: {
        stable_ms: Number($("#stableB").value),
        min_chars: Number($("#minB").value),
      },
      ui: {
        show_transcript: $("#showTranscript").checked,
        show_translation: $("#showTranslation").checked,
      },
    };
    try {
      const result = await api("/api/settings/advanced", { method: "PUT", body: JSON.stringify(payload) });
      app.settings = result.settings;
      showBanner(result.restart_required ? "已保存；重新开始后生效" : "已保存", false);
      renderTranscript();
      refreshAll();
    } catch (error) {
      showBanner(error.message, true);
    }
  }

  // ------------------------------------------------------------------ drawer
  function openDrawer() {
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
    $("#scrim").classList.remove("hidden");
    loadVoices();
  }

  function closeDrawer() {
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
    $("#scrim").classList.add("hidden");
  }

  // ------------------------------------------------------------------ bind
  function bind() {
    $("#settingsBtn").addEventListener("click", openDrawer);
    $("#closeDrawer").addEventListener("click", closeDrawer);
    $("#scrim").addEventListener("click", closeDrawer);

    $$(".mode").forEach((button) => button.addEventListener("click", () => switchMode(button.dataset.mode)));
    $("#startBtn").addEventListener("click", start);
    $("#stopBtn").addEventListener("click", stop);
    $("#testBtn").addEventListener("click", testTone);
    $("#panicBtn").addEventListener("click", panic);
    $("#createVm").addEventListener("click", createVirtualMic);
    $("#refreshVoices").addEventListener("click", () => loadVoices(true));

    const volume = $("#volume");
    volume.addEventListener("input", () => {
      $("#volumeVal").textContent = `${volume.value}%`;
      $("#outputGain").value = volume.value;
      $("#outputGainVal").textContent = `${volume.value}%`;
    });
    volume.addEventListener("change", () => saveAudio({ output_gain: Number(volume.value) / 100 }));

    $("#outputGain").addEventListener("input", (event) => {
      const value = Number(event.target.value);
      $("#outputGainVal").textContent = `${value}%`;
      $("#volume").value = value;
      $("#volumeVal").textContent = `${value}%`;
    });
    $("#outputGain").addEventListener("change", (event) => saveAudio({ output_gain: Number(event.target.value) / 100 }));

    $("#monitorGain").addEventListener("input", (event) => {
      $("#monitorGainVal").textContent = `${event.target.value}%`;
    });
    $("#monitorGain").addEventListener("change", (event) => saveAudio({ monitor_gain: Number(event.target.value) / 100 }));
    $("#micGain").addEventListener("input", (event) => {
      $("#micGainVal").textContent = `${event.target.value}%`;
    });
    $("#micGain").addEventListener("change", (event) => saveAudio({ mic_gain: Number(event.target.value) / 100 }));

    $("#monitorEnabled").addEventListener("change", (event) => {
      if (event.target.checked) {
        showBanner("建议戴耳机。外放会让真实麦克风再次拾音，形成反馈。", false);
      }
      $("#monitorGain").disabled = !event.target.checked;
      saveAudio({ monitor_enabled: event.target.checked });
    });

    $("#inputDevice").addEventListener("change", (event) => {
      saveAudio({ input_device: event.target.value }, "输入设备改动会在下一次开始时生效。");
    });
    $("#virtualDevice").addEventListener("change", (event) => {
      saveAudio({ virtual_device: event.target.value }, "虚拟麦克风改动会在下一次开始时生效。");
    });

    $("#zhVoice").addEventListener("change", (event) => {
      const option = event.target.selectedOptions[0];
      const label = option ? option.textContent.split(" · ")[0].replace("（当前）", "").trim() : "";
      saveVoices({ zh_voice_id: event.target.value, zh_voice_name: label });
    });
    $("#jaVoice").addEventListener("change", (event) => {
      const option = event.target.selectedOptions[0];
      const label = option ? option.textContent.split(" · ")[0].replace("（当前）", "").trim() : "";
      saveVoices({ ja_voice_id: event.target.value, ja_voice_name: label });
    });
    $("#fishModel").addEventListener("change", (event) => saveVoices({ fish_model: event.target.value }));
    $("#translationTone").addEventListener("change", (event) => saveTranslate(event.target.value));

    $$("[data-save-key]").forEach((button) => button.addEventListener("click", () => saveKey(button.dataset.saveKey)));
    $$("[data-del-key]").forEach((button) => button.addEventListener("click", () => deleteKey(button.dataset.delKey)));
    $$("[data-test-key]").forEach((button) => button.addEventListener("click", () => testKey(button.dataset.testKey)));

    $("#advancedHead").addEventListener("click", () => {
      $("#advancedHead").classList.toggle("open");
      $("#advancedBody").classList.toggle("collapsed");
    });
    $("#saveAdvanced").addEventListener("click", saveAdvanced);

    $$(".mode").forEach((button) => button.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") button.click();
    }));
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  bind();
  refreshAll();
  connectWS();
})();
