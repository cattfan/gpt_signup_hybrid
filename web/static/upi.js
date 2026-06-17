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
    proxyFromStep: $('upi-proxy-from-step'),
    notifyToggle:  $('upi-notify-toggle'),
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
    modalCountdown:$('upi-qr-modal-countdown'),
    modalExpVn:    $('upi-qr-modal-exp-vn'),
    modalExpIn:    $('upi-qr-modal-exp-in'),
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

  // Countdown tới expires_at (unix seconds). Trả {text, expired}.
  function fmtCountdown(expiresAt) {
    if (!expiresAt) return { text: '', expired: false };
    const remainMs = expiresAt * 1000 - Date.now();
    if (remainMs <= 0) return { text: 'Hết hạn', expired: true };
    const total = Math.floor(remainMs / 1000);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return { text: `${m}:${String(s).padStart(2, '0')}`, expired: false };
  }

  // ── Plan check (sau khi QR hết hạn) ─────────────────────────────────
  // Sau khi QR hết hạn → auto-poll POST check-session đến khi thấy Plus hoặc
  // hết lượt (upgrade UPI→Plus có thể chậm propagate). Mỗi lần nhận
  // {ok, plan, is_plus, expires, error}; server cache vào job.plan_check.
  // _planCheckInflight dedupe request đang bay; _planPollState quản poller/job.
  const _planCheckInflight = new Set();

  // Auto-poll: tối đa 6 lần check THẬT, mỗi lần cách ~20s tính TỪ lúc lần
  // trước hoàn tất (completion-driven, KHÔNG setInterval song song) — 1 check
  // worst-case ~40s nên timer cứng 20s sẽ chồng request.
  const PLAN_POLL_INTERVAL_MS = 20000;
  const PLAN_POLL_MAX = 6;
  // jobId → { count, timer }. Entry tồn tại = đang poll HOẶC đã xong (giữ entry
  // để updateCountdowns mỗi giây không spawn poller mới). Xóa khi job
  // removed / rời trạng thái success (xem _stopPlanPoll).
  const _planPollState = new Map();

  function renderPlanBadge(j) {
    if (!j || j.status !== 'success') return '';
    const pc = j.plan_check;
    if (!pc) return '';
    if (!pc.ok) {
      const errShort = (pc.error || 'check fail').slice(0, 80);
      return `<span class="badge upi-plan-badge upi-plan-err"
        title="${escHtml(errShort)}">PLAN ?</span>`;
    }
    const plan = (pc.plan || '').toString();
    if (pc.is_plus) {
      return `<span class="badge upi-plan-badge upi-plan-plus"
        title="account.planType=${escHtml(plan)}">${escHtml(plan.toUpperCase() || 'PLUS')}</span>`;
    }
    const label = (plan || 'free').toUpperCase();
    return `<span class="badge upi-plan-badge upi-plan-free"
      title="account.planType=${escHtml(plan || 'free')}">${escHtml(label)}</span>`;
  }

  // Fire 1 request check-session. Trả Promise<bool> = "đã thực sự gửi request"
  // (poller dùng để đếm đúng số lần check THẬT, không tính lần early-return).
  // force=true (poller + nút Recheck) bỏ qua cache guard j.plan_check; force=false
  // (render path mỗi giây) GIỮ guard — đây là thứ chặn flood 1 req/giây.
  function triggerPlanCheck(jobId, { force = false } = {}) {
    if (!jobId) return Promise.resolve(false);
    if (_planCheckInflight.has(jobId)) return Promise.resolve(false);
    const j = state.jobs.get(jobId);
    if (!j || j.status !== 'success') return Promise.resolve(false);
    if (!force && j.plan_check) return Promise.resolve(false);
    _planCheckInflight.add(jobId);
    return api(`/api/upi/jobs/${encodeURIComponent(jobId)}/check-session`, {
      method: 'POST',
    }).then((data) => {
      // Apply trực tiếp lên job state để render ngay; server cũng broadcast
      // job update qua SSE (plan_check field) — bên SSE handler sẽ overwrite.
      const cur = state.jobs.get(jobId);
      if (cur) {
        cur.plan_check = data;
        renderJobs();
      }
      return true;
    }).catch((err) => {
      console.warn('[upi] check-session failed:', err);
      // Đặt fake plan_check để hiện badge "PLAN ?" + tooltip lỗi.
      const cur = state.jobs.get(jobId);
      if (cur) {
        cur.plan_check = {
          ok: false, plan: null, is_plus: false, expires: null,
          checked_at: Math.floor(Date.now() / 1000),
          error: err && err.message ? err.message : 'request failed',
        };
        renderJobs();
      }
      return true;  // request đã gửi (dù lỗi) → vẫn tính 1 lần check thật
    }).finally(() => {
      _planCheckInflight.delete(jobId);
    });
  }

  // Dừng poller + xóa entry (cho phép poll lại nếu job quay về success sau này).
  function _stopPlanPoll(jobId) {
    const st = _planPollState.get(jobId);
    if (st && st.timer) clearTimeout(st.timer);
    _planPollState.delete(jobId);
  }

  // Khởi động auto-poll cho 1 job success vừa hết hạn QR. Self-guard chống
  // spawn trùng (SSE re-render gọi mỗi giây) + chống restart sau khi xong.
  function startPlanPoll(jobId) {
    if (!jobId) return;
    if (_planPollState.has(jobId)) return;  // đang poll HOẶC đã xong → không spawn lại
    const j = state.jobs.get(jobId);
    if (!j || j.status !== 'success') return;
    if (j.can_check_plan === false) return;  // mất cookies (server restart) → poll vô ích
    if (j.plan_check && j.plan_check.is_plus) return;  // đã Plus rồi
    _planPollState.set(jobId, { count: 0, timer: null });
    _planPollTick(jobId);  // check ngay lần đầu (QR vừa expired)
  }

  function _planPollTick(jobId) {
    const st = _planPollState.get(jobId);
    if (!st) return;
    const j = state.jobs.get(jobId);
    // Guard đầu tick TRƯỚC khi đọc property — job có thể bị remove/rời success.
    if (!j || j.status !== 'success') { _stopPlanPoll(jobId); return; }
    // Đã Plus / hết lượt → ngừng nhưng GIỮ entry (để không restart mỗi giây).
    if (j.plan_check && j.plan_check.is_plus) { st.timer = null; return; }
    if (st.count >= PLAN_POLL_MAX) { st.timer = null; return; }

    triggerPlanCheck(jobId, { force: true }).then((fired) => {
      if (!_planPollState.has(jobId)) return;  // bị cleanup giữa chừng
      if (fired) st.count += 1;  // chỉ đếm lần check THẬT
      const after = state.jobs.get(jobId);
      if (!after || after.status !== 'success') { _stopPlanPoll(jobId); return; }
      if ((after.plan_check && after.plan_check.is_plus) || st.count >= PLAN_POLL_MAX) {
        st.timer = null;  // xong: giữ entry, không restart
        return;
      }
      // Lên lịch tick kế ~20s TỪ completion (không phải timer song song).
      st.timer = setTimeout(() => _planPollTick(jobId), PLAN_POLL_INTERVAL_MS);
    });
  }

  // Định dạng thời điểm hết hạn theo timezone (VN / IN).
  function fmtExpiryAt(expiresAt, tz) {
    if (!expiresAt) return '-';
    try {
      return new Date(expiresAt * 1000).toLocaleString('vi-VN', {
        timeZone: tz, hour12: false,
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    } catch (_) {
      return new Date(expiresAt * 1000).toISOString();
    }
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
      // Recheck plan: force check-session ngay (bỏ qua cache), kể cả khi đã có
      // plan_check — cho user ép kiểm tra lại sau khi UPI pump lên Plus.
      if (j.status === 'success') {
        actionBtns += `<button class="icon-btn upi-recheck-btn" data-action="recheck-plan" data-id="${escHtml(id)}" title="Recheck plan">${window.GptUi.icon('retry')}</button>`;
      }
      actionBtns += `<button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Remove">${window.GptUi.icon('remove')}</button>`;

      const amountBadge = j.amount
        ? `<span class="badge badge-muted upi-amount" title="amount inr">${escHtml(fmtAmount(j.amount))}</span>`
        : '';
      const countdownBadge = (j.status === 'success' && j.qr_expires_at)
        ? `<span class="badge upi-countdown-badge" data-exp="${escHtml(String(j.qr_expires_at))}" title="QR hết hạn sau"></span>`
        : '';
      const planBadge = renderPlanBadge(j);
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
              ${countdownBadge}
              ${planBadge}
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
    updateCountdowns();
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
      // Mỗi span tự kết thúc bằng '\n' (giống applyLog) để khi SSE append
      // span mới sẽ không bị dính vào span cuối.
      dom.logPane.innerHTML = lines.map((l) => {
        const cls = /(error|FAILED|fatal|threshold)/i.test(l) ? 'log-line-error' : 'log-line-info';
        return `<span class="${cls}">${escHtml(l)}\n</span>`;
      }).join('');
      dom.logPane.scrollTop = dom.logPane.scrollHeight;
    }).catch((err) => {
      dom.logPane.textContent = `[error] ${err.message}`;
    });
  }

  // ── QR Modal ──────────────────────────────────────────────────────
  let _modalActiveJobId = null;
  let _modalExpiresAt = null;

  // Set các dòng hết hạn (VN/IN) trong modal + lưu mốc để countdown tick.
  function _setModalExpiry(expiresAt) {
    _modalExpiresAt = expiresAt || null;
    dom.modalExpVn.textContent = fmtExpiryAt(expiresAt, 'Asia/Ho_Chi_Minh');
    dom.modalExpIn.textContent = fmtExpiryAt(expiresAt, 'Asia/Kolkata');
    _tickModalCountdown();
  }

  function _tickModalCountdown() {
    if (dom.modal.style.display === 'none') return;
    const cd = fmtCountdown(_modalExpiresAt);
    dom.modalCountdown.textContent = _modalExpiresAt ? (cd.text || '-') : '-';
    dom.modalCountdown.classList.toggle('upi-countdown-expired', cd.expired);
  }

  // Cập nhật mọi badge countdown trên job list (data-exp) + modal.
  function updateCountdowns() {
    const badges = dom.jobList.querySelectorAll('.upi-countdown-badge[data-exp]');
    badges.forEach((el) => {
      const exp = parseInt(el.dataset.exp, 10);
      const cd = fmtCountdown(exp);
      el.textContent = cd.text;
      el.classList.toggle('upi-countdown-expired', cd.expired);
      // Vừa cross 0 → khởi động auto-poll (self-guard chống spawn trùng).
      // KHÔNG gọi triggerPlanCheck trực tiếp: hàm này chạy mỗi giây nên sẽ
      // flood; startPlanPoll dedupe theo _planPollState.
      if (cd.expired) {
        const row = el.closest('[data-id]');
        if (row && row.dataset.id) {
          startPlanPoll(row.dataset.id);
        }
      }
    });
    _tickModalCountdown();
  }

  function openQrModal(jobId) {
    const j = state.jobs.get(jobId);
    if (!j || !j.has_qr) return;
    _modalActiveJobId = jobId;
    dom.modalEmail.textContent = j.email;
    dom.modalAmount.textContent = j.amount ? fmtAmount(j.amount) : '-';
    dom.modalSource.textContent = j.qr_source || '-';
    dom.modalCs.textContent = j.checkout_session || '-';
    _setModalExpiry(j.qr_expires_at);
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
    _modalExpiresAt = null;
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
      } else if (action === 'recheck-plan') {
        triggerPlanCheck(id, { force: true });
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
      const proxyFromStep = parseInt(dom.proxyFromStep.value, 10) || 3;
      await api('/api/upi/config', {
        method: 'POST',
        body: JSON.stringify({
          max_concurrent: target,
          job_timeout: jobTimeout,
          approve_retries: approveRetries,
          proxy_from_step: proxyFromStep,
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

  dom.proxyFromStep.addEventListener('change', async () => {
    const val = parseInt(dom.proxyFromStep.value, 10);
    if (isNaN(val) || val < 1 || val > 6) return;
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ proxy_from_step: val }),
      });
    } catch (err) {
      console.error(err);
      await Dialog.alert({ message: 'Không lưu được proxy_from_step: ' + err.message });
    }
  });

  dom.notifyToggle.addEventListener('change', async () => {
    const enabled = dom.notifyToggle.checked;
    try {
      await api('/api/upi/config', {
        method: 'POST', body: JSON.stringify({ notify_enabled: enabled }),
      });
    } catch (err) {
      dom.notifyToggle.checked = !enabled; // revert nếu fail
      await Dialog.alert({ message: 'Không lưu được toggle: ' + err.message });
    }
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
    if (snap.proxy_from_step) dom.proxyFromStep.value = String(snap.proxy_from_step);

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

    // Job rời success (retry → running, error, …) → dừng poller để khỏi leak
    // timer + tránh tick đọc job đã đổi trạng thái.
    if (j.status !== 'success') _stopPlanPoll(j.id);

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
      _setModalExpiry(j.qr_expires_at);
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
    _stopPlanPoll(jobId);  // dọn timer poll (H1: callback sau remove sẽ TypeError + leak)
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
    if (cfg.proxy_from_step) dom.proxyFromStep.value = String(cfg.proxy_from_step);
    state.approveRetries = cfg.approve_retries;
    dom.notifyToggle.checked = !!cfg.notify_enabled;
  }).catch(() => {});

  // Duration timer cho running jobs + countdown QR
  setInterval(() => {
    let hasRunning = false;
    for (const [, j] of state.jobs) {
      if (j.status === 'running' && j.started_at) {
        hasRunning = true;
        j.duration = (Date.now() / 1000) - j.started_at;
      }
    }
    if (hasRunning) renderJobs();
    else updateCountdowns();
  }, 1000);
})();
