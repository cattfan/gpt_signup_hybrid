/* gpt_signup_hybrid — UPI QR tab logic.
   Job item compact 1-row giống Get Session. Click icon QR → mở modal full-size. */
(() => {
  'use strict';

  const LS_INPUT_UPI = 'gpt_reg.input.upi';

  const state = {
    jobs: new Map(),
    order: [],
    activeJobId: null,
    maxConcurrent: 1,
    approveRetries: 500,
  };

  const $ = (id) => document.getElementById(id);
  const dom = {
    comboInput:    $('upi-combo-input'),
    btnRun:        $('upi-btn-run'),
    btnStopAll:    $('upi-btn-stop-all'),
    btnClearInput: $('upi-btn-clear-input'),
    comboCount:    $('upi-combo-count'),
    approveRetries:$('upi-approve-retries'),
    jobTimeout:    $('upi-job-timeout'),
    jobList:       $('upi-job-list'),
    jobSummary:    $('upi-job-summary'),
    logPane:       $('upi-log-pane'),
    logTarget:     $('upi-log-target'),
    errorPane:     $('upi-error-pane'),
    btnCopyError:  $('upi-btn-copy-error'),
    btnClearDone:  $('upi-btn-clear-done'),
    // Modal
    modal:         $('upi-qr-modal'),
    modalImg:      $('upi-qr-modal-img'),
    modalEmail:    $('upi-qr-modal-email'),
    modalAmount:   $('upi-qr-modal-amount'),
    modalSource:   $('upi-qr-modal-source'),
    modalCs:       $('upi-qr-modal-cs'),
    modalClose:    $('upi-qr-modal-close'),
    modalOk:       $('upi-qr-modal-ok'),
    modalCopyUrl:  $('upi-qr-modal-copy-url'),
    modalDownload: $('upi-qr-modal-download'),
    modalOpen:     $('upi-qr-modal-open'),
  };

  // ── Helpers ───────────────────────────────────────────────────────
  function fmtDuration(secs) {
    if (secs == null) return '';
    if (secs < 60) return secs.toFixed(1) + 's';
    return Math.floor(secs / 60) + 'm' + Math.floor(secs % 60) + 's';
  }

  function fmtAmount(amount) {
    if (!amount) return '';
    return `₹${(amount / 100).toFixed(2)}`;
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
    return fetch(path, { ...opts, headers }).then((r) => {
      if (!r.ok) return r.text().then((t) => { throw new Error(`HTTP ${r.status}: ${t}`); });
      return r.json();
    });
  }

  // ── QR blob cache: tải PNG về local 1 lần khi job success, sau đó
  // dùng Blob URL cho <img>/download/open. Không gọi /api/upi/jobs/{id}/qr
  // mỗi lần mở modal. Revoke khi job remove/retry để tránh memory leak.
  // ────────────────────────────────────────────────────────────────────
  const qrBlobCache = new Map();   // jobId → { url, finished_at, contentType }
  const qrFetchPromise = new Map(); // jobId → in-flight Promise (dedup)

  function _qrApiUrl(jobId) {
    const token = window.GptUi.getAuthToken();
    return `/api/upi/jobs/${encodeURIComponent(jobId)}/qr` + (token ? `?token=${encodeURIComponent(token)}` : '');
  }

  function fetchQrBlob(jobId, finishedAt) {
    // Cache hit và finished_at chưa đổi → trả entry cũ.
    const cached = qrBlobCache.get(jobId);
    if (cached && cached.finished_at === finishedAt) {
      return Promise.resolve(cached);
    }
    // Có in-flight cho job này → reuse.
    if (qrFetchPromise.has(jobId)) {
      return qrFetchPromise.get(jobId);
    }
    // Có cache cũ với finished_at khác → revoke trước khi fetch lại.
    if (cached) {
      try { URL.revokeObjectURL(cached.url); } catch (_) {}
      qrBlobCache.delete(jobId);
    }
    const token = window.GptUi.getAuthToken();
    const promise = fetch(_qrApiUrl(jobId), {
      headers: token ? { 'X-API-Token': token } : {},
    }).then((r) => {
      if (!r.ok) throw new Error(`QR fetch HTTP ${r.status}`);
      const ct = r.headers.get('content-type') || 'image/png';
      return r.blob().then((blob) => ({ blob, ct }));
    }).then(({ blob, ct }) => {
      const url = URL.createObjectURL(blob);
      const entry = { url, finished_at: finishedAt, contentType: ct };
      qrBlobCache.set(jobId, entry);
      return entry;
    }).finally(() => {
      qrFetchPromise.delete(jobId);
    });
    qrFetchPromise.set(jobId, promise);
    return promise;
  }

  function revokeQrBlob(jobId) {
    const entry = qrBlobCache.get(jobId);
    if (entry) {
      try { URL.revokeObjectURL(entry.url); } catch (_) {}
      qrBlobCache.delete(jobId);
    }
    qrFetchPromise.delete(jobId);
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
    window.GptUi.persistTextarea(LS_INPUT_UPI, dom.comboInput.value);
  });

  // ── Render job list (compact 1-row giống Get Session) ─────────────
  function renderJobs() {
    if (state.order.length === 0) {
      dom.jobList.innerHTML = '<div class="empty">Paste accounts and click Get UPI QR.</div>';
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
      // QR icon — chỉ active khi success+has_qr
      if (j.status === 'success' && j.has_qr) {
        actionBtns += `<button class="icon-btn icon-accent" data-action="view-qr" data-id="${escHtml(id)}" title="Xem QR">${window.GptUi.icon('qr')}</button>`;
      }
      // Action chính theo status
      if (j.status === 'running') {
        actionBtns += `<button class="icon-btn icon-danger" data-action="stop" data-id="${escHtml(id)}" title="Stop">${window.GptUi.icon('stop')}</button>`;
      } else {
        actionBtns += `<button class="icon-btn" data-action="retry" data-id="${escHtml(id)}" title="Retry">${window.GptUi.icon('retry')}</button>`;
      }
      if (j.status === 'success' && j.return_url) {
        actionBtns += `<button class="icon-btn" data-action="copy-checkout" data-id="${escHtml(id)}" title="Copy checkout URL">${window.GptUi.icon('copy')}</button>`;
      }
      actionBtns += `<button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Remove">${window.GptUi.icon('remove')}</button>`;

      const amountBadge = j.amount
        ? `<span class="badge badge-muted upi-amount" title="amount inr">${escHtml(fmtAmount(j.amount))}</span>`
        : '';
      const errBadge = (j.status === 'error' && j.error)
        ? `<span class="upi-err-inline" title="${escHtml(j.error)}">${escHtml(j.error.slice(0, 60))}</span>`
        : '';

      return `
        <div class="${cls}" data-id="${escHtml(id)}">
          <div class="job-index">${idx + 1}</div>
          <div class="job-status status-${escHtml(j.status)}">${escHtml(j.status)}</div>
          <div class="job-main">
            <div class="job-email" title="${escHtml(j.email)}">
              <span class="job-email-text">${escHtml(j.email)}</span>
              ${amountBadge}
              ${errBadge}
            </div>
          </div>
          <div class="job-duration">${escHtml(fmtDuration(j.duration))}</div>
          <div class="job-actions">${actionBtns}</div>
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
  function renderOutputs() {
    const errorLines = [];
    for (const id of state.order) {
      const j = state.jobs.get(id);
      if (!j) continue;
      if (j.status === 'error') {
        errorLines.push(`${j.email}  →  ${j.error || 'unknown'}`);
      }
    }
    dom.errorPane.textContent = errorLines.length ? errorLines.join('\n') : 'No errors yet.';
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
    api(`/api/upi/jobs/${jobId}/log`).then((data) => {
      const lines = data.log || [];
      dom.logPane.innerHTML = lines.map((l) => {
        const cls = /(error|FAILED|fatal|threshold)/i.test(l) ? 'log-line-error' : 'log-line-info';
        return `<span class="${cls}">${escHtml(l)}</span>`;
      }).join('\n');
      dom.logPane.scrollTop = dom.logPane.scrollHeight;
    }).catch((err) => {
      dom.logPane.textContent = `[error] ${err.message}`;
    });
  }

  // ── QR Modal ──────────────────────────────────────────────────────
  let _modalActiveJobId = null;

  function openQrModal(jobId) {
    const j = state.jobs.get(jobId);
    if (!j || !j.has_qr) return;
    _modalActiveJobId = jobId;
    dom.modalEmail.textContent = j.email;
    dom.modalAmount.textContent = j.amount ? fmtAmount(j.amount) : '-';
    dom.modalSource.textContent = j.qr_source || '-';
    dom.modalCs.textContent = j.checkout_session || '-';
    dom.modal.style.display = 'flex';
    if (dom.modalOk) dom.modalOk.focus();

    // Lấy ảnh từ Blob cache; nếu chưa có → fetch về (dùng spinner placeholder).
    const finishedAt = j.finished_at || 0;
    const cached = qrBlobCache.get(jobId);
    if (cached && cached.finished_at === finishedAt) {
      dom.modalImg.src = cached.url;
      return;
    }
    dom.modalImg.removeAttribute('src');
    fetchQrBlob(jobId, finishedAt).then((entry) => {
      // Verify modal vẫn đang mở cùng job → set src (tránh race khi user đóng / mở job khác).
      if (_modalActiveJobId === jobId) {
        dom.modalImg.src = entry.url;
      }
    }).catch((err) => {
      if (_modalActiveJobId === jobId) {
        dom.modalImg.removeAttribute('src');
      }
      Dialog.alert({ message: 'Tải QR thất bại: ' + err.message }).catch(() => {});
    });
  }

  function closeQrModal() {
    dom.modal.style.display = 'none';
    dom.modalImg.removeAttribute('src');
    _modalActiveJobId = null;
  }

  dom.modalClose.addEventListener('click', closeQrModal);
  dom.modalOk.addEventListener('click', closeQrModal);
  dom.modal.addEventListener('click', (e) => {
    if (e.target === dom.modal) closeQrModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && dom.modal.style.display !== 'none') {
      closeQrModal();
    }
  });

  dom.modalCopyUrl.addEventListener('click', () => {
    if (!_modalActiveJobId) return;
    const j = state.jobs.get(_modalActiveJobId);
    if (j && j.return_url) window.GptUi.copyText(j.return_url);
  });

  dom.modalDownload.addEventListener('click', () => {
    if (!_modalActiveJobId) return;
    const j = state.jobs.get(_modalActiveJobId);
    const finishedAt = (j && j.finished_at) || 0;
    fetchQrBlob(_modalActiveJobId, finishedAt).then((entry) => {
      const ext = entry.contentType.includes('svg') ? 'svg' : 'png';
      const a = document.createElement('a');
      a.href = entry.url;
      a.download = `upi_qr_${j ? j.email.replace(/[^a-zA-Z0-9]+/g, '_') : _modalActiveJobId}.${ext}`;
      a.click();
    }).catch((err) => Dialog.alert({ message: 'Download fail: ' + err.message }).catch(() => {}));
  });

  dom.modalOpen.addEventListener('click', () => {
    if (!_modalActiveJobId) return;
    const j = state.jobs.get(_modalActiveJobId);
    const finishedAt = (j && j.finished_at) || 0;
    fetchQrBlob(_modalActiveJobId, finishedAt).then((entry) => {
      window.open(entry.url, '_blank', 'noopener');
    }).catch((err) => Dialog.alert({ message: 'Open fail: ' + err.message }).catch(() => {}));
  });

  // ── Job list actions ──────────────────────────────────────────────
  dom.jobList.addEventListener('click', (e) => {
    const actionBtn = e.target.closest('[data-action]');
    if (actionBtn) {
      const action = actionBtn.dataset.action;
      const id = actionBtn.dataset.id;
      e.stopPropagation();
      if (action === 'retry') {
        api(`/api/upi/jobs/${id}/retry`, { method: 'POST' })
          .catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'stop' || action === 'remove') {
        api(`/api/upi/jobs/${id}`, { method: 'DELETE' })
          .catch(async (err) => { await Dialog.alert({ message: err.message }); });
      } else if (action === 'view-qr') {
        openQrModal(id);
      } else if (action === 'copy-checkout') {
        const j = state.jobs.get(id);
        if (j && j.return_url) window.GptUi.copyText(j.return_url);
      }
      return;
    }
    const row = e.target.closest('.job');
    if (row) {
      state.activeJobId = row.dataset.id;
      renderJobs();
      renderLog(state.activeJobId);
    }
  });

  // ── Run button ────────────────────────────────────────────────────
  dom.btnRun.addEventListener('click', async () => {
    const combos = dom.comboInput.value.trim();
    if (!combos) { await Dialog.alert({ message: 'Paste accounts first.' }); return; }
    dom.btnRun.disabled = true;
    try {
      const _modeMap = {
        single: 1, multi: 2, multi3: 3, multi5: 5, multi10: 10,
        multi20: 20, multi30: 30, multi50: 50,
      };
      const target = _modeMap[document.getElementById('mode').value] || 1;
      const approveRetries = parseInt(dom.approveRetries.value, 10) || 500;
      const jobTimeout = parseInt(dom.jobTimeout.value, 10) || 1800;
      await api('/api/upi/config', {
        method: 'POST',
        body: JSON.stringify({
          max_concurrent: target,
          job_timeout: jobTimeout,
          approve_retries: approveRetries,
        }),
      });
      await api('/api/upi/jobs', {
        method: 'POST',
        body: JSON.stringify({ combos }),
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
    window.GptUi.clearPersistedTextarea(LS_INPUT_UPI);
  });

  dom.btnStopAll.addEventListener('click', async () => {
    try { await api('/api/upi/jobs/stop-all', { method: 'POST' }); }
    catch (err) { await Dialog.alert({ message: err.message }); }
  });

  dom.btnClearDone.addEventListener('click', async () => {
    try { await api('/api/upi/jobs/clear-finished', { method: 'POST' }); }
    catch (err) { await Dialog.alert({ message: err.message }); }
  });

  dom.approveRetries.addEventListener('change', async () => {
    const val = parseInt(dom.approveRetries.value, 10);
    if (isNaN(val) || val < 1) return;
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ approve_retries: val }),
      });
      state.approveRetries = val;
    } catch (err) { console.error(err); }
  });

  dom.jobTimeout.addEventListener('change', async () => {
    const val = parseInt(dom.jobTimeout.value, 10);
    if (isNaN(val) || val < 60) return;
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ job_timeout: val }),
      });
    } catch (err) { console.error(err); }
  });

  dom.btnCopyError.addEventListener('click', () => {
    window.GptUi.copyText(dom.errorPane.textContent);
  });

  // ── SSE ───────────────────────────────────────────────────────────
  function _maybePrefetchQr(j) {
    // Job vừa success + có QR → tải blob về cache local ngay (không đợi user mở modal).
    // Nếu finished_at thay đổi (retry) → fetchQrBlob tự revoke entry cũ.
    if (j && j.has_qr) {
      fetchQrBlob(j.id, j.finished_at || 0).catch(() => {
        // Best-effort — log fail không làm vỡ flow.
      });
    }
  }

  function applySnapshot(snap) {
    state.maxConcurrent = snap.max_concurrent || state.maxConcurrent;
    state.approveRetries = snap.approve_retries || state.approveRetries;
    if (snap.approve_retries) dom.approveRetries.value = snap.approve_retries;
    if (snap.job_timeout) dom.jobTimeout.value = snap.job_timeout;

    // Revoke blob cho job không còn trong snapshot (cleanup khi server clear).
    const incomingIds = new Set(snap.jobs.map((j) => j.id));
    for (const cachedId of Array.from(qrBlobCache.keys())) {
      if (!incomingIds.has(cachedId)) revokeQrBlob(cachedId);
    }

    state.order = snap.jobs.map((j) => j.id);
    state.jobs.clear();
    for (const j of snap.jobs) {
      state.jobs.set(j.id, j);
      _maybePrefetchQr(j);
    }
    renderJobs();
    renderOutputs();
  }

  function applyJobUpdate(j) {
    const prev = state.jobs.get(j.id);
    if (!prev) state.order.push(j.id);
    state.jobs.set(j.id, j);

    // Job retry (mất has_qr) hoặc QR mới (finished_at đổi) → revoke entry cũ.
    if (prev && prev.has_qr && (!j.has_qr || prev.finished_at !== j.finished_at)) {
      revokeQrBlob(j.id);
    }
    _maybePrefetchQr(j);

    renderJobs();
    renderOutputs();
    if (state.activeJobId === j.id) renderLog(j.id);

    // Modal đang mở cho job này + QR mới về → cập nhật src + meta.
    if (_modalActiveJobId === j.id) {
      dom.modalAmount.textContent = j.amount ? fmtAmount(j.amount) : '-';
      dom.modalSource.textContent = j.qr_source || '-';
      dom.modalCs.textContent = j.checkout_session || '-';
      if (j.has_qr) {
        fetchQrBlob(j.id, j.finished_at || 0).then((entry) => {
          if (_modalActiveJobId === j.id) {
            dom.modalImg.src = entry.url;
          }
        }).catch(() => {});
      }
    }

    if (j.status === 'error' && (!prev || prev.status !== 'error') && window.GptUi?.playErrorAlert) {
      window.GptUi.playErrorAlert();
    }
  }

  function applyRemove(jobId) {
    state.jobs.delete(jobId);
    state.order = state.order.filter((id) => id !== jobId);
    revokeQrBlob(jobId);
    if (state.activeJobId === jobId) { state.activeJobId = null; renderLog(null); }
    if (_modalActiveJobId === jobId) closeQrModal();
    renderJobs();
    renderOutputs();
  }

  function applyLog(jobId, line) {
    if (state.activeJobId !== jobId) return;
    const cls = /(error|FAILED|fatal|threshold)/i.test(line) ? 'log-line-error' : 'log-line-info';
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = line + '\n';
    dom.logPane.appendChild(span);
    dom.logPane.scrollTop = dom.logPane.scrollHeight;
  }

  SseBus.on('upi', (data) => {
    if (data.type === 'snapshot') applySnapshot(data);
    else if (data.type === 'job') applyJobUpdate(data.job);
    else if (data.type === 'remove') applyRemove(data.job_id);
    else if (data.type === 'clear_finished') {
      api('/api/upi/jobs').then(applySnapshot).catch(console.error);
    }
    else if (data.type === 'log') applyLog(data.job_id, data.line);
  });

  // ── Init ──────────────────────────────────────────────────────────
  const _saved = localStorage.getItem(LS_INPUT_UPI);
  if (_saved) dom.comboInput.value = _saved;
  updateComboCount();

  api('/api/upi/config').then((cfg) => {
    if (cfg.approve_retries) dom.approveRetries.value = cfg.approve_retries;
    if (cfg.job_timeout) dom.jobTimeout.value = cfg.job_timeout;
    state.approveRetries = cfg.approve_retries;
  }).catch(() => {});

  // Duration timer cho running jobs
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
