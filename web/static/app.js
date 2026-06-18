/* gpt_signup_hybrid — frontend logic */
(() => {
  'use strict';

  // ── Auth ────────────────────────────────────────────────────────────
  const _LS_TOKEN = 'gpt_reg.auth_token';

  function getAuthToken() {
    // 1. Meta tag (injected server-side khi loopback bind)
    const meta = document.querySelector('meta[name="auth-token"]');
    const metaVal = (meta && meta.content) || '';
    if (metaVal) return metaVal;
    // 2. URL query param ?token=... (cho non-loopback access)
    const params = new URLSearchParams(window.location.search);
    const urlToken = params.get('token') || '';
    if (urlToken) {
      localStorage.setItem(_LS_TOKEN, urlToken);
      return urlToken;
    }
    // 3. localStorage (previously entered)
    return localStorage.getItem(_LS_TOKEN) || '';
  }
  function withTokenQuery(url) {
    const t = getAuthToken();
    if (!t) return url;
    const sep = url.includes('?') ? '&' : '?';
    return url + sep + 'token=' + encodeURIComponent(t);
  }

  // ── SseBus — unified SSE multiplexer (frontend) ─────────────────
  const SseBus = (() => {
    let _es = null;
    let _reconnectTimer = null;
    const _handlers = new Map(); // channel -> [callback, ...]

    function connect() {
      if (_es && _es.readyState !== 2) return;
      _disconnect();
      const url = withTokenQuery('/api/sse');
      _es = new EventSource(url);

      _es.onmessage = (e) => {
        let data;
        try { data = JSON.parse(e.data); } catch (_) { return; }
        const channel = data.channel;
        if (!channel) return;
        const cbs = _handlers.get(channel);
        if (cbs) cbs.forEach(cb => cb(data));
      };

      _es.onerror = () => {
        _disconnect();
        _reconnectTimer = setTimeout(connect, 3000);
      };
    }

    function on(channel, callback) {
      if (!_handlers.has(channel)) _handlers.set(channel, []);
      _handlers.get(channel).push(callback);
    }

    function _disconnect() {
      if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
      if (_es) { try { _es.close(); } catch (_) {} _es = null; }
    }

    return { connect, on };
  })();
  window.SseBus = SseBus;

  // ── LocalStorage keys ─────────────────────────────────────────────
  // NOTE: Chỉ textarea drafts giữ ở localStorage (ngoài scope unified-settings-store).
  // Tất cả runtime config đã migrate sang Settings store (DB-backed).
  const LS_INPUT_REG = 'gpt_reg.input.reg';

  // Helper: persist textarea content vào localStorage. Lưu cả khi rỗng để
  // phân biệt "user đã xoá tay" vs "chưa từng nhập" — chỉ xoá key khi
  // user bấm Clear Input.
  function persistTextarea(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* quota — bỏ qua */ }
  }
  function clearPersistedTextarea(key) {
    try { localStorage.removeItem(key); } catch (e) { /* ignore */ }
  }
  // Expose để các tab khác (session.js, link.js) dùng chung pattern
  window.GptUi = Object.assign(window.GptUi || {}, {
    persistTextarea,
    clearPersistedTextarea,
  });

  // ── Error alert sound (Web Audio API — works in background tabs) ──
  let _audioCtx = null;
  function _getAudioCtx() {
    if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (_audioCtx.state === 'suspended') _audioCtx.resume();
    return _audioCtx;
  }
  function playErrorAlert() {
    try {
      const ctx = _getAudioCtx();
      const now = ctx.currentTime;
      // 3 beeps: 880Hz, loud, short — unmissable
      for (let i = 0; i < 3; i++) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = 'square';
        osc.frequency.value = 880;
        gain.gain.value = 0.5;
        const t = now + i * 0.25;
        osc.start(t);
        osc.stop(t + 0.15);
      }
    } catch (e) { /* AudioContext not available */ }
  }
  // Unlock AudioContext on first user interaction (required by browsers)
  function _unlockAudio() {
    try { _getAudioCtx(); } catch (e) { }
    document.removeEventListener('click', _unlockAudio);
    document.removeEventListener('keydown', _unlockAudio);
  }
  document.addEventListener('click', _unlockAudio);
  document.addEventListener('keydown', _unlockAudio);

  // Expose for session.js and link.js
  window.GptUi = Object.assign(window.GptUi || {}, { playErrorAlert });

  // ── State ─────────────────────────────────────────────────────────
  const state = {
    jobs: new Map(),          // id → job dict
    order: [],                // job id order
    activeJobId: null,        // job đang xem log
    maxConcurrent: 3,
    mode: 'multi',
    headless: true,
    debug: false,
    mailModes: [],            // [{id, label, input_placeholder, input_help, config_schema}]
    currentMailMode: 'outlook',
  };

  // ── DOM refs ──────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const dom = {
    comboInput: $('combo-input'),
    btnRun: $('btn-run'),
    btnStopAll: $('btn-stop-all'),
    btnClearInput: $('btn-clear-input'),
    comboCount: $('combo-count'),
    defaultPassword: $('default-password'),
    jobTimeout: $('job-timeout'),
    autoRetryMax: $('auto-retry-max'),
    jobList: $('job-list'),
    jobSummary: $('job-summary'),
    logPane: $('log-pane'),
    logTarget: $('log-target'),
    successPane: $('success-pane'),
    errorPane: $('error-pane'),
    btnCopySuccess: $('btn-copy-success'),
    btnCopyError: $('btn-copy-error'),
    statusPill: $('status-pill'),
    modeSelect: $('mode'),
    headlessToggle: $('headless-toggle'),
    debugToggle: $('debug-toggle'),
    inputHint: $('input-hint'),
    mailModeSelect: $('mail-mode-select'),
    regModeSelect: $('reg-mode-select'),
    mailModeConfigHost: $('mail-mode-config-host'),
  };

  // ── Helpers ───────────────────────────────────────────────────────
  const icons = Object.freeze({
    stop: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',
    retry: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
    remove: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.07 0l2.83-2.83a5 5 0 0 0-7.07-7.07L11 4"/><path d="M14 11a5 5 0 0 0-7.07 0L4.1 13.83a5 5 0 1 0 7.07 7.07L13 19"/></svg>',
    token: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 2l-2 2"/><path d="M7.61 13.39a5.5 5.5 0 1 0 7.78 7.78L21 15.5l-7.5-7.5-5.89 5.39Z"/><path d="m14.5 6.5 3 3"/></svg>',
    eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>',
    eyeOff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3 3 18 18"/><path d="M10.6 10.6A2 2 0 0 0 13.4 13.4"/><path d="M9.9 4.2A10.6 10.6 0 0 1 12 4c6.5 0 10 8 10 8a18.7 18.7 0 0 1-3.1 4.3"/><path d="M6.1 6.1C3.4 8 2 12 2 12s3.5 8 10 8a10.7 10.7 0 0 0 5.9-1.8"/></svg>',
    qr: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><line x1="14" y1="14" x2="14" y2="17"/><line x1="14" y1="20" x2="14" y2="21"/><line x1="17" y1="14" x2="21" y2="14"/><line x1="17" y1="17" x2="17" y2="21"/><line x1="20" y1="17" x2="21" y2="17"/><line x1="20" y1="20" x2="21" y2="20"/></svg>',
  });
  const mailModeUiCopy = Object.freeze({
    outlook: {
      input_help: 'One Outlook combo per line.',
      input_placeholder: 'email|password|refresh_token|client_id',
    },
    worker: {
      input_help: 'One iCloud email per line via Worker OTP.',
      input_placeholder: 'user@icloud.com',
    },
    gmail_advanced: {
      input_help: 'Mỗi dòng: api_url hoặc email|api_url. Pre-check mail_status=live.',
      input_placeholder: 'https://checkgmail.live/otp/...\nbrandonspencer7424@gmail.com|https://checkgmail.live/otp/...',
    },
  });

  function fmtDuration(secs) {
    if (secs == null) return '';
    if (secs < 60) return secs.toFixed(1) + 's';
    return Math.floor(secs / 60) + 'm' + Math.floor(secs % 60) + 's';
  }

  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function api(path, opts = {}) {
    const token = getAuthToken();
    const headers = {
      'Content-Type': 'application/json',
      ...(token ? { 'X-API-Token': token } : {}),
      ...(opts.headers || {}),
    };
    return fetch(path, {
      ...opts,
      headers,
    }).then((r) => {
      if (!r.ok) return r.text().then((t) => { throw new Error(`HTTP ${r.status}: ${t}`); });
      return r.json();
    });
  }

  function icon(name) {
    return icons[name] || '';
  }

  function copyText(text) {
    // Fallback cho non-HTTPS / mobile browsers
    function fallbackCopy(str) {
      const ta = document.createElement('textarea');
      ta.value = str;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (_) { /* ignore */ }
      document.body.removeChild(ta);
      return ok;
    }

    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(() => {
        if (fallbackCopy(text)) return;
        return Dialog.alert({ message: 'Copy failed.' }).then(() => { throw new Error('copy failed'); });
      });
    }
    // Non-secure context: dùng fallback trực tiếp
    if (fallbackCopy(text)) return Promise.resolve();
    return Dialog.alert({ message: 'Copy failed.' }).then(() => { throw new Error('copy failed'); });
  }

  let _activeTabId = null;
  function activateTab(tabId) {
    const prevTab = _activeTabId;
    _activeTabId = tabId;
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.tab === tabId);
    });
    document.querySelectorAll('.tab-content').forEach((tab) => {
      tab.classList.toggle('active', tab.id === `tab-${tabId}`);
    });
    Settings.save('ui.active_tab', tabId, getAuthToken());
    document.dispatchEvent(new CustomEvent('gpt:tab', { detail: { tab: tabId, prev: prevTab } }));
  }

  function initTabs() {
    if (document.body.dataset.tabsBound === 'true') return;
    document.body.dataset.tabsBound = 'true';
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => activateTab(btn.dataset.tab));
    });
    // Tab tạm ẩn (chưa dùng được). Mở lại: bỏ khỏi danh sách + bỏ comment nút nav trong index.html.
    const hiddenTabs = ['link', 'hme'];
    let initialTab = Settings.get('ui.active_tab') || document.querySelector('.tab-btn.active')?.dataset.tab || 'reg';
    if (hiddenTabs.includes(initialTab)) initialTab = 'reg';
    activateTab(initialTab);
  }

  window.GptUi = Object.assign(window.GptUi || {}, {
    icon,
    copyText,
    activateTab,
    initTabs,
    getAuthToken,
  });

  // ── Combo counter ─────────────────────────────────────────────────
  function updateComboCount() {
    const lines = dom.comboInput.value.split('\n').filter((l) => {
      const s = l.trim();
      return s && !s.startsWith('#');
    });
    dom.comboCount.textContent = `${lines.length} combo${lines.length === 1 ? '' : 's'}`;
  }

  dom.comboInput.addEventListener('input', () => {
    updateComboCount();
    persistTextarea(LS_INPUT_REG, dom.comboInput.value);
  });

  // ── Render job list ──────────────────────────────────────────────
  function renderJobs() {
    if (state.order.length === 0) {
      dom.jobList.innerHTML = '<div class="empty">No jobs yet. Paste combos and click Run.</div>';
      dom.jobSummary.textContent = '0 total';
      return;
    }

    const stats = { queued: 0, running: 0, success: 0, error: 0, cancelled: 0 };
    const html = state.order.map((id, idx) => {
      const j = state.jobs.get(id);
      if (!j) return '';
      stats[j.status] = (stats[j.status] || 0) + 1;
      const cls = state.activeJobId === id ? 'job is-active' : 'job';
      const actionBtn = j.status === 'running'
        ? `<button class="icon-btn icon-danger" data-action="stop" data-id="${escHtml(id)}" title="Stop">${icon('stop')}</button>`
        : `<button class="icon-btn" data-action="retry" data-id="${escHtml(id)}" title="Retry">${icon('retry')}</button>`;
      return `
        <div class="${cls}" data-id="${escHtml(id)}">
          <div class="job-index">${idx + 1}</div>
          <div class="job-status status-${escHtml(j.status)}">${escHtml(j.status)}</div>
          <div class="job-main">
            <div class="job-email" title="${escHtml(j.email)}">${escHtml(j.email)}<span class="badge-mode badge-mode-${escHtml(j.mail_mode || 'outlook')}">${escHtml(j.mail_mode || 'outlook')}</span></div>
          </div>
          <div class="job-duration">${escHtml(fmtDuration(j.duration))}</div>
          <div class="job-actions">
            ${actionBtn}
            <button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Remove">${icon('remove')}</button>
          </div>
        </div>
      `;
    }).join('');

    dom.jobList.innerHTML = html;
    dom.jobSummary.textContent = [
      `${state.order.length} total`,
      stats.running ? `${stats.running} running` : '',
      stats.queued ? `${stats.queued} queued` : '',
      stats.success ? `${stats.success} done` : '',
      stats.error ? `${stats.error} failed` : '',
    ].filter(Boolean).join(' · ');

    updateStatusPill(stats);
  }

  function updateStatusPill(stats) {
    if (stats.running > 0) {
      dom.statusPill.className = 'pill pill-running';
      dom.statusPill.textContent = `running ${stats.running}/${state.maxConcurrent}`;
    } else if (stats.queued > 0) {
      dom.statusPill.className = 'pill pill-running';
      dom.statusPill.textContent = `queued ${stats.queued}`;
    } else if (stats.error > 0 && stats.success === 0) {
      dom.statusPill.className = 'pill pill-error';
      dom.statusPill.textContent = 'error';
    } else if (stats.success > 0) {
      dom.statusPill.className = 'pill pill-success';
      dom.statusPill.textContent = `done ${stats.success}`;
    } else {
      dom.statusPill.className = 'pill pill-idle';
      dom.statusPill.textContent = 'idle';
    }
  }

  // ── Render success/error output ──────────────────────────────────
  // Secrets không còn nằm trong job snapshot — fetch riêng qua /api/jobs/secrets.
  // Cache local để tránh round-trip mỗi render; refresh khi snapshot/SSE-job update.
  const secretsCache = new Map(); // job_id → {password, secret, first_code, session_path}
  let _secretsRefreshScheduled = false;

  async function refreshSecrets() {
    try {
      const data = await api('/api/jobs/secrets');
      secretsCache.clear();
      const map = data.secrets || {};
      for (const id of Object.keys(map)) {
        secretsCache.set(id, map[id] || {});
      }
      renderOutputs();
    } catch (err) {
      console.warn('refreshSecrets failed', err.message);
    }
  }

  function scheduleSecretsRefresh() {
    if (_secretsRefreshScheduled) return;
    _secretsRefreshScheduled = true;
    // Coalesce nhiều SSE update gần nhau — fetch 1 lần sau 250ms
    setTimeout(() => {
      _secretsRefreshScheduled = false;
      refreshSecrets();
    }, 250);
  }

  function renderOutputs() {
    const successLines = [];
    const errorLines = [];
    for (const id of state.order) {
      const j = state.jobs.get(id);
      if (!j) continue;
      const sec = secretsCache.get(id) || {};
      const password = sec.password || '';
      const secret = sec.secret || '';
      if (j.status === 'success' && secret) {
        successLines.push(`${j.email}|${password}|${secret}`);
      } else if (j.status === 'error') {
        // Signup OK nhưng 2FA fail (job.has_password=true, has_secret=false) → vẫn xuất
        if (password) {
          successLines.push(`${j.email}|${password}|no_2fa`);
        }
        errorLines.push(`${j.email}  →  ${j.error || 'unknown'}`);
      }
    }
    dom.successPane.textContent = successLines.length
      ? successLines.join('\n')
      : 'Format: email|password|secret_2fa';
    dom.errorPane.textContent = errorLines.length
      ? errorLines.join('\n')
      : 'No errors yet.';
  }

  // ── Render log của 1 job ─────────────────────────────────────────
  function renderLog(jobId) {
    if (!jobId) {
      dom.logPane.textContent = '';
      dom.logTarget.textContent = '-';
      return;
    }
    const j = state.jobs.get(jobId);
    if (!j) return;
    dom.logTarget.textContent = j.email;
    api(`/api/jobs/${jobId}/log`).then((data) => {
      const lines = data.log || [];
      // Mỗi span tự kết thúc bằng '\n' (giống applyLog) để SSE append sau
      // không bị dính vào span cuối.
      dom.logPane.innerHTML = lines.map((l) => {
        const cls = /(error|FAILED|fatal)/i.test(l)
          ? 'log-line-error'
          : 'log-line-info';
        return `<span class="${cls}">${escHtml(l)}\n</span>`;
      }).join('');
      dom.logPane.scrollTop = dom.logPane.scrollHeight;
    }).catch((err) => {
      dom.logPane.textContent = `[error] ${err.message}`;
    });
  }

  // ── Job actions ──────────────────────────────────────────────────
  dom.jobList.addEventListener('click', async (e) => {
    const target = e.target;
    const actionBtn = target.closest('[data-action]');
    if (actionBtn) {
      const action = actionBtn.dataset.action;
      const id = actionBtn.dataset.id;
      e.stopPropagation();

      if (action === 'retry') {
        if (!(await Dialog.confirm({ message: 'Retry this job?' }))) return;
        api(`/api/jobs/${id}/retry`, { method: 'POST' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'stop') {
        if (!(await Dialog.confirm({ message: 'Stop this running job?' }))) return;
        api(`/api/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'remove') {
        if (!(await Dialog.confirm({ message: 'Remove this job from the list and textarea?' }))) return;
        const j = state.jobs.get(id);
        if (j) removeFromTextarea(j.email);
        api(`/api/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      }
      return;
    }
    const row = target.closest('.job');
    if (row) {
      state.activeJobId = row.dataset.id;
      renderJobs();
      renderLog(state.activeJobId);
    }
  });

  function removeFromTextarea(email) {
    const lines = dom.comboInput.value.split('\n');
    const filtered = lines.filter((l) => {
      const m = l.trim().split('|')[0];
      return m.toLowerCase() !== email.toLowerCase();
    });
    dom.comboInput.value = filtered.join('\n');
    updateComboCount();
    persistTextarea(LS_INPUT_REG, dom.comboInput.value);
  }

  // ── Mode → concurrency mapping ────────────────────────────────────
  function _modeToConcurrency(mode) {
    const map = { single: 1, multi: 2, multi3: 3, multi5: 5, multi10: 10, multi20: 20, multi30: 30, multi50: 50 };
    return map[mode] || 1;
  }

  // ── Run button ───────────────────────────────────────────────────
  dom.btnRun.addEventListener('click', async () => {
    const combos = dom.comboInput.value.trim();
    if (!combos) {
      await Dialog.alert({ message: 'Paste combos first.' });
      return;
    }
    dom.btnRun.disabled = true;
    try {
      // Luôn sync config server trước khi chạy
      const target = _modeToConcurrency(state.mode);
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ max_concurrent: target }),
      });
      state.maxConcurrent = target;

      // Build payload theo mail mode
      const payload = {
        combos,
        default_password: dom.defaultPassword.value.trim() || null,
        mail_mode: state.currentMailMode,
        reg_mode: dom.regModeSelect.value || 'browser',
      };
      if (state.currentMailMode === 'worker') {
        // Đọc trực tiếp từ DOM input (không chỉ localStorage — user có thể chưa trigger persist)
        const urlInp = dom.mailModeConfigHost.querySelector('input[data-config-key="logs_url"]');
        const keyInp = dom.mailModeConfigHost.querySelector('input[data-config-key="api_key"]');
        payload.email_logs_url = (urlInp && urlInp.value.trim()) || '';
        payload.email_api_key = (keyInp && keyInp.value.trim()) || '';
      }

      await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    } finally {
      dom.btnRun.disabled = false;
      validateWorkerConfig();
    }
  });

  dom.btnClearInput.addEventListener('click', () => {
    dom.comboInput.value = '';
    updateComboCount();
    clearPersistedTextarea(LS_INPUT_REG);
  });

  dom.btnStopAll.addEventListener('click', async () => {
    if (!(await Dialog.confirm({ message: 'Stop all running or queued jobs?' }))) return;
    try {
      const res = await api('/api/jobs/stop-all', { method: 'POST' });
      console.log('stopped:', res.stopped);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  document.getElementById('btn-clear-done').addEventListener('click', async () => {
    try {
      const res = await api('/api/jobs/clear-finished', { method: 'POST' });
      // Refresh list (SSE sẽ broadcast clear_finished event)
      console.log('cleared:', res.removed);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  document.getElementById('btn-clear-all').addEventListener('click', async () => {
    if (!(await Dialog.confirm({ message: 'Xoá TẤT CẢ jobs (mọi trạng thái)? Hành động không thể hoàn tác.', danger: true, confirmLabel: 'Xoá' }))) return;
    try {
      const res = await api('/api/jobs/clear-all', { method: 'POST' });
      console.log('clear-all:', res.removed);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  document.getElementById('btn-retry-failed').addEventListener('click', async () => {
    if (!(await Dialog.confirm({ message: 'Retry tất cả jobs error & cancelled?' }))) return;
    try {
      const res = await api('/api/jobs/retry-failed', { method: 'POST' });
      console.log('retry-failed:', res.retried);
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    }
  });

  dom.modeSelect.addEventListener('change', async () => {
    state.mode = dom.modeSelect.value;
    const target = _modeToConcurrency(state.mode);
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ max_concurrent: target }),
      });
      state.maxConcurrent = target;
    } catch (err) {
      console.error(err);
    }
    // Persist reg.mode to Settings store (R6.1 — no dedicated endpoint for mode)
    Settings.save('reg.mode', state.mode, getAuthToken());
  });

  dom.headlessToggle.addEventListener('change', async () => {
    const headless = dom.headlessToggle.checked;
    // Cảnh báo: jobs đang RUNNING không bị ảnh hưởng (browser đã launch)
    let runningCount = 0;
    for (const [, j] of state.jobs) {
      if (j.status === 'running') runningCount += 1;
    }
    if (runningCount > 0) {
      const ok = await Dialog.confirm({ message:
        `Có ${runningCount} job đang RUNNING — đổi Headless không ` +
        `áp dụng cho job đó (browser đã launch). Chỉ ảnh hưởng job mới.\n\n` +
        `Tiếp tục đổi sang ${headless ? 'Headless' : 'Headed'}?`
      });
      if (!ok) {
        dom.headlessToggle.checked = state.headless;
        return;
      }
    }
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ headless }),
      });
      state.headless = headless;
    } catch (err) {
      console.error(err);
      dom.headlessToggle.checked = state.headless;
    }
  });

  dom.debugToggle.addEventListener('change', async () => {
    const debug = dom.debugToggle.checked;
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ debug }),
      });
      state.debug = debug;
    } catch (err) {
      console.error(err);
      dom.debugToggle.checked = state.debug;
    }
  });

  dom.jobTimeout.addEventListener('change', async () => {
    const val = parseInt(dom.jobTimeout.value, 10);
    if (isNaN(val) || val < 30 || val > 600) return;
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ job_timeout: val }),
      });
    } catch (err) {
      console.error(err);
    }
  });

  dom.autoRetryMax.addEventListener('change', async () => {
    const val = parseInt(dom.autoRetryMax.value, 10);
    if (isNaN(val) || val < 0 || val > 10) return;
    const enabled = val > 0;
    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ auto_retry: enabled, auto_retry_max: val || 1 }),
      });
    } catch (err) {
      console.error(err);
    }
  });

  // Password field persist — write-through via Settings API (ui-only, no dedicated endpoint)
  dom.defaultPassword.addEventListener('input', () => {
    const token = getAuthToken();
    if (token) Settings.save('reg.default_password', dom.defaultPassword.value || null, token);
  });

  // ── Copy buttons ─────────────────────────────────────────────────
  dom.btnCopySuccess.addEventListener('click', () => copyText(dom.successPane.textContent));
  dom.btnCopyError.addEventListener('click', () => copyText(dom.errorPane.textContent));

  // ── SSE event stream ─────────────────────────────────────────────
  function applySnapshot(jobs) {
    state.order = jobs.map((j) => j.id);
    state.jobs.clear();
    for (const j of jobs) state.jobs.set(j.id, j);
    // Prune secretsCache theo job set hiện tại
    for (const cachedId of Array.from(secretsCache.keys())) {
      if (!state.jobs.has(cachedId)) secretsCache.delete(cachedId);
    }
    renderJobs();
    renderOutputs();
    scheduleSecretsRefresh();
  }

  function applyJobUpdate(j) {
    const prev = state.jobs.get(j.id);
    if (!prev) {
      state.order.push(j.id);
    }
    state.jobs.set(j.id, j);
    renderJobs();
    renderOutputs();
    // Khi job chuyển success/error → có thể có secrets mới → fetch lại
    if (j.status === 'success' || j.status === 'error') {
      scheduleSecretsRefresh();
    }
    if (j.status === 'error' && (!prev || prev.status !== 'error')) {
      playErrorAlert();
    }
    if (state.activeJobId === j.id) {
      // refresh log nếu đang xem
      renderLog(j.id);
    }
  }

  function applyRemove(jobId) {
    state.jobs.delete(jobId);
    state.order = state.order.filter((id) => id !== jobId);
    secretsCache.delete(jobId);
    if (state.activeJobId === jobId) {
      state.activeJobId = null;
      renderLog(null);
    }
    renderJobs();
    renderOutputs();
  }

  function applyLog(jobId, line) {
    if (state.activeJobId !== jobId) return;
    const cls = /(error|FAILED|fatal)/i.test(line) ? 'log-line-error' : 'log-line-info';
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = line + '\n';
    dom.logPane.appendChild(span);
    dom.logPane.scrollTop = dom.logPane.scrollHeight;
  }

  // ── SseBus handler for 'reg' channel ───────────────────────────────
  SseBus.on('reg', (data) => {
    if (data.type === 'snapshot') {
      state.maxConcurrent = data.max_concurrent;
      if (typeof data.headless === 'boolean') {
        state.headless = data.headless;
        dom.headlessToggle.checked = data.headless;
      }
      if (typeof data.debug === 'boolean') {
        state.debug = data.debug;
        dom.debugToggle.checked = data.debug;
      }
      if (data.job_timeout) {
        dom.jobTimeout.value = data.job_timeout;
      }
      applySnapshot(data.jobs);
    } else if (data.type === 'job') {
      applyJobUpdate(data.job);
    } else if (data.type === 'remove') {
      applyRemove(data.job_id);
    } else if (data.type === 'clear_finished') {
      api('/api/jobs').then((r) => applySnapshot(r.jobs)).catch(console.error);
    } else if (data.type === 'clear_all') {
      state.jobs.clear();
      state.order = [];
      secretsCache.clear();
      state.activeJobId = null;
      renderJobs();
      renderOutputs();
      renderLog(null);
    } else if (data.type === 'log') {
      applyLog(data.job_id, data.line);
    }
  });

  // ── Mail Mode ─────────────────────────────────────────────────────
  let _workerConfigDebounce = null;

  function getWorkerConfig() {
    // Hydrate from Settings store (mail_mode.worker_config is a JSON object)
    const cfg = Settings.get('mail_mode.worker_config');
    return (cfg && typeof cfg === 'object') ? cfg : {};
  }

  function saveWorkerConfig(cfg) {
    Settings.save('mail_mode.worker_config', cfg, getAuthToken());
  }

  function renderMailModeSelector(modes) {
    dom.mailModeSelect.innerHTML = modes.map(m =>
      `<option value="${escHtml(m.id)}">${escHtml(m.label)}</option>`
    ).join('');
  }

  function renderMailModeConfig(modes, modeId) {
    const spec = modes.find(m => m.id === modeId);
    if (!spec || spec.config_schema.length === 0) {
      dom.mailModeConfigHost.innerHTML = '';
      return;
    }
    const saved = getWorkerConfig();
    // Ensure defaults are persisted immediately
    let needSave = false;
    for (const f of spec.config_schema) {
      if (saved[f.key] === undefined) {
        saved[f.key] = f.default;
        needSave = true;
      }
    }
    if (needSave) saveWorkerConfig(saved);
    const fields = spec.config_schema.map(f => {
      const val = saved[f.key] !== undefined ? saved[f.key] : f.default;
      const widthClass = f.key === 'api_key' ? 'config-field-short' : 'config-field-long';
      return `
        <label class="input-group ${widthClass}">
          <span class="input-label">${escHtml(f.label)}${f.required ? ' *' : ''}</span>
          <input type="text" data-config-key="${escHtml(f.key)}" value="${escHtml(val)}" spellcheck="false" autocomplete="off" />
          <span class="input-error" id="err-${escHtml(f.key)}"></span>
        </label>
      `;
    }).join('');
    // Sử dụng display:contents wrapper — elements trực tiếp nằm trong flex row
    dom.mailModeConfigHost.innerHTML = `<div class="mail-mode-config-panel">${fields}</div>`;
    // Attach events
    dom.mailModeConfigHost.querySelectorAll('input[data-config-key]').forEach(inp => {
      inp.addEventListener('input', () => debouncePersistWorkerConfig());
      inp.addEventListener('blur', () => debouncePersistWorkerConfig());
    });
    validateWorkerConfig();
  }

  function debouncePersistWorkerConfig() {
    clearTimeout(_workerConfigDebounce);
    _workerConfigDebounce = setTimeout(() => {
      const cfg = {};
      dom.mailModeConfigHost.querySelectorAll('input[data-config-key]').forEach(inp => {
        cfg[inp.dataset.configKey] = inp.value;
      });
      saveWorkerConfig(cfg);
      validateWorkerConfig();
    }, 500);
  }

  function validateWorkerConfig() {
    if (state.currentMailMode !== 'worker') {
      dom.btnRun.disabled = false;
      return;
    }
    const spec = state.mailModes.find(m => m.id === 'worker');
    if (!spec) return;
    let valid = true;
    for (const f of spec.config_schema) {
      const inp = dom.mailModeConfigHost.querySelector(`input[data-config-key="${f.key}"]`);
      const errEl = document.getElementById(`err-${f.key}`);
      if (!inp || !errEl) continue;
      const val = inp.value.trim();
      if (f.validate_prefix && f.validate_prefix.length) {
        if (!f.validate_prefix.some(p => val.startsWith(p))) {
          errEl.textContent = `Must start with ${f.validate_prefix.join(' or ')}`;
          errEl.className = 'input-error';
          valid = false;
          continue;
        }
      }
      if (f.required && !val) {
        errEl.textContent = 'Required';
        errEl.className = 'input-error';
        valid = false;
        continue;
      }
      if (!f.required && !val) {
        errEl.textContent = 'Blank - Worker sends no Authorization header';
        errEl.className = 'input-warn';
        continue;
      }
      errEl.textContent = '';
    }
    dom.btnRun.disabled = !valid;
  }

  function applyMailMode(modeId) {
    state.currentMailMode = modeId;
    dom.mailModeSelect.value = modeId;
    Settings.save('mail_mode.current', modeId, getAuthToken());
    const spec = state.mailModes.find(m => m.id === modeId);
    if (spec) {
      const uiCopy = mailModeUiCopy[modeId] || {};
      dom.comboInput.placeholder = uiCopy.input_placeholder || spec.input_placeholder;
      dom.inputHint.textContent = uiCopy.input_help || spec.input_help;
    }
    renderMailModeConfig(state.mailModes, modeId);
  }

  async function bootstrapMailModes() {
    try {
      const data = await api('/api/mail-modes');
      state.mailModes = data.modes || [];
    } catch (err) {
      console.error('Failed to load mail modes:', err);
      state.mailModes = [
        { id: 'outlook', label: 'Hotmail (combo)', input_placeholder: 'email|password|refresh_token|client_id', input_help: 'One Outlook combo per line.', config_schema: [] },
      ];
    }
    renderMailModeSelector(state.mailModes);
    // Restore from Settings store (DB-backed)
    const saved = Settings.get('mail_mode.current');
    const validIds = state.mailModes.map(m => m.id);
    const initial = (saved && validIds.includes(saved)) ? saved : 'outlook';
    applyMailMode(initial);
    // Listen change
    dom.mailModeSelect.addEventListener('change', () => {
      applyMailMode(dom.mailModeSelect.value);
    });

    // ── Reg Mode selector (browser / pure_request) ───────────────
    const savedRegMode = Settings.get('reg_mode.current');
    if (savedRegMode && ['browser', 'pure_request'].includes(savedRegMode)) {
      dom.regModeSelect.value = savedRegMode;
    }
    dom.regModeSelect.addEventListener('change', () => {
      Settings.save('reg_mode.current', dom.regModeSelect.value, getAuthToken());
    });
  }

  // ── Init ─────────────────────────────────────────────────────────
  // Settings hydration: load all settings from DB via Settings.bootstrap(token),
  // then hydrate UI controls. Server is source of truth (write-through from
  // POST /api/config ensures DB stays in sync).

  // Restore combo textarea — chỉ mất khi user bấm Clear Input
  const _savedReg = localStorage.getItem(LS_INPUT_REG);
  if (_savedReg) dom.comboInput.value = _savedReg;

  // Bootstrap: load settings from DB then hydrate UI
  (async () => {
    const token = getAuthToken();
    await Settings.bootstrap(token);

    // Hydrate state + UI controls từ Settings store (DB-backed)
    const mode = Settings.get('reg.mode');
    if (mode) state.mode = mode;
    dom.modeSelect.value = state.mode;

    const headless = Settings.get('reg.headless');
    if (typeof headless === 'boolean') state.headless = headless;
    dom.headlessToggle.checked = state.headless;

    const debug = Settings.get('reg.debug');
    if (typeof debug === 'boolean') state.debug = debug;
    dom.debugToggle.checked = state.debug;

    const defaultPassword = Settings.get('reg.default_password');
    if (defaultPassword) dom.defaultPassword.value = defaultPassword;

    const jobTimeout = Settings.get('reg.job_timeout');
    if (typeof jobTimeout === 'number') dom.jobTimeout.value = jobTimeout;

    const autoRetry = Settings.get('reg.auto_retry');
    const autoRetryMax = Settings.get('reg.auto_retry_max');
    if (typeof autoRetryMax === 'number') {
      dom.autoRetryMax.value = autoRetry ? autoRetryMax : 0;
    }

    // Server GET /api/config — source of truth cho runtime state (headless/debug/etc.)
    // Override từ DB nếu server đã apply khác (ví dụ manager changed in-memory).
    try {
      const cfg = await api('/api/config');
      if (typeof cfg.headless === 'boolean') {
        state.headless = cfg.headless;
        dom.headlessToggle.checked = cfg.headless;
      }
      if (typeof cfg.debug === 'boolean') {
        state.debug = cfg.debug;
        dom.debugToggle.checked = cfg.debug;
      }
      if (typeof cfg.job_timeout === 'number') {
        dom.jobTimeout.value = cfg.job_timeout;
      }
      if (typeof cfg.auto_retry_max === 'number') {
        dom.autoRetryMax.value = cfg.auto_retry ? cfg.auto_retry_max : 0;
      }
    } catch (err) {
      console.error('GET /api/config failed, dùng Settings DB fallback:', err);
    }

    // initTabs + bootstrapMailModes phải chạy SAU Settings.bootstrap()
    // vì cần Settings.get('ui.active_tab') + Settings.get('mail_mode.current')
    initTabs();
    bootstrapMailModes();
  })();

  updateComboCount();

  // Start unified SSE connection (single connection for all channels)
  SseBus.connect();

  // Timer cập nhật duration cho jobs đang running mỗi giây
  setInterval(() => {
    let hasRunning = false;
    for (const [id, j] of state.jobs) {
      if (j.status === 'running' && j.started_at) {
        hasRunning = true;
        j.duration = (Date.now() / 1000) - j.started_at;
      }
    }
    if (hasRunning) renderJobs();
  }, 1000);
})();
