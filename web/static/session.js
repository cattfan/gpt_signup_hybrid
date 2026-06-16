/* gpt_signup_hybrid — Get Session tab logic */
(() => {
  'use strict';

  // ── LocalStorage keys ─────────────────────────────────────────────
  const LS_INPUT_SES = 'gpt_reg.input.session';

  // ── State ─────────────────────────────────────────────────────────
  const state = {
    jobs: new Map(),
    order: [],
    activeJobId: null,
    maxConcurrent: 1,
  };

  // ── DOM refs ──────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const dom = {
    comboInput:   $('ses-combo-input'),
    btnRun:       $('ses-btn-run'),
    btnStopAll:   $('ses-btn-stop-all'),
    btnClearInput: $('ses-btn-clear-input'),
    comboCount:   $('ses-combo-count'),
    jobTimeout:   $('ses-job-timeout'),
    jobList:      $('ses-job-list'),
    jobSummary:   $('ses-job-summary'),
    logPane:      $('ses-log-pane'),
    logTarget:    $('ses-log-target'),
    successPane:  null,
    errorPane:    $('ses-error-pane'),
    btnCopyError:   $('ses-btn-copy-error'),
    btnClearDone:   $('ses-btn-clear-done'),
  };

  // ── Helpers ───────────────────────────────────────────────────────
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
    const token = window.GptUi.getAuthToken();
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
    window.GptUi.persistTextarea(LS_INPUT_SES, dom.comboInput.value);
  });

  // ── Render job list ───────────────────────────────────────────────
  function renderJobs() {
    if (state.order.length === 0) {
      dom.jobList.innerHTML = '<div class="empty">Paste combos and click Get Session.</div>';
      dom.jobSummary.textContent = '0 total';
      return;
    }

    const stats = { queued: 0, running: 0, success: 0, error: 0, cancelled: 0 };
    const html = state.order.map((id, idx) => {
      const j = state.jobs.get(id);
      if (!j) return '';
      stats[j.status] = (stats[j.status] || 0) + 1;
      const cls = state.activeJobId === id ? 'job is-active' : 'job';

      let actionBtns = '';
      if (j.status === 'running') {
        actionBtns = `<button class="icon-btn icon-danger" data-action="stop" data-id="${escHtml(id)}" title="Stop">${window.GptUi.icon('stop')}</button>`;
      } else if (j.status === 'success') {
        actionBtns = `
          <button class="icon-btn" data-action="reload" data-id="${escHtml(id)}" title="Reload session (lấy lại để xem type account mới)">${window.GptUi.icon('retry')}</button>
          <button class="icon-btn" data-action="download" data-id="${escHtml(id)}" title="Download JSON">${window.GptUi.icon('download')}</button>
          <button class="icon-btn" data-action="copy-json" data-id="${escHtml(id)}" title="Copy JSON">${window.GptUi.icon('copy')}</button>
          <button class="icon-btn" data-action="copy-token" data-id="${escHtml(id)}" title="Copy access token">${window.GptUi.icon('token')}</button>
        `;
      } else {
        actionBtns = `<button class="icon-btn" data-action="retry" data-id="${escHtml(id)}" title="Retry">${window.GptUi.icon('retry')}</button>`;
      }

      const planBadge = j.plan_type
        ? `<span class="plan-badge plan-${escHtml(j.plan_type.toLowerCase())}">${escHtml(j.plan_type)}</span>`
        : '';

      return `
        <div class="${cls}" data-id="${escHtml(id)}">
          <div class="job-index">${idx + 1}</div>
          <div class="job-status status-${escHtml(j.status)}">${escHtml(j.status)}</div>
          <div class="job-main">
            <div class="job-email" title="${escHtml(j.email)}">
              <span class="job-email-text">${escHtml(j.email)}</span>
              ${planBadge}
            </div>
          </div>
          <div class="job-duration">${escHtml(fmtDuration(j.duration))}</div>
          <div class="job-actions">
            ${actionBtns}
            <button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Remove">${window.GptUi.icon('remove')}</button>
          </div>
        </div>
      `;
    }).join('');

    dom.jobList.innerHTML = html;
    dom.jobSummary.textContent = [
      `${state.order.length} total`,
      stats.running ? `${stats.running} running` : '',
      stats.success ? `${stats.success} done` : '',
      stats.error ? `${stats.error} failed` : '',
    ].filter(Boolean).join(' · ');
  }

  // ── Render outputs ────────────────────────────────────────────────
  // Session data lưu local khi job success (để copy/download)
  const sessionCache = new Map(); // job_id → session_data

  function renderOutputs() {
    const errorLines = [];
    for (const id of state.order) {
      const j = state.jobs.get(id);
      if (!j) continue;
      if (j.status === 'success' && j.has_session && !sessionCache.has(id)) {
        // Auto-fetch session data khi job success
        loadSessionData(id);
      } else if (j.status === 'error') {
        errorLines.push(`${j.email}  →  ${j.error || 'unknown'}`);
      }
    }
    dom.errorPane.textContent = errorLines.length
      ? errorLines.join('\n')
      : 'No errors yet.';
  }

  function loadSessionData(jobId) {
    api(`/api/session/jobs/${jobId}`).then((data) => {
      if (data.session_data) {
        sessionCache.set(jobId, data.session_data);
      }
    }).catch(() => {});
  }

  // ── Render log ────────────────────────────────────────────────────
  function renderLog(jobId) {
    if (!jobId) {
      dom.logPane.textContent = '';
      dom.logTarget.textContent = '-';
      return;
    }
    const j = state.jobs.get(jobId);
    if (!j) return;
    dom.logTarget.textContent = j.email;
    api(`/api/session/jobs/${jobId}/log`).then((data) => {
      const lines = data.log || [];
      dom.logPane.innerHTML = lines.map((l) => {
        const cls = /(error|FAILED|fatal)/i.test(l) ? 'log-line-error' : 'log-line-info';
        return `<span class="${cls}">${escHtml(l)}</span>`;
      }).join('\n');
      dom.logPane.scrollTop = dom.logPane.scrollHeight;
    }).catch((err) => {
      dom.logPane.textContent = `[error] ${err.message}`;
    });
  }

  // ── Highlight dòng input tương ứng với job đang chọn ──────────────
  function highlightInputLine(jobId) {
    const j = state.jobs.get(jobId);
    if (!j || !j.email) return;
    const text = dom.comboInput.value;
    if (!text) return;
    const lines = text.split('\n');
    const target = j.email.trim().toLowerCase();
    let offset = 0;
    let foundIndex = -1;
    let start = 0;
    let end = 0;
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const email = line.trim().split('|')[0].trim().toLowerCase();
      if (email === target) {
        foundIndex = i;
        start = offset;
        end = offset + line.length;
        break;
      }
      offset += line.length + 1; // +1 cho ký tự '\n'
    }
    if (foundIndex === -1) return;
    dom.comboInput.focus();
    dom.comboInput.setSelectionRange(start, end);
    // Scroll dòng được chọn vào giữa khung textarea
    const cs = getComputedStyle(dom.comboInput);
    const lineHeight = parseFloat(cs.lineHeight) || 16;
    const padTop = parseFloat(cs.paddingTop) || 0;
    const targetTop = padTop + foundIndex * lineHeight;
    dom.comboInput.scrollTop = Math.max(0, targetTop - dom.comboInput.clientHeight / 2);
  }

  // ── Job actions ───────────────────────────────────────────────────
  dom.jobList.addEventListener('click', (e) => {
    const actionBtn = e.target.closest('[data-action]');
    if (actionBtn) {
      const action = actionBtn.dataset.action;
      const id = actionBtn.dataset.id;
      e.stopPropagation();
      if (action === 'retry') {
        api(`/api/session/jobs/${id}/retry`, { method: 'POST' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'reload') {
        // Lấy lại session để cập nhật type account (chạy lại get_session)
        sessionCache.delete(id);
        api(`/api/session/jobs/${id}/retry`, { method: 'POST' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'stop') {
        api(`/api/session/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'remove') {
        api(`/api/session/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'download' || action === 'copy-json' || action === 'copy-token') {
        // Lấy session data
        const cached = sessionCache.get(id);
        if (cached) {
          doSessionAction(action, id, cached);
        } else {
          api(`/api/session/jobs/${id}`).then((data) => {
            if (data.session_data) {
              sessionCache.set(id, data.session_data);
              doSessionAction(action, id, data.session_data);
            }
          }).catch(async (err) => { await Dialog.alert({ message: err.message }); });
        }
      }
      return;
    }
    const row = e.target.closest('.job');
    if (row) {
      state.activeJobId = row.dataset.id;
      renderJobs();
      renderLog(state.activeJobId);
      highlightInputLine(state.activeJobId);
    }
  });

  function doSessionAction(action, jobId, sessionData) {
    const j = state.jobs.get(jobId);
    const email = j ? j.email : 'session';
    if (action === 'download') {
      const filename = `session.${email}.json`;
      const blob = new Blob([JSON.stringify(sessionData, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } else if (action === 'copy-json') {
      window.GptUi.copyText(JSON.stringify(sessionData, null, 2));
    } else if (action === 'copy-token') {
      if (sessionData.accessToken) {
        window.GptUi.copyText(sessionData.accessToken);
      }
    }
  }

  // ── Run button ────────────────────────────────────────────────────
  dom.btnRun.addEventListener('click', async () => {
    const combos = dom.comboInput.value.trim();
    if (!combos) { await Dialog.alert({ message: 'Paste combos first.' }); return; }
    dom.btnRun.disabled = true;
    try {
      // Sync config
      const _modeMap = { single: 1, multi: 2, multi3: 3, multi5: 5, multi10: 10, multi20: 20, multi30: 30, multi50: 50 };
      const target = _modeMap[document.getElementById('mode').value] || 1;
      await api('/api/session/config', {
        method: 'POST',
        body: JSON.stringify({ max_concurrent: target }),
      });
      const regMode = document.getElementById('reg-mode-select')?.value || 'pure_request';
      await api('/api/session/jobs', {
        method: 'POST',
        body: JSON.stringify({ combos, reg_mode: regMode }),
      });
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    } finally {
      dom.btnRun.disabled = false;
    }
  });

  dom.btnClearInput.addEventListener('click', () => {
    dom.comboInput.value = '';
    updateComboCount();
    window.GptUi.clearPersistedTextarea(LS_INPUT_SES);
  });

  dom.btnStopAll.addEventListener('click', async () => {
    try {
      await api('/api/session/jobs/stop-all', { method: 'POST' });
    } catch (err) { await Dialog.alert({ message: err.message }); }
  });

  dom.btnClearDone.addEventListener('click', async () => {
    try {
      await api('/api/session/jobs/clear-finished', { method: 'POST' });
    } catch (err) { await Dialog.alert({ message: err.message }); }
  });

  dom.jobTimeout.addEventListener('change', async () => {
    const val = parseInt(dom.jobTimeout.value, 10);
    if (isNaN(val) || val < 30) return;
    try {
      await api('/api/session/config', {
        method: 'POST',
        body: JSON.stringify({ job_timeout: val }),
      });
    } catch (err) { console.error(err); }
  });

  // ── Copy error button ──────────────────────────────────────────────
  dom.btnCopyError.addEventListener('click', () => {
    window.GptUi.copyText(dom.errorPane.textContent);
  });

  // ── SSE (via SseBus) ────────────────────────────────────────────
  function applySnapshot(jobs) {
    state.order = jobs.map((j) => j.id);
    state.jobs.clear();
    for (const j of jobs) state.jobs.set(j.id, j);
    // Prune sessionCache: chỉ giữ entry cho jobs còn trong snapshot
    for (const cachedId of Array.from(sessionCache.keys())) {
      if (!state.jobs.has(cachedId)) sessionCache.delete(cachedId);
    }
    renderJobs();
    renderOutputs();
  }

  function applyJobUpdate(j) {
    const prev = state.jobs.get(j.id);
    if (!prev) state.order.push(j.id);
    state.jobs.set(j.id, j);
    renderJobs();
    renderOutputs();
    if (state.activeJobId === j.id) renderLog(j.id);
    if (j.status === 'error' && (!prev || prev.status !== 'error') && window.GptUi?.playErrorAlert) {
      window.GptUi.playErrorAlert();
    }
  }

  function applyRemove(jobId) {
    state.jobs.delete(jobId);
    state.order = state.order.filter((id) => id !== jobId);
    sessionCache.delete(jobId);
    if (state.activeJobId === jobId) { state.activeJobId = null; renderLog(null); }
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

  SseBus.on('session', (data) => {
    if (data.type === 'snapshot') {
      state.maxConcurrent = data.max_concurrent;
      applySnapshot(data.jobs);
    } else if (data.type === 'job') {
      applyJobUpdate(data.job);
    } else if (data.type === 'remove') {
      applyRemove(data.job_id);
    } else if (data.type === 'clear_finished') {
      api('/api/session/jobs').then((r) => applySnapshot(r.jobs)).catch(console.error);
    } else if (data.type === 'log') {
      applyLog(data.job_id, data.line);
    }
  });

  // ── Init ──────────────────────────────────────────────────────────
  // Restore textarea — chỉ mất khi user bấm Clear Input
  const _savedSes = localStorage.getItem(LS_INPUT_SES);
  if (_savedSes) dom.comboInput.value = _savedSes;
  updateComboCount();

  // Duration timer
  setInterval(() => {
    let hasRunning = false;
    for (const [, j] of state.jobs) {
      if (j.status === 'running' && j.started_at) {
        hasRunning = true;
        j.duration = (Date.now() / 1000) - j.started_at;
      }
    }
    if (hasRunning) renderJobs();
  }, 1000);
})();
