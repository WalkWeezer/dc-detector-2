// DC-Detector v0.2 — Веб-интерфейс
// Вкладки: Детекция, Телеметрия, LoRa, Обнаружения, Записи
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
    detToggle: $('det-toggle'), detToggleLabel: $('det-toggle-label'),
    modelSelect: $('model-select'), modelSwitchBtn: $('model-switch-btn'),
    confSlider: $('conf-slider'), confVal: $('conf-val'),
    saveConfSlider: $('save-conf-slider'), saveConfVal: $('save-conf-val'),
    imgszSelect: $('imgsz-select'),
    skipSlider: $('skip-slider'), skipVal: $('skip-val'),
    applyConfigBtn: $('apply-config-btn'),
    awbModeSelect: $('awb-mode-select'), awbGainsGroup: $('awb-gains-group'),
    awbRedSlider: $('awb-red-slider'), awbRedVal: $('awb-red-val'),
    awbBlueSlider: $('awb-blue-slider'), awbBlueVal: $('awb-blue-val'),
    applyAwbBtn: $('apply-awb-btn'),
    trackerList: $('tracker-list'), trackerRefresh: $('tracker-refresh'),
    // LoRa
    loraText: $('lora-text'), loraSendBtn: $('lora-send-btn'),
    loraLog: $('lora-log'), loraConnLabel: $('lora-conn-label'),
    loraStats: $('lora-stats'), loraClearBtn: $('lora-clear-btn'),
    // Recording HUD (video overlay)
    recHudIndicator: $('rec-hud-indicator'),
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
    // Log popup
    logToggle: $('log-toggle'), logPopup: $('log-popup'), logPopupClose: $('log-popup-close'),
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
    recordingFile: null,   // basename of active recording (e.g. "rec_20260225.avi")
    loraMessages: [],
    sessions: [],
    recordings: [],
  };

  let mavWs = null, loraWs = null, detWs = null, capWs = null;
  const _knownTrackIds = new Set();  // for new-detection toasts

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
      if (err.name === 'AbortError') throw new Error('Таймаут');
      throw err;
    }
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' Б';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' КБ';
    return (bytes / 1048576).toFixed(1) + ' МБ';
  }

  function formatDuration(ms) {
    if (ms < 1000) return ms + ' мс';
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + ' с';
    const m = Math.floor(s / 60);
    return m + ' мин ' + (s % 60) + ' с';
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
    loadAwb();
    connectCaptureWS();
  }

  // ---------------------------------------------------------------------------
  // Capture WebSocket — recording status push
  // ---------------------------------------------------------------------------
  function connectCaptureWS() {
    if (capWs && capWs.readyState < 2) return;
    const url = `${wsProto}//${host}:${ports.cap}/ws`;
    capWs = new WebSocket(url);

    capWs.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.event === 'status') {
          const wasRecording = state.recording;
          state.recording = !!data.recording;
          state.recordingFile = data.recording_path
            ? data.recording_path.replace(/\\/g, '/').split('/').pop()
            : null;
          updateRecUI();
          // Refresh recordings list when recording state changes
          if (wasRecording && !state.recording) loadRecordings();
        } else if (data.event === 'recording_started') {
          state.recording = true;
          state.recordingFile = data.path
            ? data.path.replace(/\\/g, '/').split('/').pop()
            : null;
          updateRecUI();
          loadRecordings();
        } else if (data.event === 'recording_stopped') {
          state.recording = false;
          state.recordingFile = null;
          updateRecUI();
          loadRecordings();
        }
      } catch { /* ignore parse errors */ }
    };

    capWs.onclose = () => { setTimeout(connectCaptureWS, 3000); };
    capWs.onerror = () => { capWs.close(); };
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
    // Canvas must match the container (.video-wrapper), not the <img> element,
    // because object-fit:contain may leave letterbox bars.
    const wrapper = els.stream.parentElement;
    if (!wrapper) return;
    const w = Math.round(wrapper.clientWidth);
    const h = Math.round(wrapper.clientHeight);
    if (w > 0 && h > 0 && (els.overlay.width !== w || els.overlay.height !== h)) {
      els.overlay.width = w;
      els.overlay.height = h;
    }
    drawOverlay();
  }

  // ---------------------------------------------------------------------------
  // Overlay drawing — accounts for object-fit:contain letterboxing
  // ---------------------------------------------------------------------------

  /**
   * Compute the actual rendered image rect inside the container.
   * object-fit:contain scales uniformly and centers the image.
   */
  function getImageLayout() {
    const cW = els.overlay.width;
    const cH = els.overlay.height;
    const natW = els.stream.naturalWidth || 1;
    const natH = els.stream.naturalHeight || 1;

    const scale = Math.min(cW / natW, cH / natH);
    const renderW = natW * scale;
    const renderH = natH * scale;
    const offsetX = (cW - renderW) / 2;
    const offsetY = (cH - renderH) / 2;

    return { scale, offsetX, offsetY, renderW, renderH };
  }

  function drawOverlay() {
    if (!overlayCtx) return;
    overlayCtx.clearRect(0, 0, els.overlay.width, els.overlay.height);
    if (!state.tracks.length) return;

    const { scale, offsetX, offsetY } = getImageLayout();

    overlayCtx.textBaseline = 'top';
    overlayCtx.font = `${Math.max(11, Math.round(13 * scale))}px 'Segoe UI', sans-serif`;

    state.tracks.forEach((t) => {
      const bbox = t.bbox;
      if (!bbox) return;

      const bx = bbox.x * scale + offsetX;
      const by = bbox.y * scale + offsetY;
      const bw = bbox.w * scale;
      const bh = bbox.h * scale;
      if (!bw || !bh) return;

      const isSelected = t.track_id === state.selectedTrackId;
      overlayCtx.strokeStyle = isSelected
        ? 'rgba(126,229,255,0.95)'
        : 'rgba(64,255,188,0.85)';
      overlayCtx.lineWidth = isSelected ? 3 : 2;
      overlayCtx.strokeRect(bx, by, bw, bh);

      const conf = (t.confidence * 100).toFixed(1);
      const labelText = `${t.class_name || 'объект'} #${t.track_id} ${conf}%`;
      const metrics = overlayCtx.measureText(labelText);
      const tw = metrics.width + 10;
      const th = Math.max(16, Math.round(18 * scale));
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
      updateStatusIndicator('На связи', 'detected');
      appendLog(els.wsDetLog, '[подключено]');
    };

    detWs.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.event === 'tracks') {
          state.tracks = Array.isArray(data.tracks) ? data.tracks : [];
          if (data.metrics) {
            state.metrics = data.metrics;
            if (data.metrics.enabled !== undefined) syncDetToggle(data.metrics.enabled);
            updateMetrics();
          }
          drawOverlay();
          updateTrackStats();
          renderTrackers();

          // Toast for new tracks
          state.tracks.forEach((t) => {
            if (t.track_id >= 0 && !_knownTrackIds.has(t.track_id)) {
              _knownTrackIds.add(t.track_id);
              const conf = (t.confidence * 100).toFixed(0);
              showToast(`${t.class_name || 'объект'} #${t.track_id} (${conf}%)`, 'detect');
            }
          });

          // Toast when total_detections increases (saved to DB)
          const total = data.metrics && data.metrics.total_detections;
          if (total !== undefined && state._lastSavedTotal !== undefined && total > state._lastSavedTotal) {
            showToast(`Детекция сохранена (всего ${total})`, 'save', 2500);
          }
          state._lastSavedTotal = total;
        }
      } catch { /* ignore */ }
      appendLog(els.wsDetLog, e.data.substring(0, 200));
    };

    detWs.onclose = () => {
      updateStatusIndicator('Нет связи', 'error');
      appendLog(els.wsDetLog, '[отключено]');
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
    setText('m-infer', m.last_inference_ms !== undefined ? m.last_inference_ms + ' мс' : '—');
    setText('m-avg', m.avg_frame_ms !== undefined ? m.avg_frame_ms + ' мс' : '—');
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
      container.textContent = 'Нет активных треков';
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
        img.alt = `Трек ${t.track_id}`;
        img.loading = 'lazy';
        img.addEventListener('click', (ev) => { ev.stopPropagation(); openDetectionModal(t); });
        thumb.appendChild(img);
      }

      card.querySelector('.ti-top strong').textContent = `#${t.track_id}`;
      card.querySelector('.ti-top span').textContent = `${(t.confidence * 100).toFixed(1)}%`;
      card.querySelector('.ti-class').textContent = t.class_name || 'объект';

      // Lifetime
      if (t.first_seen) {
        const start = new Date(t.first_seen).getTime();
        const now = Date.now();
        const dur = now - start;
        card.querySelector('.ti-life').textContent = `${formatDuration(dur)}`;
      }
    });

    existingCards.forEach((card, tid) => {
      if (!newIds.has(tid)) card.remove();
    });
  }

  // ---------------------------------------------------------------------------
  // Model & Config (Detection tab)
  // ---------------------------------------------------------------------------
  async function loadModels(retries = 3) {
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
        opt.textContent = 'Модели не найдены';
        els.modelSelect.appendChild(opt);
        if (retries > 0) setTimeout(() => loadModels(retries - 1), 3000);
      }
    } catch {
      // Service may not be ready — retry
      if (retries > 0) setTimeout(() => loadModels(retries - 1), 3000);
    }
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
      showError('Ошибка смены модели: ' + err.message);
    }
  }

  async function loadConfig() {
    try {
      const cfg = await fetchJSON(`${origin(ports.det)}/config`);
      if (cfg.enabled !== undefined) syncDetToggle(cfg.enabled);
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

  async function toggleDetection() {
    const enabled = els.detToggle ? els.detToggle.checked : true;
    if (els.detToggleLabel) els.detToggleLabel.textContent = enabled ? 'ВКЛ' : 'ВЫКЛ';
    try {
      await fetchJSON(`${origin(ports.det)}/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
    } catch (err) {
      showError('Ошибка переключения детекции: ' + err.message);
    }
  }

  function syncDetToggle(enabled) {
    if (els.detToggle) els.detToggle.checked = enabled;
    if (els.detToggleLabel) els.detToggleLabel.textContent = enabled ? 'ВКЛ' : 'ВЫКЛ';
  }

  // ---------------------------------------------------------------------------
  // AWB (Capture service)
  // ---------------------------------------------------------------------------
  async function loadAwb() {
    try {
      const data = await fetchJSON(`${origin(ports.cap)}/awb`);
      if (els.awbModeSelect) els.awbModeSelect.value = data.awb_mode || 'auto';
      if (data.colour_gains && data.colour_gains.length === 2) {
        if (els.awbRedSlider) { els.awbRedSlider.value = Math.round(data.colour_gains[0] * 100); setText('awb-red-val', data.colour_gains[0].toFixed(2)); }
        if (els.awbBlueSlider) { els.awbBlueSlider.value = Math.round(data.colour_gains[1] * 100); setText('awb-blue-val', data.colour_gains[1].toFixed(2)); }
      }
      updateAwbGainsVisibility();
    } catch { /* service may not be ready */ }
  }

  function updateAwbGainsVisibility() {
    if (!els.awbGainsGroup || !els.awbModeSelect) return;
    els.awbGainsGroup.style.display = els.awbModeSelect.value === 'off' ? 'flex' : 'none';
  }

  async function applyAwb() {
    const mode = els.awbModeSelect ? els.awbModeSelect.value : 'auto';
    const body = { awb_mode: mode };
    if (mode === 'off') {
      const red = els.awbRedSlider ? parseInt(els.awbRedSlider.value, 10) / 100 : 1.0;
      const blue = els.awbBlueSlider ? parseInt(els.awbBlueSlider.value, 10) / 100 : 1.0;
      body.colour_gains = [red, blue];
    }
    try {
      await fetchJSON(`${origin(ports.cap)}/awb`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (err) {
      showError('Ошибка AWB: ' + err.message);
    }
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
      showError('Ошибка обновления настроек: ' + err.message);
    }
  }

  // ---------------------------------------------------------------------------
  // MAVLink WebSocket — telemetry
  // ---------------------------------------------------------------------------
  const FIX_NAMES = { 0: 'Нет GPS', 1: 'Нет фикс.', 2: '2D', 3: '3D', 4: 'DGPS', 5: 'RTK Float', 6: 'RTK Fixed' };

  function connectMavlinkWS() {
    if (mavWs && mavWs.readyState < 2) return;
    const url = `${wsProto}//${host}:${ports.mav}/ws`;
    mavWs = new WebSocket(url);

    mavWs.onopen = () => {
      state.mavConnected = true;
      setMavStatus(true);
      appendLog(els.wsMavLog, '[подключено]');
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
      appendLog(els.wsMavLog, '[отключено]');
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
    setText('t-alt-msl', (g.alt_msl || 0).toFixed(1) + ' м');
    setText('t-alt-rel', (g.alt_rel || 0).toFixed(1) + ' м');
    setText('t-fix', FIX_NAMES[g.fix_type] || String(g.fix_type || '—'));
    setText('t-sats', g.satellites || 0);
    setText('t-hdop', (g.hdop || 0).toFixed(1));

    const h = t.heartbeat || {};
    const v = t.vfr || {};
    const modeEl = $('t-mode');
    if (modeEl) modeEl.innerHTML = `<span class="mode-badge">${h.mode || 'Н/Д'}</span>`;
    const armedEl = $('t-armed');
    if (armedEl) {
      armedEl.innerHTML = h.armed
        ? '<span class="armed-badge yes">АКТИВЕН</span>'
        : '<span class="armed-badge">НЕАКТИВЕН</span>';
    }
    setText('t-speed', (v.groundspeed || 0).toFixed(1) + ' м/с');
    setText('t-airspeed', (v.airspeed || 0).toFixed(1) + ' м/с');
    setText('t-heading', (v.heading || 0) + '\u00b0');
    setText('t-climb', (v.climb || 0).toFixed(1) + ' м/с');
    setText('t-throttle', (v.throttle || 0) + '%');

    const b = t.battery || {};
    setText('t-bat-v', (b.voltage || 0).toFixed(1) + ' В');
    setText('t-bat-a', (b.current || 0).toFixed(1) + ' А');
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
      setText('t-bat-pct', 'Н/Д');
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
      appendLog(els.wsLoraLog, '[подключено]');
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
      appendLog(els.wsLoraLog, '[отключено]');
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
      els.loraConnLabel.textContent = on ? 'Подключено' : 'Отключено';
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
    els.loraStats.textContent = `Отпр: ${tx} | Принято: ${rx} | Телеметрия: ${tel} | Всего: ${msgs.length}`;
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
      addLoraMsg({ direction: 'tx', data: `[Ошибка: ${err.message}] ${text}`, ts: Date.now() / 1000 });
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
      els.recList.innerHTML = `<div style="color:#ff7f8a;padding:8px">Ошибка: ${err.message}</div>`;
    }
  }

  function renderRecordings() {
    if (!els.recList) return;
    const recs = [...(state.recordings || [])];

    // Sort
    const sort = els.dbSort ? els.dbSort.value : 'date-desc';
    if (sort === 'date-asc') recs.sort((a, b) => (a.modified || 0) - (b.modified || 0));
    else recs.sort((a, b) => (b.modified || 0) - (a.modified || 0));

    if (!recs.length) {
      els.recList.innerHTML = '<div style="color:#64748b;text-align:center;padding:12px">Нет записей</div>';
      return;
    }

    // Group files by timestamp: rec_20260225_143000.avi & det_20260225_143000.avi → one group
    const groups = new Map(); // key = timestamp, value = { raw, det, date, size }
    const byName = new Map();
    recs.forEach((r) => {
      const name = typeof r === 'string' ? r : (r.filename || '');
      byName.set(name, r);
      const m = name.match(/^(rec|det)_(.+)\.avi$/);
      const key = m ? m[2] : name;  // timestamp or full name for unknown files
      if (!groups.has(key)) groups.set(key, { raw: null, det: null, modified: 0, size: 0 });
      const g = groups.get(key);
      if (m && m[1] === 'det') g.det = name;
      else if (m && m[1] === 'rec') g.raw = name;
      else g.raw = name;  // unknown format — treat as raw
      g.modified = Math.max(g.modified, r.modified || 0);
      g.size += r.size_bytes || 0;
    });

    // Active recording ID
    const activeId = (state.recording && state.recordingFile)
      ? (state.recordingFile.match(/^rec_(.+)\.avi$/) || [])[1]
      : null;

    const dlUrl = (name) => `${origin(ports.cap)}/recordings/${encodeURIComponent(name)}`;

    els.recList.innerHTML = '';
    groups.forEach((g, key) => {
      const isActive = activeId && key === activeId;
      const date = g.modified ? formatTimestamp(g.modified) : '';
      const size = g.size ? formatBytes(g.size) : '';

      const item = document.createElement('div');
      item.className = 'rec-item' + (isActive ? ' rec-active' : '');
      item.innerHTML = `
        <span class="rec-name" title="${key}">${key}</span>
        ${isActive ? '<span class="active-badge">Запись...</span>' : ''}
        <span class="rec-date">${date}</span>
        <span class="rec-size">${size}</span>
        <div class="rec-actions">
          ${!isActive && g.raw ? `<a class="rec-dl-btn rec-dl-raw" href="${dlUrl(g.raw)}" target="_blank">Сырое</a>` : ''}
          ${!isActive && g.det ? `<a class="rec-dl-btn rec-dl-det" href="${dlUrl(g.det)}" target="_blank">Детекция</a>` : ''}
          ${!isActive ? `<button class="sm danger del-rec-btn" data-raw="${g.raw || ''}" data-det="${g.det || ''}">✕</button>` : ''}
        </div>`;
      els.recList.appendChild(item);
    });

    // Bind delete buttons — delete both files in the group
    els.recList.querySelectorAll('.del-rec-btn').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const rawName = btn.dataset.raw;
        const detName = btn.dataset.det;
        const label = rawName || detName;
        if (!confirm(`Удалить запись «${label}»?`)) return;
        try {
          const deletes = [];
          if (rawName) deletes.push(fetchJSON(`${origin(ports.cap)}/recordings/${encodeURIComponent(rawName)}`, { method: 'DELETE' }).catch(() => {}));
          if (detName) deletes.push(fetchJSON(`${origin(ports.cap)}/recordings/${encodeURIComponent(detName)}`, { method: 'DELETE' }).catch(() => {}));
          await Promise.all(deletes);
          loadRecordings();
        } catch (err) {
          showError('Ошибка удаления: ' + err.message);
        }
      });
    });
  }

  async function startRecording() {
    try {
      if (els.recStartBtn) els.recStartBtn.disabled = true;
      // Generate shared ID so both files have matching timestamps
      const now = new Date();
      const recId = now.getFullYear().toString()
        + String(now.getMonth() + 1).padStart(2, '0')
        + String(now.getDate()).padStart(2, '0') + '_'
        + String(now.getHours()).padStart(2, '0')
        + String(now.getMinutes()).padStart(2, '0')
        + String(now.getSeconds()).padStart(2, '0');
      const body = JSON.stringify({ id: recId });
      const hdrs = { 'Content-Type': 'application/json' };
      // Start both raw and annotated recording in parallel with same ID
      const [capData] = await Promise.all([
        fetchJSON(`${origin(ports.cap)}/recording/start`, { method: 'POST', headers: hdrs, body }),
        fetchJSON(`${origin(ports.det)}/recording/start`, { method: 'POST', headers: hdrs, body }).catch(() => {}),
      ]);
      state.recording = true;
      state.recordingFile = capData.path
        ? capData.path.replace(/\\/g, '/').split('/').pop()
        : null;
      updateRecUI();
      loadRecordings();
      showToast('Запись начата', 'rec');
    } catch (err) {
      showError('Ошибка записи: ' + err.message);
    } finally {
      if (els.recStartBtn) els.recStartBtn.disabled = false;
    }
  }

  async function stopRecording() {
    try {
      if (els.recStopBtn) els.recStopBtn.disabled = true;
      // Stop both raw and annotated recording in parallel
      const [capData] = await Promise.all([
        fetchJSON(`${origin(ports.cap)}/recording/stop`, { method: 'POST' }),
        fetchJSON(`${origin(ports.det)}/recording/stop`, { method: 'POST' }).catch(() => {}),
      ]);
      state.recording = false;
      state.recordingFile = null;
      updateRecUI();
      loadRecordings();
      const name = capData.path ? capData.path.split('/').pop() : '';
      showToast(`Запись сохранена${name ? ': ' + name : ''}`, 'stop');
    } catch (err) {
      showError('Ошибка остановки: ' + err.message);
    } finally {
      if (els.recStopBtn) els.recStopBtn.disabled = false;
    }
  }

  function updateRecUI() {
    if (els.recDot) els.recDot.classList.toggle('active', state.recording);
    if (els.recHudIndicator) els.recHudIndicator.classList.toggle('active', state.recording);
    if (els.recStatus) els.recStatus.textContent = state.recording ? 'ЗАПИСЬ' : 'СТОП';
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
      els.sessList.innerHTML = `<div style="color:#ff7f8a;padding:8px">Ошибка: ${err.message}</div>`;
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
      els.sessList.innerHTML = '<div style="color:#64748b;text-align:center;padding:12px">Нет сессий</div>';
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
          ${s.active ? '<span class="active-badge">Активна</span>' : ''}
          <span class="sess-date">${date}</span>
        </div>
        <div class="sess-meta">
          <span>Детекции: ${s.detections}</span>
          <span>Треки: ${s.tracks}</span>
          <span>GIF: ${s.gifs}</span>
          <span>${size}</span>
        </div>
        ${s.classes && s.classes.length ? `<div class="sess-classes">${s.classes.join(', ')}</div>` : ''}
        <div class="sess-actions">
          <button class="sm sess-view-btn" data-sid="${s.session_id}">Открыть</button>
          ${s.active ? '' : `<button class="sm danger sess-del-btn" data-sid="${s.session_id}">Удалить</button>`}
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
        if (!confirm(`Удалить сессию «${sid}»?`)) return;
        try {
          await fetchJSON(`${origin(ports.det)}/sessions/${sid}`, { method: 'DELETE' });
          loadSessions();
        } catch (err) {
          showError('Ошибка удаления: ' + err.message);
        }
      });
    });
  }

  async function viewSession(sessionId) {
    try {
      const data = await fetchJSON(`${origin(ports.det)}/detections`);
      const dets = Array.isArray(data.detections) ? data.detections : [];
      if (!dets.length) {
        showError('Нет детекций для отображения');
        return;
      }
      // Group by track_id, show latest per track
      const byTrack = new Map();
      dets.forEach((d) => { if (d.track_id >= 0) byTrack.set(d.track_id, d); });
      if (!byTrack.size) {
        showError('Нет отслеживаемых детекций');
        return;
      }
      // Open first track in modal
      const first = byTrack.values().next().value;
      openDetectionModal(first);
    } catch (err) {
      showError('Ошибка: ' + err.message);
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
      els.modalImage.alt = `Трек ${det.track_id}`;
    }
    if (els.modalDl) {
      els.modalDl.innerHTML = '';
      const pairs = [
        ['Трек ID', det.track_id],
        ['Класс', det.class_name],
        ['Уверенность', ((det.confidence || 0) * 100).toFixed(2) + '%'],
        ['Кадр', det.frame_number],
        ['Время', det.timestamp],
        ['Первое появл.', det.first_seen || '—'],
      ];
      if (det.bbox) {
        pairs.push(['Область', `x:${det.bbox.x} y:${det.bbox.y} ${det.bbox.w}×${det.bbox.h}`]);
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
        if (target === 'sessions') loadSessions();
        if (target === 'recordings') loadRecordings();
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
        updateStatusIndicator('Ошибка потока', 'error');
        showError('Не удалось подключиться к видеопотоку');
        setTimeout(startStream, 3000);
      };
    }

    // Detection toggle
    if (els.detToggle) els.detToggle.addEventListener('change', toggleDetection);

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

    // AWB controls
    if (els.awbModeSelect) els.awbModeSelect.addEventListener('change', updateAwbGainsVisibility);
    if (els.awbRedSlider) els.awbRedSlider.addEventListener('input', () => {
      setText('awb-red-val', (parseInt(els.awbRedSlider.value, 10) / 100).toFixed(2));
    });
    if (els.awbBlueSlider) els.awbBlueSlider.addEventListener('input', () => {
      setText('awb-blue-val', (parseInt(els.awbBlueSlider.value, 10) / 100).toFixed(2));
    });
    if (els.applyAwbBtn) els.applyAwbBtn.addEventListener('click', applyAwb);

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
    if (els.dbRefreshBtn) els.dbRefreshBtn.addEventListener('click', loadSessions);
    if (els.dbSort) els.dbSort.addEventListener('change', renderSessions);
    if (els.sessClassFilter) els.sessClassFilter.addEventListener('input', renderSessions);

    // Modal
    if (els.modalClose) els.modalClose.addEventListener('click', closeModal);
    if (els.modalBackdrop) els.modalBackdrop.addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeModal(); closeLogPopup(); } });

    // Log popup
    if (els.logToggle) els.logToggle.addEventListener('click', toggleLogPopup);
    if (els.logPopupClose) els.logPopupClose.addEventListener('click', closeLogPopup);
  }

  // ---------------------------------------------------------------------------
  // Toasts
  // ---------------------------------------------------------------------------
  const toastContainer = $('toast-container');

  /**
   * Show a toast notification.
   * @param {string} text  — message
   * @param {'rec'|'stop'|'detect'|'save'} type — visual style
   * @param {number} duration — ms before auto-dismiss (default 3000)
   */
  function showToast(text, type = 'detect', duration = 3000) {
    if (!toastContainer) return;
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.innerHTML = `<span class="toast-icon"></span><span class="toast-text">${text}</span>`;
    toastContainer.appendChild(el);
    // Keep max 5 toasts
    while (toastContainer.children.length > 5) {
      toastContainer.removeChild(toastContainer.firstChild);
    }
    setTimeout(() => {
      el.classList.add('out');
      setTimeout(() => el.remove(), 300);
    }, duration);
  }

  // ---------------------------------------------------------------------------
  // Log popup
  // ---------------------------------------------------------------------------
  function toggleLogPopup() {
    if (els.logPopup) els.logPopup.classList.toggle('hidden');
  }
  function closeLogPopup() {
    if (els.logPopup) els.logPopup.classList.add('hidden');
  }

  // ---------------------------------------------------------------------------
  // Start
  // ---------------------------------------------------------------------------
  init();
})();
