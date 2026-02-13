// DC-Detector v0.2 — Frontend Application
// 4 tabs: Detection, Telemetry, LoRa, DB
(() => {
  'use strict';

  // ---------------------------------------------------------------------------
  // Configuration
  // ---------------------------------------------------------------------------
  const ports = { cap: 8001, det: 8002, mav: 8003, lora: 8004 };
  const host = location.hostname || 'localhost';
  const proto = location.protocol;
  const origin = (port) => `${proto}//${host}:${port}`;
  const wsProto = proto === 'https:' ? 'wss:' : 'ws:';

  // ---------------------------------------------------------------------------
  // DOM helpers
  // ---------------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const setText = (id, v) => { const el = $(id); if (el) el.textContent = v; };

  const els = {
    stream: $('stream'), overlay: $('overlay'),
    statusIndicator: $('status-indicator'), trackCount: $('track-count'), detFps: $('det-fps'),
    mavStatus: $('mav-status'), loraStatus: $('lora-status'),
    mavDot: $('mav-dot'), loraDot: $('lora-dot'),
    errorMessage: $('error-message'),
    // Detection tab
    modelSelect: $('model-select'), modelSwitchBtn: $('model-switch-btn'),
    confSlider: $('conf-slider'), confVal: $('conf-val'),
    saveConfSlider: $('save-conf-slider'), saveConfVal: $('save-conf-val'),
    imgszSelect: $('imgsz-select'),
    skipSlider: $('skip-slider'), skipVal: $('skip-val'),
    applyConfigBtn: $('apply-config-btn'),
    trackerList: $('tracker-list'), trackerRefresh: $('tracker-refresh'),
    // LoRa
    loraText: $('lora-text'), loraSendBtn: $('lora-send-btn'),
    loraLog: $('lora-log'), loraConnLabel: $('lora-conn-label'),
    loraStats: $('lora-stats'), loraClearBtn: $('lora-clear-btn'),
    // DB - Recording
    recDot: $('rec-dot'), recStatus: $('rec-status'),
    recStartBtn: $('rec-start-btn'), recStopBtn: $('rec-stop-btn'),
    recList: $('rec-list'),
    // DB - Shared filter
    dbSort: $('db-sort'), sessClassFilter: $('sess-class-filter'), dbRefreshBtn: $('db-refresh-btn'),
    // DB - Sessions
    sessList: $('sess-list'),
    // Modal
    modal: $('det-modal'), modalBackdrop: $('modal-backdrop'),
    modalClose: $('modal-close'), modalImage: $('modal-image'), modalDl: $('modal-dl'),
    // WS logs
    wsDetLog: $('ws-det-log'), wsMavLog: $('ws-mav-log'), wsLoraLog: $('ws-lora-log'),
  };

  const overlayCtx = els.overlay ? els.overlay.getContext('2d') : null;

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  const state = {
    tracks: [],
    metrics: {},
    selectedTrackId: null,
    mavConnected: false,
    loraConnected: false,
    recording: false,
    loraMessages: [],
    sessions: [],
    recordings: [],
  };

  let mavWs = null, loraWs = null, detWs = null;

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------
  function appendLog(logEl, text) {
    if (!logEl) return;
    logEl.textContent += text + '\n';
    if (logEl.textContent.length > 20000) logEl.textContent = logEl.textContent.slice(-10000);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function updateStatusIndicator(text, variant) {
    if (!els.statusIndicator) return;
    els.statusIndicator.textContent = text;
    els.statusIndicator.classList.remove('detected', 'error');
    if (variant) els.statusIndicator.classList.add(variant);
  }

  async function fetchJSON(url, opts = {}) {
    const timeout = opts.timeout || 8000;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(url, { ...opts, signal: ctrl.signal });
      clearTimeout(timer);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    } catch (err) {
      clearTimeout(timer);
      if (err.name === 'AbortError') throw new Error('Timeout');
      throw err;
    }
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }

  function formatDuration(ms) {
    if (ms < 1000) return ms + 'ms';
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    return m + 'm ' + (s % 60) + 's';
  }

  function formatTimestamp(ts) {
    try {
      const d = new Date(ts * 1000);
      return d.toLocaleDateString('ru-RU') + ' ' + d.toLocaleTimeString('ru-RU');
    } catch { return '—'; }
  }

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------
  async function init() {
    try {
      const svc = await fetchJSON('/api/services');
      ports.cap  = svc.capture.port;
      ports.det  = svc.detection.port;
      ports.mav  = svc.mavlink.port;
      ports.lora = svc.lora.port;
    } catch (e) {
      console.warn('Using default ports:', e.message);
    }

    initTabs();
    initEvents();
    startStream();
    connectDetectionWS();
    connectMavlinkWS();
    connectLoraWS();
    loadModels();
    loadConfig();
  }

  // ---------------------------------------------------------------------------
  // MJPEG stream (raw capture — boxes drawn via canvas overlay)
  // ---------------------------------------------------------------------------
  function startStream() {
    if (!els.stream) return;
    els.stream.src = `${origin(ports.cap)}/stream?t=${Date.now()}`;
  }

  function resizeCanvas() {
    if (!els.stream || !els.overlay) return;
    const rect = els.stream.getBoundingClientRect();
    const w = Math.round(rect.width);
    const h = Math.round(rect.height);
    if (w > 0 && h > 0 && (els.overlay.width !== w || els.overlay.height !== h)) {
      els.overlay.width = w;
      els.overlay.height = h;
    }
    drawOverlay();
  }

  // ---------------------------------------------------------------------------
  // Overlay drawing
  // ---------------------------------------------------------------------------
  function drawOverlay() {
    if (!overlayCtx) return;
    overlayCtx.clearRect(0, 0, els.overlay.width, els.overlay.height);
    if (!state.tracks.length) return;

    const natW = els.stream.naturalWidth || 1;
    const natH = els.stream.naturalHeight || 1;
    const scaleX = els.overlay.width / natW;
    const scaleY = els.overlay.height / natH;

    overlayCtx.textBaseline = 'top';
    overlayCtx.font = `${Math.max(11, Math.round(13 * scaleX))}px 'Segoe UI', sans-serif`;

    state.tracks.forEach((t) => {
      const bbox = t.bbox;
      if (!bbox) return;

      const bx = bbox.x * scaleX;
      const by = bbox.y * scaleY;
      const bw = bbox.w * scaleX;
      const bh = bbox.h * scaleY;
      if (!bw || !bh) return;

      const isSelected = t.track_id === state.selectedTrackId;
      overlayCtx.strokeStyle = isSelected
        ? 'rgba(126,229,255,0.95)'
        : 'rgba(64,255,188,0.85)';
      overlayCtx.lineWidth = isSelected ? 3 : 2;
      overlayCtx.strokeRect(bx, by, bw, bh);

      const conf = (t.confidence * 100).toFixed(1);
      const labelText = `${t.class_name || 'obj'} #${t.track_id} ${conf}%`;
      const metrics = overlayCtx.measureText(labelText);
      const tw = metrics.width + 10;
      const th = Math.max(16, Math.round(18 * scaleY));
      const tx = Math.max(0, Math.min(bx, els.overlay.width - tw));
      const ty = Math.max(0, by - th - 3);

      overlayCtx.fillStyle = 'rgba(10,17,31,0.85)';
      overlayCtx.fillRect(tx, ty, tw, th);
      overlayCtx.fillStyle = isSelected ? '#7ee5ff' : '#40ffbc';
      overlayCtx.fillText(labelText, tx + 5, ty + 2);
    });
  }

  // ---------------------------------------------------------------------------
  // Detection WebSocket
  // ---------------------------------------------------------------------------
  function connectDetectionWS() {
    if (detWs && detWs.readyState < 2) return;
    const url = `${wsProto}//${host}:${ports.det}/ws`;
    detWs = new WebSocket(url);

    detWs.onopen = () => {
      updateStatusIndicator('Online', 'detected');
      appendLog(els.wsDetLog, '[connected]');
    };

    detWs.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.event === 'tracks') {
          state.tracks = Array.isArray(data.tracks) ? data.tracks : [];
          if (data.metrics) {
            state.metrics = data.metrics;
            updateMetrics();
          }
          drawOverlay();
          updateTrackStats();
          renderTrackers();
        }
      } catch { /* ignore */ }
      appendLog(els.wsDetLog, e.data.substring(0, 200));
    };

    detWs.onclose = () => {
      updateStatusIndicator('Disconnected', 'error');
      appendLog(els.wsDetLog, '[disconnected]');
      setTimeout(connectDetectionWS, 3000);
    };
    detWs.onerror = () => {};
  }

  function updateTrackStats() {
    const tracks = state.tracks;
    setText('track-count', tracks.length || '—');
    const fps = state.metrics.fps;
    setText('det-fps', fps !== undefined ? fps : '—');
  }

  function updateMetrics() {
    const m = state.metrics;
    setText('m-fps', m.fps ?? '—');
    setText('m-infer', m.last_inference_ms !== undefined ? m.last_inference_ms + ' ms' : '—');
    setText('m-avg', m.avg_frame_ms !== undefined ? m.avg_frame_ms + ' ms' : '—');
    setText('m-frame', m.frame_number ?? '—');
    setText('m-tracks', m.active_tracks ?? '—');
    setText('m-total', m.total_detections ?? '—');
  }

  // ---------------------------------------------------------------------------
  // Tracker list with thumbnails
  // ---------------------------------------------------------------------------
  function renderTrackers() {
    const container = els.trackerList;
    if (!container) return;

    if (!state.tracks.length) {
      container.innerHTML = '';
      container.classList.add('empty-state');
      container.textContent = 'No active tracks';
      return;
    }

    container.classList.remove('empty-state');
    const sorted = [...state.tracks].sort((a, b) => (a.track_id || 0) - (b.track_id || 0));
    const existingCards = new Map();
    container.querySelectorAll('.tracker-card').forEach((card) => {
      existingCards.set(Number(card.dataset.trackId), card);
    });

    const newIds = new Set();
    sorted.forEach((t) => {
      newIds.add(t.track_id);
      let card = existingCards.get(t.track_id);
      if (!card) {
        card = document.createElement('div');
        card.className = 'tracker-card';
        card.dataset.trackId = t.track_id;
        card.innerHTML = `
          <div class="tracker-thumb"></div>
          <div class="tracker-info">
            <div class="ti-top"><strong></strong><span></span></div>
            <div class="ti-class"></div>
            <div class="ti-life"></div>
          </div>`;
        card.addEventListener('click', () => {
          state.selectedTrackId = (state.selectedTrackId === t.track_id) ? null : t.track_id;
          drawOverlay();
          renderTrackers();
        });
        container.appendChild(card);
      }

      card.classList.toggle('selected', t.track_id === state.selectedTrackId);

      // Thumbnail
      const thumb = card.querySelector('.tracker-thumb');
      if (t.jpeg_url && !thumb.querySelector('img')) {
        const img = document.createElement('img');
        img.src = `${origin(ports.det)}${t.jpeg_url}`;
        img.alt = `Track ${t.track_id}`;
        img.loading = 'lazy';
        img.addEventListener('click', (ev) => { ev.stopPropagation(); openDetectionModal(t); });
        thumb.appendChild(img);
      }

      card.querySelector('.ti-top strong').textContent = `#${t.track_id}`;
      card.querySelector('.ti-top span').textContent = `${(t.confidence * 100).toFixed(1)}%`;
      card.querySelector('.ti-class').textContent = t.class_name || 'object';

      // Lifetime
      if (t.first_seen) {
        const start = new Date(t.first_seen).getTime();
        const now = Date.now();
        const dur = now - start;
        card.querySelector('.ti-life').textContent = `alive ${formatDuration(dur)}`;
      }
    });

    existingCards.forEach((card, tid) => {
      if (!newIds.has(tid)) card.remove();
    });
  }

  // ---------------------------------------------------------------------------
  // Model & Config (Detection tab)
  // ---------------------------------------------------------------------------
  async function loadModels() {
    try {
      const data = await fetchJSON(`${origin(ports.det)}/models`);
      if (!els.modelSelect) return;
      els.modelSelect.innerHTML = '';
      (data.models || []).forEach((m) => {
        const opt = document.createElement('option');
        opt.value = m.name;
        opt.textContent = `${m.name} (${m.size_mb} MB)`;
        if (m.name === data.current) opt.selected = true;
        els.modelSelect.appendChild(opt);
      });
      if (!data.models || !data.models.length) {
        const opt = document.createElement('option');
        opt.textContent = 'No models found';
        els.modelSelect.appendChild(opt);
      }
    } catch { /* service may not be ready */ }
  }

  async function switchModel() {
    const name = els.modelSelect ? els.modelSelect.value : '';
    if (!name) return;
    try {
      await fetchJSON(`${origin(ports.det)}/model`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
    } catch (err) {
      showError('Model switch failed: ' + err.message);
    }
  }

  async function loadConfig() {
    try {
      const cfg = await fetchJSON(`${origin(ports.det)}/config`);
      if (els.confSlider) {
        els.confSlider.value = Math.round(cfg.confidence * 100);
        setText('conf-val', cfg.confidence.toFixed(2));
      }
      if (els.saveConfSlider && cfg.save_confidence != null) {
        els.saveConfSlider.value = Math.round(cfg.save_confidence * 100);
        setText('save-conf-val', cfg.save_confidence.toFixed(2));
      }
      if (els.imgszSelect) els.imgszSelect.value = String(cfg.imgsz);
      if (els.skipSlider) {
        els.skipSlider.value = cfg.skip_frames;
        setText('skip-val', cfg.skip_frames);
      }
    } catch { /* service may not be ready */ }
  }

  async function applyConfig() {
    const conf = els.confSlider ? parseInt(els.confSlider.value, 10) / 100 : 0.5;
    const saveConf = els.saveConfSlider ? parseInt(els.saveConfSlider.value, 10) / 100 : 0.5;
    const imgsz = els.imgszSelect ? parseInt(els.imgszSelect.value, 10) : 640;
    const skip = els.skipSlider ? parseInt(els.skipSlider.value, 10) : 0;
    try {
      await fetchJSON(`${origin(ports.det)}/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confidence: conf, save_confidence: saveConf, imgsz, skip_frames: skip }),
      });
    } catch (err) {
      showError('Config update failed: ' + err.message);
    }
  }

  // ---------------------------------------------------------------------------
  // MAVLink WebSocket — telemetry
  // ---------------------------------------------------------------------------
  const FIX_NAMES = { 0: 'No GPS', 1: 'No Fix', 2: '2D', 3: '3D', 4: 'DGPS', 5: 'RTK Float', 6: 'RTK Fixed' };

  function connectMavlinkWS() {
    if (mavWs && mavWs.readyState < 2) return;
    const url = `${wsProto}//${host}:${ports.mav}/ws`;
    mavWs = new WebSocket(url);

    mavWs.onopen = () => {
      state.mavConnected = true;
      setMavStatus(true);
      appendLog(els.wsMavLog, '[connected]');
    };

    mavWs.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.structured) updateTelemetry(d.structured);
        if (d.connected !== undefined) {
          state.mavConnected = d.connected;
          setMavStatus(d.connected);
        }
      } catch { /* ignore */ }
      appendLog(els.wsMavLog, e.data.substring(0, 200));
    };

    mavWs.onclose = () => {
      state.mavConnected = false;
      setMavStatus(false);
      appendLog(els.wsMavLog, '[disconnected]');
      setTimeout(connectMavlinkWS, 3000);
    };
    mavWs.onerror = () => {};
  }

  function setMavStatus(on) {
    const dot = $('mav-dot');
    if (dot) on ? dot.classList.add('on') : dot.classList.remove('on');
    if (els.mavStatus) {
      els.mavStatus.innerHTML = `<span class="dot ${on ? 'on' : ''}" id="mav-dot"></span> ${on ? 'OK' : '—'}`;
    }
  }

  function updateTelemetry(t) {
    if (!t) return;
    const g = t.gps || {};
    setText('t-lat', (g.lat || 0).toFixed(6));
    setText('t-lon', (g.lon || 0).toFixed(6));
    setText('t-alt-msl', (g.alt_msl || 0).toFixed(1) + ' m');
    setText('t-alt-rel', (g.alt_rel || 0).toFixed(1) + ' m');
    setText('t-fix', FIX_NAMES[g.fix_type] || String(g.fix_type || '—'));
    setText('t-sats', g.satellites || 0);
    setText('t-hdop', (g.hdop || 0).toFixed(1));

    const h = t.heartbeat || {};
    const v = t.vfr || {};
    const modeEl = $('t-mode');
    if (modeEl) modeEl.innerHTML = `<span class="mode-badge">${h.mode || 'N/A'}</span>`;
    const armedEl = $('t-armed');
    if (armedEl) {
      armedEl.innerHTML = h.armed
        ? '<span class="armed-badge yes">ARMED</span>'
        : '<span class="armed-badge">DISARMED</span>';
    }
    setText('t-speed', (v.groundspeed || 0).toFixed(1) + ' m/s');
    setText('t-airspeed', (v.airspeed || 0).toFixed(1) + ' m/s');
    setText('t-heading', (v.heading || 0) + '\u00b0');
    setText('t-climb', (v.climb || 0).toFixed(1) + ' m/s');
    setText('t-throttle', (v.throttle || 0) + '%');

    const b = t.battery || {};
    setText('t-bat-v', (b.voltage || 0).toFixed(1) + ' V');
    setText('t-bat-a', (b.current || 0).toFixed(1) + ' A');
    const pct = b.remaining >= 0 ? b.remaining : -1;
    if (pct >= 0) {
      setText('t-bat-pct', pct + '%');
      const bar = $('t-bat-bar');
      if (bar) {
        bar.style.width = pct + '%';
        bar.className = 'bat-bar ' + (pct > 30 ? 'high' : pct > 15 ? 'mid' : 'low');
      }
      const pctEl = $('t-bat-pct');
      if (pctEl) pctEl.className = 'val' + (pct <= 15 ? ' crit' : pct <= 30 ? ' warn' : ' ok');
    } else {
      setText('t-bat-pct', 'N/A');
    }

    const a = t.attitude || {};
    setText('t-roll', (a.roll || 0).toFixed(1) + '\u00b0');
    setText('t-pitch', (a.pitch || 0).toFixed(1) + '\u00b0');
    setText('t-yaw', (a.yaw || 0).toFixed(1) + '\u00b0');
  }

  // ---------------------------------------------------------------------------
  // LoRa WebSocket + messaging
  // ---------------------------------------------------------------------------
  function connectLoraWS() {
    if (loraWs && loraWs.readyState < 2) return;
    const url = `${wsProto}//${host}:${ports.lora}/ws`;
    loraWs = new WebSocket(url);

    loraWs.onopen = () => {
      state.loraConnected = true;
      setLoraStatus(true);
      appendLog(els.wsLoraLog, '[connected]');
    };

    loraWs.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.connected !== undefined) {
          state.loraConnected = d.connected;
          setLoraStatus(d.connected);
        }
        if (d.event === 'messages' && Array.isArray(d.messages)) {
          d.messages.forEach((m) => addLoraMsg(m));
        }
      } catch { /* ignore */ }
      appendLog(els.wsLoraLog, e.data.substring(0, 200));
    };

    loraWs.onclose = () => {
      state.loraConnected = false;
      setLoraStatus(false);
      appendLog(els.wsLoraLog, '[disconnected]');
      setTimeout(connectLoraWS, 3000);
    };
    loraWs.onerror = () => {};
  }

  function setLoraStatus(on) {
    const dot = $('lora-dot');
    if (dot) on ? dot.classList.add('on') : dot.classList.remove('on');
    if (els.loraStatus) {
      els.loraStatus.innerHTML = `<span class="dot ${on ? 'on' : ''}" id="lora-dot"></span> ${on ? 'OK' : '—'}`;
    }
    if (els.loraConnLabel) {
      els.loraConnLabel.textContent = on ? 'Connected' : 'Disconnected';
      els.loraConnLabel.classList.toggle('on', on);
    }
  }

  function addLoraMsg(msg) {
    if (!els.loraLog) return;
    const div = document.createElement('div');
    const dir = msg.direction || 'rx';
    const isTel = msg.type === 'telemetry' || msg.type === 'telemetry_rx';
    div.className = 'lora-msg ' + dir + (isTel ? ' tel' : '');

    const ts = msg.ts ? new Date(msg.ts * 1000).toLocaleTimeString('ru-RU') : '';
    const arrow = dir === 'tx' ? '\u2192' : '\u2190';
    const rssi = msg.rssi !== undefined ? ` [RSSI: ${msg.rssi}]` : '';

    div.innerHTML = `<span class="lm-time">${ts}</span><span class="lm-dir">${arrow}</span><span class="lm-data">${msg.data || ''}${rssi}</span>`;
    els.loraLog.appendChild(div);
    els.loraLog.scrollTop = els.loraLog.scrollHeight;

    while (els.loraLog.children.length > 500) {
      els.loraLog.removeChild(els.loraLog.firstChild);
    }

    state.loraMessages.push(msg);
    updateLoraStats();
  }

  function updateLoraStats() {
    if (!els.loraStats) return;
    const msgs = state.loraMessages;
    const tx = msgs.filter((m) => m.direction === 'tx').length;
    const rx = msgs.filter((m) => m.direction === 'rx').length;
    const tel = msgs.filter((m) => m.type === 'telemetry' || m.type === 'telemetry_rx').length;
    els.loraStats.textContent = `TX: ${tx} | RX: ${rx} | Telemetry: ${tel} | Total: ${msgs.length}`;
  }

  async function sendLoraMessage() {
    const text = els.loraText ? els.loraText.value.trim() : '';
    if (!text) return;
    try {
      await fetchJSON(`${origin(ports.lora)}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      addLoraMsg({ direction: 'tx', data: text, ts: Date.now() / 1000 });
      if (els.loraText) els.loraText.value = '';
    } catch (err) {
      addLoraMsg({ direction: 'tx', data: `[Error: ${err.message}] ${text}`, ts: Date.now() / 1000 });
    }
  }

  // ---------------------------------------------------------------------------
  // DB tab — Recordings
  // ---------------------------------------------------------------------------
  async function loadRecordings() {
    if (!els.recList) return;
    try {
      const data = await fetchJSON(`${origin(ports.cap)}/recordings`);
      state.recordings = Array.isArray(data.recordings) ? data.recordings : [];
      renderRecordings();
    } catch (err) {
      els.recList.innerHTML = `<div style="color:#ff7f8a;padding:8px">Error: ${err.message}</div>`;
    }
  }

  function renderRecordings() {
    if (!els.recList) return;
    let recs = [...(state.recordings || [])];

    // Sort using the shared selector
    const sort = els.dbSort ? els.dbSort.value : 'date-desc';
    if (sort === 'date-asc') recs.sort((a, b) => (a.modified || 0) - (b.modified || 0));
    else recs.sort((a, b) => (b.modified || 0) - (a.modified || 0));

    if (!recs.length) {
      els.recList.innerHTML = '<div style="color:#64748b;text-align:center;padding:12px">No recordings</div>';
      return;
    }
    els.recList.innerHTML = '';
    recs.forEach((r) => {
      const name = typeof r === 'string' ? r : (r.filename || 'recording');
      const size = r.size_bytes ? formatBytes(r.size_bytes) : '';
      const date = r.modified ? formatTimestamp(r.modified) : '';
      const item = document.createElement('div');
      item.className = 'rec-item';
      item.innerHTML = `
        <span class="rec-name" title="${name}">${name}</span>
        <span class="rec-date">${date}</span>
        <span class="rec-size">${size}</span>
        <div class="rec-actions">
          <a href="${origin(ports.cap)}/recordings/${encodeURIComponent(name)}" target="_blank" style="color:#7ee5ff;text-decoration:none;font-size:0.72rem;padding:2px 6px">DL</a>
          <button class="sm danger del-rec-btn" data-name="${name}">Del</button>
        </div>`;
      els.recList.appendChild(item);
    });
    // Bind delete buttons
    els.recList.querySelectorAll('.del-rec-btn').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const fn = btn.dataset.name;
        if (!confirm(`Delete recording "${fn}"?`)) return;
        try {
          await fetchJSON(`${origin(ports.cap)}/recordings/${encodeURIComponent(fn)}`, { method: 'DELETE' });
          loadRecordings();
        } catch (err) {
          showError('Delete failed: ' + err.message);
        }
      });
    });
  }

  async function startRecording() {
    try {
      if (els.recStartBtn) els.recStartBtn.disabled = true;
      await fetchJSON(`${origin(ports.cap)}/recording/start`, { method: 'POST' });
      state.recording = true;
      updateRecUI();
    } catch (err) {
      showError('Recording error: ' + err.message);
    } finally {
      if (els.recStartBtn) els.recStartBtn.disabled = false;
    }
  }

  async function stopRecording() {
    try {
      if (els.recStopBtn) els.recStopBtn.disabled = true;
      await fetchJSON(`${origin(ports.cap)}/recording/stop`, { method: 'POST' });
      state.recording = false;
      updateRecUI();
      loadRecordings();
    } catch (err) {
      showError('Stop error: ' + err.message);
    } finally {
      if (els.recStopBtn) els.recStopBtn.disabled = false;
    }
  }

  function updateRecUI() {
    if (els.recDot) els.recDot.classList.toggle('active', state.recording);
    if (els.recStatus) {
      els.recStatus.textContent = state.recording ? 'REC' : 'Idle';
      els.recStatus.style.color = state.recording ? '#ff4040' : '#94a3b8';
    }
    if (els.recStartBtn) els.recStartBtn.disabled = state.recording;
    if (els.recStopBtn) els.recStopBtn.disabled = !state.recording;
  }

  // ---------------------------------------------------------------------------
  // DB tab — Detection Sessions
  // ---------------------------------------------------------------------------
  async function loadSessions() {
    if (!els.sessList) return;
    try {
      const data = await fetchJSON(`${origin(ports.det)}/sessions`);
      state.sessions = data.sessions || [];
      renderSessions();
    } catch (err) {
      els.sessList.innerHTML = `<div style="color:#ff7f8a;padding:8px">Error: ${err.message}</div>`;
    }
  }

  function renderSessions() {
    if (!els.sessList) return;
    let sessions = [...state.sessions];

    // Filter by class
    const classFilter = (els.sessClassFilter ? els.sessClassFilter.value.trim().toLowerCase() : '');
    if (classFilter) {
      sessions = sessions.filter((s) =>
        s.classes && s.classes.some((c) => c.toLowerCase().includes(classFilter))
      );
    }

    // Sort (shared selector)
    const sort = els.dbSort ? els.dbSort.value : 'date-desc';
    if (sort === 'date-asc') sessions.sort((a, b) => a.created - b.created);
    else if (sort === 'det-desc') sessions.sort((a, b) => b.detections - a.detections);
    else sessions.sort((a, b) => b.created - a.created);

    if (!sessions.length) {
      els.sessList.innerHTML = '<div style="color:#64748b;text-align:center;padding:12px">No sessions</div>';
      return;
    }

    els.sessList.innerHTML = '';
    sessions.forEach((s) => {
      const card = document.createElement('div');
      card.className = 'sess-card' + (s.active ? ' sess-active' : '');
      const date = formatTimestamp(s.created);
      const size = formatBytes(s.size_bytes || 0);
      card.innerHTML = `
        <div class="sess-top">
          <span class="sess-id">${s.session_id}</span>
          ${s.active ? '<span class="active-badge">Active</span>' : ''}
          <span class="sess-date">${date}</span>
        </div>
        <div class="sess-meta">
          <span>Detections: ${s.detections}</span>
          <span>Tracks: ${s.tracks}</span>
          <span>GIFs: ${s.gifs}</span>
          <span>${size}</span>
        </div>
        ${s.classes && s.classes.length ? `<div class="sess-classes">${s.classes.join(', ')}</div>` : ''}
        <div class="sess-actions">
          <button class="sm sess-view-btn" data-sid="${s.session_id}">View</button>
          ${s.active ? '' : `<button class="sm danger sess-del-btn" data-sid="${s.session_id}">Delete</button>`}
        </div>`;
      els.sessList.appendChild(card);
    });

    // Bind view buttons
    els.sessList.querySelectorAll('.sess-view-btn').forEach((btn) => {
      btn.addEventListener('click', () => viewSession(btn.dataset.sid));
    });

    // Bind delete buttons
    els.sessList.querySelectorAll('.sess-del-btn').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const sid = btn.dataset.sid;
        if (!confirm(`Delete session "${sid}"?`)) return;
        try {
          await fetchJSON(`${origin(ports.det)}/sessions/${sid}`, { method: 'DELETE' });
          loadSessions();
        } catch (err) {
          showError('Delete failed: ' + err.message);
        }
      });
    });
  }

  async function viewSession(sessionId) {
    try {
      const data = await fetchJSON(`${origin(ports.det)}/detections`);
      const dets = Array.isArray(data.detections) ? data.detections : [];
      if (!dets.length) {
        showError('No detections to show');
        return;
      }
      // Group by track_id, show latest per track
      const byTrack = new Map();
      dets.forEach((d) => { if (d.track_id >= 0) byTrack.set(d.track_id, d); });
      if (!byTrack.size) {
        showError('No tracked detections');
        return;
      }
      // Open first track in modal
      const first = byTrack.values().next().value;
      openDetectionModal(first);
    } catch (err) {
      showError('Error: ' + err.message);
    }
  }

  // ---------------------------------------------------------------------------
  // Modal
  // ---------------------------------------------------------------------------
  function openDetectionModal(det) {
    if (!els.modal) return;
    if (els.modalImage) {
      const url = det.gif_url || det.jpeg_url;
      els.modalImage.src = url ? `${origin(ports.det)}${url}` : '';
      els.modalImage.alt = `Track ${det.track_id}`;
    }
    if (els.modalDl) {
      els.modalDl.innerHTML = '';
      const pairs = [
        ['Track ID', det.track_id],
        ['Class', det.class_name],
        ['Confidence', ((det.confidence || 0) * 100).toFixed(2) + '%'],
        ['Frame', det.frame_number],
        ['Time', det.timestamp],
        ['First Seen', det.first_seen || '—'],
      ];
      if (det.bbox) {
        pairs.push(['BBox', `x:${det.bbox.x} y:${det.bbox.y} ${det.bbox.w}x${det.bbox.h}`]);
      }
      pairs.forEach(([label, value]) => {
        if (value === undefined || value === null) return;
        const dt = document.createElement('dt');
        dt.textContent = label;
        const dd = document.createElement('dd');
        dd.textContent = String(value);
        els.modalDl.append(dt, dd);
      });
    }
    els.modal.classList.remove('hidden');
    els.modal.setAttribute('aria-hidden', 'false');
  }

  function closeModal() {
    if (!els.modal) return;
    els.modal.classList.add('hidden');
    els.modal.setAttribute('aria-hidden', 'true');
    if (els.modalImage) els.modalImage.src = '';
  }

  // ---------------------------------------------------------------------------
  // Error display
  // ---------------------------------------------------------------------------
  function showError(msg) {
    if (els.errorMessage) {
      els.errorMessage.textContent = msg;
      setTimeout(() => { if (els.errorMessage) els.errorMessage.textContent = ''; }, 5000);
    }
  }

  // ---------------------------------------------------------------------------
  // Tabs
  // ---------------------------------------------------------------------------
  function initTabs() {
    const btns = document.querySelectorAll('.tab-btn');
    const panels = document.querySelectorAll('.tab-panel');

    btns.forEach((btn) => {
      btn.addEventListener('click', () => {
        const target = btn.dataset.tab;
        btns.forEach((b) => b.classList.toggle('active', b === btn));
        panels.forEach((p) => p.classList.toggle('active', p.id === `tab-${target}`));

        // Load data on tab switch
        if (target === 'db') { loadRecordings(); loadSessions(); }
        if (target === 'lora' && !state.loraMessages.length) loadLoraHistory();
      });
    });
  }

  async function loadLoraHistory() {
    try {
      const data = await fetchJSON(`${origin(ports.lora)}/messages`);
      const msgs = data.messages || [];
      msgs.forEach((m) => addLoraMsg(m));
    } catch { /* ok */ }
  }

  // ---------------------------------------------------------------------------
  // Events
  // ---------------------------------------------------------------------------
  function initEvents() {
    window.addEventListener('resize', resizeCanvas);

    if (els.stream) {
      els.stream.onload = resizeCanvas;
      els.stream.onerror = () => {
        updateStatusIndicator('Stream Error', 'error');
        showError('Failed to connect to video stream');
        setTimeout(startStream, 3000);
      };
    }

    // Detection tab — config
    if (els.confSlider) els.confSlider.addEventListener('input', () => {
      setText('conf-val', (parseInt(els.confSlider.value, 10) / 100).toFixed(2));
    });
    if (els.saveConfSlider) els.saveConfSlider.addEventListener('input', () => {
      setText('save-conf-val', (parseInt(els.saveConfSlider.value, 10) / 100).toFixed(2));
    });
    if (els.skipSlider) els.skipSlider.addEventListener('input', () => {
      setText('skip-val', els.skipSlider.value);
    });
    if (els.applyConfigBtn) els.applyConfigBtn.addEventListener('click', applyConfig);
    if (els.modelSwitchBtn) els.modelSwitchBtn.addEventListener('click', switchModel);

    // Tracker refresh
    if (els.trackerRefresh) els.trackerRefresh.addEventListener('click', async () => {
      try {
        const data = await fetchJSON(`${origin(ports.det)}/tracks`);
        state.tracks = data.tracks || [];
        drawOverlay();
        updateTrackStats();
        renderTrackers();
      } catch (err) { showError(err.message); }
    });

    // LoRa
    if (els.loraSendBtn) els.loraSendBtn.addEventListener('click', sendLoraMessage);
    if (els.loraText) els.loraText.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') sendLoraMessage();
    });
    if (els.loraClearBtn) els.loraClearBtn.addEventListener('click', () => {
      if (els.loraLog) els.loraLog.innerHTML = '';
      state.loraMessages = [];
      updateLoraStats();
    });

    // DB - Recording
    if (els.recStartBtn) els.recStartBtn.addEventListener('click', startRecording);
    if (els.recStopBtn) els.recStopBtn.addEventListener('click', stopRecording);

    // DB - Shared sort/filter & refresh
    if (els.dbRefreshBtn) els.dbRefreshBtn.addEventListener('click', () => {
      loadRecordings();
      loadSessions();
    });
    if (els.dbSort) els.dbSort.addEventListener('change', () => {
      renderRecordings();
      renderSessions();
    });
    if (els.sessClassFilter) els.sessClassFilter.addEventListener('input', renderSessions);

    // Modal
    if (els.modalClose) els.modalClose.addEventListener('click', closeModal);
    if (els.modalBackdrop) els.modalBackdrop.addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
  }

  // ---------------------------------------------------------------------------
  // Start
  // ---------------------------------------------------------------------------
  init();
})();
