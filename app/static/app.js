/* Capsule frontend — single-page Alpine app.
 *
 * Talks to the Phase 1+ backend over the REST endpoints in CLAUDE.md §2.
 * Keeps state on <body x-data="capsule()">; routes via location.hash.
 *
 * Visual-first rules:
 *   - status colour is always paired with an icon (icon-directional mirrors
 *     under RTL).
 *   - every translatable string goes through t(); no concatenation.
 *   - every progress badge has both an emoji-free Lucide icon and text.
 */

(function () {
  'use strict';

  const LANGS = ['en', 'ar', 'ja', 'es'];
  const ROUTES = ['home', 'settings'];

  function readPref(key, fallback) {
    try { return localStorage.getItem(key) || fallback; }
    catch (_) { return fallback; }
  }
  function writePref(key, value) {
    try { localStorage.setItem(key, value); }
    catch (_) { /* private mode — ignore */ }
  }

  function detectInitialLang() {
    const url = new URL(window.location.href);
    const fromQuery = url.searchParams.get('lang');
    if (fromQuery && LANGS.includes(fromQuery)) return fromQuery;
    const fromStorage = readPref('capsule.lang', null);
    if (fromStorage && LANGS.includes(fromStorage)) return fromStorage;
    const fromBrowser = (navigator.language || 'en').split('-', 1)[0];
    if (LANGS.includes(fromBrowser)) return fromBrowser;
    return 'en';
  }

  function safeJSON(s) { try { return JSON.parse(s); } catch (_) { return null; } }

  // Locale-aware relative time. Uses Intl.RelativeTimeFormat so en/ja/ar/es
  // (and any future locale) get correct words and digits with no extra i18n
  // keys (CLAUDE.md §13 #6 — no hardcoded user-facing strings).
  function relTime(iso, locale) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return iso;
    const delta = Math.round((t - Date.now()) / 1000); // negative ⇒ past
    const rtf = new Intl.RelativeTimeFormat(locale || 'en', { numeric: 'auto' });
    const a = Math.abs(delta);
    if (a < 60) return rtf.format(delta, 'second');
    if (a < 3600) return rtf.format(Math.round(delta / 60), 'minute');
    if (a < 86400) return rtf.format(Math.round(delta / 3600), 'hour');
    return rtf.format(Math.round(delta / 86400), 'day');
  }

  function formatBytes(n) {
    if (n == null) return '—';
    let i = 0; const units = ['B','KB','MB','GB','TB'];
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  // Phase ordering — used by phaseDone/phaseClass below.
  // ``status`` arrives from the backend as either a job status (queued,
  // running, retrying, paused, done, failed_permanent, cancelled) or a
  // pipeline phase (classifying, snapshotting, downloading, finalizing).
  // The orchestrator emits both onto the same SSE channel.
  const PHASE_BY_STATUS = {
    queued: 0,
    running: 0,
    classifying: 0,
    snapshotting: 1,
    downloading: 2,
    finalizing: 3,
    done: 4,
    // Retry / pause: phases below the latest reached one stay green; the
    // current phase gets the retry/pause treatment via phaseClass.
    retrying: -2,
    paused: -3,
    failed_permanent: -1,
    failed: -1,            // legacy alias kept for any in-flight rows
    cancelled: -4,
  };

  const TERMINAL_STATUSES = new Set(['done', 'failed_permanent', 'cancelled']);
  const FAILED_STATUSES = new Set(['failed_permanent', 'failed']);
  // After this many consecutive EventSource transport errors with no
  // intervening status event, surface a Reconnect affordance to the user.
  const SSE_DISCONNECT_THRESHOLD = 3;

  window.capsule = function () {
    return {
      // --- Identity / routing ---
      route: 'home',

      // --- Locale ---
      locale: detectInitialLang(),
      direction: 'ltr',
      // App-wide capture profile (slow ↔ fast). Server is the source of
      // truth (`GET /api/system/profile`); this is the cached value driving
      // the speed pill. Default 'slow' until refreshProfile() lands.
      profile: 'slow',
      languages: LANGS,
      messages: {},
      _formatters: {},

      // --- Data ---
      systemVersion: null,
      activeJobs: [],
      _jobSources: {}, // job_id -> EventSource
      // Consecutive transport errors per job. Cleared whenever a 'status'
      // event arrives. After SSE_DISCONNECT_THRESHOLD failures we flip
      // job.disconnected so the UI surfaces a Reconnect affordance instead
      // of leaving a phantom "running" card forever (CLAUDE.md §4.7).
      _streamErrors: {},

      // --- Download visualizer ---
      // Rolling buffer of recent combined-speed samples (bytes/sec), pushed
      // once per second by tickAggregate while any job is downloading.
      // Capped at 30 entries so the sparkline shows the last ~30 s.
      _aggHistory: [],
      _aggTickHandle: null,

      // --- UI state ---
      updating: false,
      updateResult: null,

      // --- Downloader ---
      simpleTab: 'single',          // 'single' | 'list'
      simpleUrl: '',
      simpleUrls: '',
      recentCaptures: [],
      quickCaseId: null,
      quickCaseSlug: null,
      quickCaseName: null,
      pathCopied: false,
      // Inline error banner shown when /api/jobs/batch fails (CLAUDE.md §4.7).
      // Replaces the old browser alert(): translatable headline + optional
      // technical-details block users can copy into a bug report.
      batchError: null,             // { headline, technical } | null
      // CLAUDE.md §15: surface the dedup outcome ("X new · Y already in
      // this case") after every preflight run. Auto-clears on next submit.
      batchSummary: null,           // { new, dup, ib, failed } | null
      // CLAUDE.md §15 modal — queue of duplicates the user must resolve
      // before the rest of the batch is submitted.
      duplicateModal: null,         // { queue:[], index:0, pending:[], lang } | null
      // Clear-list confirmation dialog (plan §I).
      clearDialog: null,            // { open, count, case_name, freed_bytes_human } | null
      clearInProgress: false,

      // CLAUDE.md §15 v0.7: per-submission download options (audio_only,
      // quality cap, subtitle languages). Persisted in localStorage so
      // sticky preferences survive reloads. Sent on every JobBatchItem in
      // _postBatch().
      downloadOptions: {
        audio_only: false,
        quality_cap: null,          // 'audio'|'480'|'720'|'1080'|'best'|null
        subtitle_langs: [],
      },
      // Cancel/Restart per-job confirmation dialog. Goes through the same
      // shape as clearDialog for the destructive-action discipline in §15.
      controlConfirm: null,         // { open, action, jobId, busy } | null

      // --- Extension pairing (Settings → Browser extension) ---
      extension: {
        tokens: [],
        justIssued: null,    // { token, server_fingerprint, ... } shown once
        pairing: false,
      },

      toast: '',
      _toastTimer: null,

      async boot() {
        await this.loadBundle(this.locale);
        this.applyHtmlAttrs();
        this.loadDownloadOptions();
        this.parseHash();
        window.addEventListener('hashchange', () => this.parseHash());
        await this.refreshAll();
        await this.refreshProfile();
        await this.refreshRecentCaptures();
        this.renderAll();
        this.refreshIcons();
      },

      // --- Download options (CLAUDE.md §15 v0.7) ---

      loadDownloadOptions() {
        try {
          const raw = localStorage.getItem('capsule.downloadOptions');
          if (!raw) return;
          const parsed = JSON.parse(raw);
          if (typeof parsed === 'object' && parsed) {
            this.downloadOptions.audio_only = !!parsed.audio_only;
            this.downloadOptions.quality_cap = parsed.quality_cap || null;
            const langs = Array.isArray(parsed.subtitle_langs)
              ? parsed.subtitle_langs.filter(s => typeof s === 'string')
              : [];
            this.downloadOptions.subtitle_langs = langs;
          }
        } catch (_) { /* corrupted localStorage — ignore, defaults stand */ }
      },

      _persistDownloadOptions() {
        try {
          localStorage.setItem(
            'capsule.downloadOptions',
            JSON.stringify(this.downloadOptions),
          );
        } catch (_) { /* quota / private mode — best effort */ }
      },

      hasNonDefaultDownloadOptions() {
        const o = this.downloadOptions;
        return !!(o.audio_only || o.quality_cap || (o.subtitle_langs || []).length);
      },

      effectiveQualityCap() {
        if (this.downloadOptions.audio_only) return 'audio';
        return this.downloadOptions.quality_cap || 'best';
      },

      setAudioOnly(on) {
        this.downloadOptions.audio_only = !!on;
        // When audio-only is toggled on, the quality cap collapses to
        // 'audio' so the segmented pill reflects the same state. When
        // toggled off, drop back to 'best' so a height the user picked
        // earlier doesn't silently re-apply.
        if (on) {
          this.downloadOptions.quality_cap = null;
        }
        this._persistDownloadOptions();
      },

      setQualityCap(cap) {
        if (cap === 'audio') {
          this.downloadOptions.audio_only = true;
          this.downloadOptions.quality_cap = null;
        } else if (cap === 'best') {
          this.downloadOptions.audio_only = false;
          this.downloadOptions.quality_cap = null;
        } else {
          this.downloadOptions.audio_only = false;
          this.downloadOptions.quality_cap = cap;
        }
        this._persistDownloadOptions();
      },

      toggleSubLang(lang) {
        const cur = this.downloadOptions.subtitle_langs;
        if (lang === 'all') {
          // 'all' is exclusive — picking it clears any specific picks.
          this.downloadOptions.subtitle_langs = cur.includes('all') ? [] : ['all'];
        } else {
          if (cur.includes(lang)) {
            this.downloadOptions.subtitle_langs = cur.filter(x => x !== lang);
          } else {
            this.downloadOptions.subtitle_langs = cur
              .filter(x => x !== 'all')
              .concat([lang]);
          }
        }
        this._persistDownloadOptions();
      },

      resetDownloadOptions() {
        this.downloadOptions.audio_only = false;
        this.downloadOptions.quality_cap = null;
        this.downloadOptions.subtitle_langs = [];
        this._persistDownloadOptions();
      },

      // Build the per-item payload bits to ride along on JobBatchItem.
      // Returns an empty object when nothing is set so the wire payload
      // stays tight and existing tests on the legacy shape keep passing.
      _downloadOptionsPayload() {
        const out = {};
        if (this.downloadOptions.audio_only) out.audio_only = true;
        if (this.downloadOptions.quality_cap) {
          out.quality_cap = this.downloadOptions.quality_cap;
        }
        if ((this.downloadOptions.subtitle_langs || []).length) {
          out.subtitle_langs = this.downloadOptions.subtitle_langs.slice();
        }
        return out;
      },

      // --- Job control routes (CLAUDE.md §15 v0.7) ---

      canPause(j) { return j && j.status === 'running'; },
      canResume(j) { return j && j.status === 'paused'; },
      canCancel(j) {
        return j && j.status !== 'done' && j.status !== 'cancelled';
      },
      canRestart(j) {
        if (!j) return false;
        if (j.status === 'failed_permanent') return true;
        if (j.stalled) return true;
        return false;
      },
      hasJobControls(j) {
        return this.canPause(j) || this.canResume(j)
          || this.canCancel(j) || this.canRestart(j);
      },

      stalledChipText(j) {
        if (!j || !j.stalled) return '';
        const elapsed = (j.stalled && j.stalled.elapsed_s) || 0;
        return this.t('download.stalled.chip', { seconds: elapsed });
      },

      async pauseJob(id) { return this._postControl(id, 'pause'); },
      async resumeJob(id) { return this._postControl(id, 'resume'); },
      async cancelJob(id) { return this._postControl(id, 'cancel'); },
      async restartJob(id) { return this._postControl(id, 'restart'); },

      confirmCancel(id) {
        this.controlConfirm = { open: true, action: 'cancel', jobId: id, busy: false };
        this.$nextTick(() => this.refreshIcons());
      },
      confirmRestart(id) {
        this.controlConfirm = { open: true, action: 'restart', jobId: id, busy: false };
        this.$nextTick(() => this.refreshIcons());
      },
      closeControlConfirm() {
        this.controlConfirm = null;
      },
      async executeControlConfirm() {
        if (!this.controlConfirm || this.controlConfirm.busy) return;
        const { action, jobId } = this.controlConfirm;
        this.controlConfirm.busy = true;
        try {
          await this._postControl(jobId, action);
        } finally {
          this.controlConfirm = null;
        }
      },

      async _postControl(id, action) {
        try {
          const r = await fetch(`/api/jobs/${id}/${action}`, { method: 'POST' });
          if (!r.ok) {
            const detail = await r.text().catch(() => '');
            this.batchError = {
              headline: this.t('errors.unknown'),
              technical: `POST /api/jobs/${id}/${action} → HTTP ${r.status}\n${detail}`,
            };
            return false;
          }
          // Optimistic: SSE will reconcile authoritative state shortly.
          const idx = this.activeJobs.findIndex(j => j.id === id);
          if (idx >= 0) {
            const updated = await r.json().catch(() => null);
            if (updated && updated.status) {
              this.activeJobs[idx].status = updated.status;
            }
          }
          return true;
        } catch (e) {
          this.batchError = {
            headline: this.t('errors.unknown'),
            technical: `POST /api/jobs/${id}/${action} threw ${e}`,
          };
          return false;
        }
      },

      async refreshProfile() {
        try {
          const r = await fetch('/api/system/profile');
          if (r.ok) {
            const d = await r.json();
            // `effective.name` is the resolved profile the pipeline actually
            // uses, after app + per-case overrides. Trust it over the raw
            // app_settings.profile field, which can be missing on first run.
            const name = d.effective?.name;
            if (name === 'fast' || name === 'slow') this.profile = name;
          }
        } catch (_) { /* leave default */ }
      },

      async setProfile(next) {
        if (next !== 'slow' && next !== 'fast') return;
        if (this.profile === next) return;
        const prev = this.profile;
        this.profile = next; // optimistic — flip the pill instantly
        try {
          const r = await fetch('/api/system/profile', {
            method: 'PUT',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({profile: next}),
          });
          if (!r.ok) throw new Error('http ' + r.status);
        } catch (_) {
          this.profile = prev;
          this.showToast(this.t('simple.speed.error'));
        }
      },

      // --- Routing ---

      goto(route) {
        if (!ROUTES.includes(route)) route = 'home';
        window.location.hash = `#/${route}`;
      },

      parseHash() {
        const h = (window.location.hash || '#/').slice(2);
        const [route] = h.split('/');
        this.route = ROUTES.includes(route) ? route : 'home';
        if (this.route === 'settings') {
          this.refreshSystemVersion();
          this.refreshExtensionTokens();
        }
      },

      async refreshAll() {
        await this.refreshSystemVersion();
      },

      // --- Capture (jobs) ---

      subscribeJob(job_id) {
        if (this._jobSources[job_id]) return;
        const es = new EventSource(`/api/jobs/${job_id}/events`);
        this._jobSources[job_id] = es;

        const update = (mut) => {
          const idx = this.activeJobs.findIndex(j => j.id === job_id);
          if (idx < 0) return;
          this.activeJobs[idx] = { ...this.activeJobs[idx], ...mut };
        };

        es.addEventListener('status', e => {
          const data = safeJSON(e.data) || {};
          // Any successful event clears the SSE disconnect counter — the
          // socket is alive again.
          this._streamErrors[job_id] = 0;
          // Clear the lingering error banner once a retry kicks off — the
          // backend re-emits 'running' / 'classifying' / etc. on retry.
          const mut = { status: data.status, disconnected: false };
          if (data.status && !FAILED_STATUSES.has(data.status) && data.status !== 'retrying') {
            mut.error = null;
          }
          update(mut);
        });
        es.addEventListener('progress', e => {
          const data = safeJSON(e.data) || {};
          // Real progress clears the stalled chip even if the backend
          // didn't fire a separate 'progress' after 'stalled'. The runner
          // does emit one, but the UI shouldn't depend on the order.
          const mut = { progress_latest: data };
          const j = this.activeJobs.find(x => x.id === job_id);
          if (j && j.stalled) mut.stalled = false;
          update(mut);
          this._startAggTick();
        });
        // CLAUDE.md §15 v0.7 — distinct lifecycle events. The orchestrator
        // also emits a 'status' event, so these handlers focus on the
        // visual transitions (clearing local error/stalled state, closing
        // the channel on cancel) rather than the status flip.
        es.addEventListener('paused', e => {
          const data = safeJSON(e.data) || {};
          update({ status: data.status || 'paused' });
        });
        es.addEventListener('resumed', e => {
          const data = safeJSON(e.data) || {};
          update({ status: data.status || 'queued', stalled: false });
        });
        es.addEventListener('cancelled', e => {
          const data = safeJSON(e.data) || {};
          update({ status: data.status || 'cancelled', error: null, stalled: false });
          es.close(); delete this._jobSources[job_id];
        });
        es.addEventListener('restarted', e => {
          const data = safeJSON(e.data) || {};
          update({
            status: data.status || 'queued',
            error: null,
            progress_latest: null,
            stalled: false,
          });
        });
        es.addEventListener('stalled', e => {
          const data = safeJSON(e.data) || {};
          update({ stalled: { elapsed_s: data.elapsed_s || 0, since: Date.now() } });
        });
        es.addEventListener('classification', e => {
          const data = safeJSON(e.data) || {};
          update({ classification: data });
        });
        es.addEventListener('done', e => {
          const data = safeJSON(e.data) || {};
          update({ status: 'done', result: data, error: null });
          es.close(); delete this._jobSources[job_id];
          // Plan §U9: do NOT auto-dismiss. Slow-connection users step away
          // for hours; a card that disappears in 1.2s is a bug. The user
          // dismisses each card explicitly (or "Dismiss completed" on bulk).
          // Still refresh the recent-captures grid so the new capture lands.
          this.refreshRecentCaptures();
        });
        es.addEventListener('error', e => {
          const data = safeJSON(e.data) || {};
          // Plan §U5: transient errors keep the channel open; the
          // orchestrator will emit 'running' again when the retry fires.
          // Permanent / internal errors close the channel.
          if (data.severity === 'transient') {
            update({ status: 'retrying', error: data });
            return;
          }
          update({ status: 'failed_permanent', error: data });
          es.close(); delete this._jobSources[job_id];
        });
        // EventSource auto-reconnects on transport error; close it explicitly
        // after a final state to avoid noise. After repeated transport
        // errors with no successful event in between, surface a Reconnect
        // affordance so the user isn't staring at a phantom "running" card.
        es.onerror = () => {
          const j = this.activeJobs.find(x => x.id === job_id);
          if (!j) {
            es.close(); delete this._jobSources[job_id];
            return;
          }
          if (TERMINAL_STATUSES.has(j.status)) {
            es.close(); delete this._jobSources[job_id];
            return;
          }
          this._streamErrors[job_id] = (this._streamErrors[job_id] || 0) + 1;
          if (this._streamErrors[job_id] >= SSE_DISCONNECT_THRESHOLD && !j.disconnected) {
            update({ disconnected: true });
          }
        };
      },

      reconnectJob(job_id) {
        const src = this._jobSources[job_id];
        if (src) { src.close(); delete this._jobSources[job_id]; }
        this._streamErrors[job_id] = 0;
        const idx = this.activeJobs.findIndex(j => j.id === job_id);
        if (idx >= 0) {
          this.activeJobs[idx] = { ...this.activeJobs[idx], disconnected: false };
        }
        this.subscribeJob(job_id);
      },

      dismissJob(job_id) {
        const src = this._jobSources[job_id];
        if (src) { src.close(); delete this._jobSources[job_id]; }
        this.activeJobs = this.activeJobs.filter(j => j.id !== job_id);
      },

      dismissAllCompleted() {
        const stale = this.activeJobs.filter(j => TERMINAL_STATUSES.has(j.status));
        for (const j of stale) this.dismissJob(j.id);
      },

      hasDismissable() {
        return this.activeJobs.some(j => TERMINAL_STATUSES.has(j.status));
      },

      isRetrying(j)   { return j.status === 'retrying'; },
      isPaused(j)     { return j.status === 'paused'; },
      isFailed(j)     { return FAILED_STATUSES.has(j.status); },
      isCancelled(j)  { return j.status === 'cancelled'; },
      isTerminal(j)   { return TERMINAL_STATUSES.has(j.status); },

      retryWaitText(j) {
        // Compose the "next attempt in N minutes" chip for retrying jobs.
        const ts = j.error && j.error.next_retry_at;
        if (!ts) return '';
        const ms = new Date(ts).getTime() - Date.now();
        if (ms <= 0) return this.t('capture.retry.imminent');
        const mins = Math.round(ms / 60000);
        if (mins >= 60) {
          const hrs = Math.round(mins / 60);
          return this.t('capture.retry.in_hours', { count: hrs });
        }
        if (mins >= 1) {
          return this.t('capture.retry.in_minutes', { count: mins });
        }
        const secs = Math.max(1, Math.round(ms / 1000));
        return this.t('capture.retry.in_seconds', { count: secs });
      },

      attemptsText(j) {
        const n = j.error && j.error.attempts;
        if (!n) return '';
        return this.t('capture.retry.attempt', { count: n });
      },

      formatProgress(j) {
        // Legacy single-line formatter kept for any callers that still
        // expect a flat string. New UI uses progressLabel + progressBytes.
        const label = this.progressLabel(j);
        const bytes = this.progressBytes(j);
        if (label && bytes) return `${label} · ${bytes}`;
        return label || bytes;
      },

      progressBytes(j) {
        const p = j.progress_latest;
        if (!p) return '';
        if (p.downloaded_bytes != null && p.total_bytes) {
          const pct = Math.round((p.downloaded_bytes / p.total_bytes) * 100);
          return `${pct}% · ${formatBytes(p.downloaded_bytes)}/${formatBytes(p.total_bytes)}`;
        }
        return '';
      },

      progressLabel(j) {
        // Translate the sub-status sent by the backend (video stream,
        // audio stream, merging, ...) so the user can see *which* file is
        // being downloaded. Falls back to the generic "Downloading" so
        // older sessions / unknown sub-statuses still render something.
        const p = j.progress_latest;
        if (!p) return '';
        const sub = p.sub_status;
        if (sub) {
          const key = 'capture.progress.substatus.' + sub;
          const translated = this.t(key);
          if (translated && translated !== key) return translated;
        }
        return this.t('capture.status.downloading');
      },

      // --- Download visualizer ---
      // The aggregate widget and per-job progress bar both read these
      // helpers; the 1 Hz tick is started lazily from the SSE 'progress'
      // listener and self-stops when no jobs remain in the downloading phase.

      isDownloading(j) { return j.status === 'downloading'; },

      aggregateActive() {
        for (const j of this.activeJobs) if (j.status === 'downloading') return true;
        return false;
      },

      aggregateSpeed() {
        let sum = 0;
        for (const j of this.activeJobs) {
          if (j.status !== 'downloading') continue;
          const s = j.progress_latest && j.progress_latest.speed;
          if (s) sum += s;
        }
        return sum;
      },

      aggregateCount() {
        let n = 0;
        for (const j of this.activeJobs) if (j.status === 'downloading') n++;
        return n;
      },

      jobPct(j) {
        const p = j.progress_latest;
        if (!p || p.downloaded_bytes == null || !p.total_bytes) return null;
        return Math.max(0, Math.min(100, Math.round((p.downloaded_bytes / p.total_bytes) * 100)));
      },

      _startAggTick() {
        if (this._aggTickHandle) return;
        this._aggTickHandle = setInterval(() => this.tickAggregate(), 1000);
      },

      tickAggregate() {
        if (!this.aggregateActive()) {
          // Self-stop when nothing is downloading; the next 'progress' event
          // restarts the tick. Leaving _aggHistory in place is fine — the
          // widget is hidden, and the buffer caps at 30 anyway.
          if (this._aggTickHandle) { clearInterval(this._aggTickHandle); this._aggTickHandle = null; }
          return;
        }
        this._aggHistory.push(this.aggregateSpeed());
        if (this._aggHistory.length > 30) this._aggHistory.shift();
      },

      sparklinePoints(history, w, h) {
        const n = history ? history.length : 0;
        if (n < 2) {
          const y = (h / 2).toFixed(2);
          return `0,${y} ${w},${y}`;
        }
        let max = 1;
        for (let i = 0; i < n; i++) if (history[i] > max) max = history[i];
        const stepX = w / (n - 1);
        const out = new Array(n);
        for (let i = 0; i < n; i++) {
          const x = (i * stepX).toFixed(2);
          const y = (h - (history[i] / max) * h).toFixed(2);
          out[i] = `${x},${y}`;
        }
        return out.join(' ');
      },

      formatSpeed(bps) {
        if (!bps) return '—';
        return `${formatBytes(bps)}${this.t('capture.progress.speed_suffix')}`;
      },

      formatEta(seconds) {
        if (seconds == null || seconds < 0 || !isFinite(seconds)) return '';
        if (seconds < 60) return this.t('capture.progress.eta_seconds', { n: Math.max(1, Math.round(seconds)) });
        if (seconds < 3600) return this.t('capture.progress.eta_minutes', { n: Math.round(seconds / 60) });
        return this.t('capture.progress.eta_hours', { n: Math.round(seconds / 3600) });
      },

      phaseIcon(phase) {
        return { page: 'globe', media: 'download-cloud', hash: 'hash', sign: 'shield-check' }[phase];
      },

      phaseIndex(phase) {
        return { page: 1, media: 2, hash: 3, sign: 4 }[phase];
      },

      phaseDone(j, phase) {
        const idx = PHASE_BY_STATUS[j.status] ?? 0;
        return idx >= this.phaseIndex(phase);
      },

      phaseClass(j, phase) {
        if (FAILED_STATUSES.has(j.status) && this.phaseIndex(phase) > (PHASE_BY_STATUS.snapshotting)) {
          return 'bg-rose-100 text-rose-600 dark:bg-rose-950/40';
        }
        // Retrying / paused live above the queue but not yet at a phase —
        // show the previously reached phases as completed and the rest as
        // muted; the inline retry banner conveys the "we're waiting" state.
        if (this.phaseDone(j, phase)) {
          const idx = PHASE_BY_STATUS[j.status] ?? 0;
          if (idx === this.phaseIndex(phase) && j.status !== 'done') {
            return 'bg-accent text-white shadow-sm ring-2 ring-accent ring-offset-2 ring-offset-white dark:ring-offset-zinc-900 motion-safe:animate-pulse';
          }
          return 'bg-accent text-white shadow-sm';
        }
        return 'bg-zinc-100 dark:bg-zinc-800 text-zinc-400';
      },

      // --- Downloader ---

      parsedSimpleUrls() {
        return this.simpleUrls.split('\n').map(s => s.trim()).filter(Boolean);
      },

      async submitSimple() {
        if (!this.simpleUrl) return;
        const url = this.simpleUrl;
        this.simpleUrl = '';
        await this._postBatch([url]);
      },

      async submitSimpleBatch() {
        const urls = this.parsedSimpleUrls();
        if (!urls.length) return;
        this.simpleUrls = '';
        await this._postBatch(urls);
      },

      async _postBatch(urls) {
        // Preflight-first flow (CLAUDE.md §15). Step 1: classify and
        // dedup-probe every URL before running the capture pipeline.
        this.batchError = null;
        this.batchSummary = null;
        let preflight;
        try {
          const r = await fetch('/api/jobs/preflight', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({ urls, case_id: this.quickCaseId || undefined }),
          });
          if (!r.ok) {
            this.batchError = {
              headline: this.t('errors.batch_submit_failed.headline'),
              technical: `HTTP ${r.status}: preflight failed`,
            };
            return;
          }
          preflight = await r.json();
        } catch (err) {
          this.batchError = {
            headline: this.t('errors.batch_submit_failed.headline'),
            technical: String(err),
          };
          return;
        }
        this.quickCaseId = preflight.case_id;
        this.batchSummary = {
          new: preflight.summary.new,
          dup: preflight.summary.duplicates_blocked,
          ib: preflight.summary.within_batch_duplicates,
          failed: preflight.summary.classification_failed,
        };

        // Step 2: split results into "submit now" (new) and "ask the
        // user" (duplicate). Within-batch and classification_failed
        // entries surface in the summary chip but don't queue a modal —
        // they're either already-collapsed or unsubmittable.
        const items = [];
        const duplicateQueue = [];
        // CLAUDE.md §15 v0.7: ride the per-submission download options on
        // every JobBatchItem. Empty payload when nothing is set.
        const opts = this._downloadOptionsPayload();
        for (const r of (preflight.results || [])) {
          if (r.status === 'new') {
            items.push({ url: r.url_submitted, ...opts });
          } else if (r.status === 'duplicate') {
            duplicateQueue.push(r);
          }
        }

        if (duplicateQueue.length > 0) {
          // Open the §15 modal; the user resolves each duplicate; on the
          // last decision we call _submitItems with whatever they kept.
          this.duplicateModal = {
            queue: duplicateQueue,
            index: 0,
            pending: items,
            lang: this.locale,
            current: duplicateQueue[0],
          };
          await this.$nextTick();
          this.refreshIcons();
          return;
        }

        await this._submitItems(items);
      },

      // Submit the resolved item list to /api/jobs/batch. ``items`` is
      // ``[{url, force_recapture?, original_download_id?}]`` per the §15
      // shape. Caller has already shown the user the summary chip.
      async _submitItems(items) {
        if (!items.length) {
          await this.$nextTick();
          this.refreshIcons();
          return;
        }
        let res;
        try {
          res = await fetch('/api/jobs/batch', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({
              items,
              case_id: this.quickCaseId || undefined,
              lang: this.locale,
            }),
          });
        } catch (err) {
          this.batchError = {
            headline: this.t('errors.batch_submit_failed.headline'),
            technical: String(err),
          };
          return;
        }
        if (!res.ok) {
          let detail = res.statusText || '';
          try {
            const errBody = await res.json();
            if (errBody && errBody.detail !== undefined) {
              detail = typeof errBody.detail === 'string'
                ? errBody.detail
                : JSON.stringify(errBody.detail);
            }
          } catch (_) { /* not JSON; keep statusText */ }
          this.batchError = {
            headline: this.t('errors.batch_submit_failed.headline'),
            technical: `HTTP ${res.status}: ${detail}`,
          };
          return;
        }
        const body = await res.json();
        this.quickCaseId = body.case_id;
        for (const job of (body.jobs || [])) {
          this.activeJobs.push(job);
          this.subscribeJob(job.id);
        }
        await this.$nextTick();
        this.refreshIcons();
      },

      batchSummaryText() {
        if (!this.batchSummary) return '';
        const parts = [
          this.t('duplicate.batch.summary.new', {count: this.batchSummary.new}),
        ];
        if (this.batchSummary.dup > 0) {
          parts.push(this.t('duplicate.batch.summary.dup', {count: this.batchSummary.dup}));
        }
        if (this.batchSummary.ib > 0) {
          parts.push(this.t('duplicate.batch.summary.ib', {count: this.batchSummary.ib}));
        }
        return parts.join(' · ');
      },

      // ---- §15 duplicate-handling modal --------------------------------

      // Apply the user's choice for the current duplicate, then advance
      // the queue or — once all duplicates are resolved — submit the
      // accumulated items list.
      async duplicateOutcome(choice) {
        if (!this.duplicateModal) return;
        const m = this.duplicateModal;
        const dup = m.current;
        const existingId = dup.existing && dup.existing.id;
        const caseId = this.quickCaseId;
        if (choice === 'opened_existing') {
          // Audit the choice, then ask the backend to reveal the folder.
          await this._auditDuplicateOutcome(caseId, existingId, 'opened_existing');
          if (dup.existing && dup.existing.item_dir) {
            try {
              await fetch('/api/system/reveal', {
                method: 'POST',
                headers: {'content-type': 'application/json'},
                body: JSON.stringify({ relative_path: dup.existing.item_dir }),
              });
            } catch (_) { /* best-effort */ }
          }
        } else if (choice === 'recapture') {
          // Append a forced-re-capture item to the pending list. The
          // backend will suffix the url_hash with __c{N+1}. Carries the
          // current download options too — the user may have toggled
          // audio-only between the original capture and the re-capture.
          m.pending.push({
            url: dup.url_submitted,
            force_recapture: true,
            original_download_id: existingId,
            ...this._downloadOptionsPayload(),
          });
        } else if (choice === 'cancelled') {
          await this._auditDuplicateOutcome(caseId, existingId, 'cancelled');
        }

        // Advance.
        const nextIdx = m.index + 1;
        if (nextIdx < m.queue.length) {
          this.duplicateModal = {
            ...m,
            index: nextIdx,
            current: m.queue[nextIdx],
          };
          await this.$nextTick();
          this.refreshIcons();
          return;
        }
        // Done — submit any accumulated items (new + forced re-captures).
        const pending = m.pending;
        this.duplicateModal = null;
        await this._submitItems(pending);
      },

      async _auditDuplicateOutcome(caseId, existingId, outcome) {
        if (!caseId || !existingId) return;
        try {
          await fetch('/api/jobs/duplicate-outcome', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({
              case_id: caseId, existing_id: existingId, outcome,
            }),
          });
        } catch (_) { /* best-effort audit; UI must keep moving */ }
      },

      dismissDuplicate() {
        // Esc treated as Cancel for the *current* duplicate.
        if (this.duplicateModal) this.duplicateOutcome('cancelled');
      },

      async copyBatchErrorTechnical() {
        const t = this.batchError && this.batchError.technical;
        if (!t) return;
        try { await navigator.clipboard.writeText(t); } catch (_) { /* ignore */ }
      },

      // --- Per-job error card helpers (CLAUDE.md §4.7) ----------------
      // The orchestrator emits an enriched ``error`` payload on permanent
      // failure: { i18n_key, phase, exc_type, detail, cause_i18n_key,
      // suggested_action_i18n_key, [stderr_tail, returncode, severity] }.
      // These helpers translate that into the four UI affordances:
      // headline / cause / action / technical-details.

      // Phase string the orchestrator was in when the failure happened.
      // Falls back to 'unknown' so the i18n key always resolves.
      jobErrorPhase(j) {
        const known = new Set(['classifying', 'snapshotting', 'downloading', 'finalizing']);
        const p = j && j.error && j.error.phase;
        return known.has(p) ? p : 'unknown';
      },

      // Phase-aware headline. For internal failures (errors.unknown +
      // a captured phase), prefer "Capture failed during {phase}." over
      // the generic "Something went wrong." so the investigator sees
      // *which step* broke. For backend-classified errors (yt-dlp etc.)
      // the orchestrator already supplies a domain-specific i18n_key —
      // keep that.
      jobErrorHeadline(j) {
        if (!j || !j.error) return this.t('errors.unknown');
        const key = j.error.i18n_key || 'errors.unknown';
        if (key === 'errors.unknown' && j.error.phase) {
          const phaseLabel = this.t('errors.phase.' + this.jobErrorPhase(j));
          return this.t('errors.unknown_during', { phase: phaseLabel });
        }
        return this.t(key);
      },

      // Suggested-action button click. Routes by action key:
      //   - rebuild_image  → open the README's troubleshooting section
      //   - try_again      → re-submit the job's URL
      //   - check_mounts   → open the README's troubleshooting section
      //   - check_update   → open Settings (handled by route hash)
      //   - add_cookies    → open Settings (handled by route hash)
      //   - default        → re-submit the job's URL
      jobErrorAction(j) {
        const action = j && j.error && j.error.suggested_action_i18n_key;
        const url = j && j.url;
        if (action === 'errors.action.rebuild_image' || action === 'errors.action.check_mounts') {
          // Open the README troubleshooting anchor in a new tab. README is
          // bundled in the repo; the dist launcher copies it next to the
          // image, but the GitHub link is a stable fallback.
          window.open('https://github.com/bhngyn/ytdlp/blob/main/README.md#troubleshooting', '_blank', 'noopener');
          return;
        }
        if (action === 'errors.action.check_update' || action === 'errors.action.add_cookies') {
          this.route = 'settings';
          window.location.hash = 'settings';
          return;
        }
        // Default: restart the same job in place. CLAUDE.md §15 v0.7
        // routes the failed-job retry through the orchestrator's
        // restart() so the audit trail is a clean ``job.restarted``
        // (with restart_count++) instead of a fresh ``job.created`` —
        // the latter would lose the connection to the original failure.
        if (j && j.id) {
          this.restartJob(j.id);
          return;
        }
        if (!url) return;
        this._postBatch([url]);
      },

      // Multi-line technical detail for the <details> expander. Joins the
      // most useful fields the backend provides — exc_type+detail for
      // internal failures, stderr_tail for yt-dlp failures. Order matters:
      // exc_type first so investigators paste the most actionable line.
      jobErrorTechnical(j) {
        if (!j || !j.error) return '';
        const parts = [];
        if (j.error.detail) parts.push(j.error.detail);
        else if (j.error.exc_type) parts.push(j.error.exc_type);
        if (j.error.returncode != null) parts.push(`exit code: ${j.error.returncode}`);
        if (j.error.stderr_tail) parts.push(j.error.stderr_tail);
        return parts.join('\n\n');
      },

      async copyJobErrorTechnical(job_id) {
        const j = this.activeJobs.find(x => x.id === job_id);
        const text = this.jobErrorTechnical(j);
        if (!text) return;
        try { await navigator.clipboard.writeText(text); } catch (_) { /* ignore */ }
      },

      async refreshRecentCaptures() {
        if (!this.quickCaseId) {
          // Resolve the default-case slug from /api/system/version so legacy
          // installs (slug: 'quick-captures') and fresh installs (slug:
          // 'downloads') both work without a frontend code change.
          const defaultSlug = this.systemVersion?.default_case_slug;
          if (!defaultSlug) { this.recentCaptures = []; return; }
          try {
            const res = await fetch('/api/cases');
            if (res.ok) {
              const body = await res.json();
              const c = (body.cases || []).find(x => x.slug === defaultSlug);
              if (c) {
                this.quickCaseId = c.id;
                this.quickCaseSlug = c.slug;
                this.quickCaseName = c.name;
              }
            }
          } catch (_) { /* ignore — empty grid will render */ }
        }
        if (!this.quickCaseId) { this.recentCaptures = []; return; }
        try {
          const res = await fetch(`/api/library?case_id=${this.quickCaseId}&limit=12`);
          if (res.ok) {
            const body = await res.json();
            this.recentCaptures = body.items || [];
          }
        } catch (_) { /* ignore */ }
        await this.$nextTick();
        this.refreshIcons();
      },

      // ---- Recent-captures list helpers (CLAUDE.md §15 plan §H) --------

      // Extract the host portion of the source URL for the secondary
      // line of each list row. Falls back to the raw URL if URL parsing
      // fails (rare — backend stores valid URLs).
      recentRowHost(item) {
        const raw = item.source_url || item.final_url || '';
        try { return new URL(raw).host; }
        catch (_) { return raw; }
      },

      // Capture-kind → Lucide icon name. Three known kinds today:
      // ``media`` (yt-dlp video), ``gallery`` (gallery-dl images,
      // CLAUDE.md §15 v0.5), ``page_only`` (page snapshot only).
      // Unknown kinds fall back to the layout-template icon.
      captureKindIcon(kind) {
        if (kind === 'media') return 'film';
        if (kind === 'gallery') return 'images';
        return 'layout-template';
      },

      // Capture-kind → i18n key for the chip label. Same three-way split
      // as captureKindIcon. ``recent.row.kind.gallery`` is rendered with
      // an ICU plural so locales with rich plural rules (Arabic) can pick
      // the right form for "{count} images".
      captureKindLabel(kind) {
        if (kind === 'media') return 'recent.row.kind.media';
        if (kind === 'gallery') return 'recent.row.kind.gallery';
        return 'recent.row.kind.page_only';
      },

      async revealRecentItem(item) {
        if (!item || !item.item_dir) return;
        try {
          const res = await fetch('/api/system/reveal', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({ relative_path: item.item_dir }),
          });
          const body = await res.json().catch(() => ({}));
          if (body && body.ok) return;
        } catch (_) { /* fall through */ }
        // Best-effort fallback: copy the host-side path so the user can
        // paste it into Finder/Explorer.
        const paths = this.systemVersion?.paths || {};
        const root = paths.host_default_case_dir || paths.default_case_dir || '';
        if (root) await this.copyPath(root + '/' + item.item_dir.split('/').slice(1).join('/'));
      },

      // ---- Clear-list flow (plan §I) ----------------------------------

      // Build a human-readable byte estimate for the dialog. Sums
      // ``file_size_bytes`` across recent captures (a lower bound — the
      // sidecars/PDFs aren't included, but the order of magnitude is
      // right and the backend reports the exact freed_bytes after).
      _formatBytes(n) {
        if (!n || n < 0) return '0 B';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0; let v = n;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
      },

      openClearDialog() {
        if (!this.recentCaptures.length) return;
        const total = this.recentCaptures.reduce(
          (s, it) => s + (Number(it.file_size_bytes) || 0), 0,
        );
        this.clearDialog = {
          open: true,
          count: this.recentCaptures.length,
          case_name: this.quickCaseName || this.quickCaseSlug || '',
          freed_bytes_human: this._formatBytes(total),
        };
      },

      closeClearDialog() {
        if (this.clearInProgress) return;
        this.clearDialog = null;
      },

      async confirmClear() {
        if (!this.clearDialog || !this.quickCaseId || this.clearInProgress) return;
        this.clearInProgress = true;
        try {
          const res = await fetch(`/api/cases/${this.quickCaseId}/clear`, {
            method: 'POST',
            headers: {'content-type': 'application/json'},
          });
          if (!res.ok) {
            this.batchError = {
              headline: this.t('recent.clear.error'),
              technical: `HTTP ${res.status}`,
            };
            return;
          }
          const body = await res.json();
          this.recentCaptures = [];
          this.showToast(
            this.t('recent.clear.snackbar', {
              count: body.deleted_count,
              freed_bytes: this._formatBytes(body.freed_bytes),
            }),
          );
        } catch (err) {
          this.batchError = {
            headline: this.t('recent.clear.error'),
            technical: String(err),
          };
        } finally {
          this.clearInProgress = false;
          this.clearDialog = null;
          await this.$nextTick();
          this.refreshIcons();
        }
      },

      async exportThenClear() {
        if (!this.quickCaseId) return;
        // Trigger the case-export download in a separate tab so the
        // dialog stays open. Recipient gets a signed bundle they can
        // verify offline before the user blows away the originals.
        const url = `/api/cases/${this.quickCaseId}/export?lang=${encodeURIComponent(this.locale)}`;
        try { window.open(url, '_blank'); }
        catch (_) { /* popup blocked — leave the dialog open */ }
      },

      async copyPath(p) {
        if (!p) return;
        try { await navigator.clipboard.writeText(p); }
        catch (_) { /* older browsers — ignore */ }
        this.pathCopied = true;
        setTimeout(() => {
          this.pathCopied = false;
          this.refreshIcons();
        }, 1200);
        await this.$nextTick();
        this.refreshIcons();
      },

      async revealQuickFolder() {
        // Backend may or may not be able to spawn a file manager. Try it;
        // on failure, degrade gracefully to copy-to-clipboard. Inside Docker
        // (Linux, no DISPLAY) reveal is impossible — the launcher passes the
        // host-side path via CAPSULE_HOST_DOWNLOADS_DIR so we can copy
        // something the user can paste into Finder/Explorer.
        const paths = this.systemVersion?.paths || {};
        const path = paths.host_default_case_dir || paths.default_case_dir;
        const defaultSlug = this.systemVersion?.default_case_slug;
        if (!defaultSlug) { await this.copyPath(path); return; }
        try {
          const res = await fetch('/api/system/reveal', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({ relative_path: defaultSlug }),
          });
          const body = await res.json().catch(() => ({}));
          if (body && body.ok) return;
        } catch (_) { /* fall through */ }
        await this.copyPath(path);
      },

      formatNumber(n) {
        try { return new Intl.NumberFormat(this.locale).format(n); }
        catch (_) { return String(n); }
      },

      showToast(message, ms = 2400) {
        this.toast = message;
        if (this._toastTimer) clearTimeout(this._toastTimer);
        this._toastTimer = setTimeout(() => { this.toast = ''; }, ms);
      },

      // --- Settings / system ---

      async refreshSystemVersion() {
        try {
          const res = await fetch('/api/system/version');
          if (res.ok) this.systemVersion = await res.json();
        } catch (_) {}
      },

      async checkForUpdates() {
        this.updating = true; this.updateResult = null;
        try {
          const res = await fetch('/api/system/update', { method: 'POST' });
          const body = await res.json();
          this.updateResult = body.ok ? `OK · yt-dlp ${body.new_version}` : `Failed (${body.returncode})`;
          await this.refreshSystemVersion();
        } catch (e) {
          this.updateResult = String(e);
        } finally {
          this.updating = false;
        }
      },

      // --- Browser extension pairing ---

      async refreshExtensionTokens() {
        try {
          const res = await fetch('/api/extension/tokens');
          if (res.ok) {
            const body = await res.json();
            this.extension.tokens = body.tokens || [];
          }
        } catch (e) {
          // Non-fatal — Settings card just shows the empty state.
        }
      },

      async pairExtension() {
        // Default label uses the host's display name when we have one.
        const label = (window.prompt(
          this.t('settings.extension.pair_prompt'),
          this.t('settings.extension.default_label')
        ) || '').trim();
        if (!label) return;
        this.extension.pairing = true;
        this.extension.justIssued = null;
        try {
          const res = await fetch('/api/extension/pair', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label }),
          });
          if (!res.ok) throw new Error(`pair failed: ${res.status}`);
          this.extension.justIssued = await res.json();
          await this.refreshExtensionTokens();
        } catch (e) {
          this.showToast(String(e));
        } finally {
          this.extension.pairing = false;
        }
      },

      async copyExtensionToken() {
        const tok = this.extension.justIssued?.token;
        if (!tok) return;
        try {
          await navigator.clipboard.writeText(tok);
          this.showToast(this.t('settings.extension.copied'));
        } catch (e) {
          this.showToast(String(e));
        }
      },

      async revokeExtensionToken(id) {
        const ok = window.confirm(this.t('settings.extension.revoke_confirm'));
        if (!ok) return;
        try {
          const res = await fetch(`/api/extension/pair/${encodeURIComponent(id)}`, { method: 'DELETE' });
          if (!res.ok) throw new Error(`revoke failed: ${res.status}`);
          this.extension.justIssued = null;
          await this.refreshExtensionTokens();
          this.showToast(this.t('settings.extension.revoked_toast'));
        } catch (e) {
          this.showToast(String(e));
        }
      },

      // --- i18n ---

      async loadBundle(lang) {
        const res = await fetch(`/api/i18n/${encodeURIComponent(lang)}`);
        if (!res.ok) throw new Error(`bundle ${lang}: ${res.status}`);
        const payload = await res.json();
        this.messages = payload.messages || {};
        this.direction = payload.dir || 'ltr';
        this._formatters = {};
      },

      t(key, args) {
        const tmpl = this.messages[key];
        if (tmpl == null) return key;
        if (!args && tmpl.indexOf('{') < 0) return tmpl;
        try {
          let f = this._formatters[key];
          if (!f) {
            // The IIFE bundle exposes IntlMessageFormat as a namespace object
            // whose `.IntlMessageFormat` (or `.default`) is the constructor.
            const Ctor = (typeof IntlMessageFormat === 'function')
              ? IntlMessageFormat
              : (IntlMessageFormat.IntlMessageFormat || IntlMessageFormat.default);
            f = new Ctor(tmpl, this.locale);
            this._formatters[key] = f;
          }
          return f.format(args || {});
        } catch (err) {
          console.warn('[i18n]', key, err);
          return tmpl;
        }
      },

      relTime(iso) {
        return relTime(iso, this.locale);
      },

      renderAll() {
        document.querySelectorAll('[data-t]').forEach(el => {
          const key = el.getAttribute('data-t');
          const argsAttr = el.getAttribute('data-t-args');
          const args = argsAttr ? safeJSON(argsAttr) : null;
          el.textContent = this.t(key, args);
        });
        document.querySelectorAll('[data-t-placeholder]').forEach(el => {
          el.setAttribute('placeholder', this.t(el.getAttribute('data-t-placeholder')));
        });
        document.querySelectorAll('[data-t-aria]').forEach(el => {
          el.setAttribute('aria-label', this.t(el.getAttribute('data-t-aria')));
        });
        document.title = `${this.t('app.name')} — ${this.t('app.tagline')}`;
      },

      applyHtmlAttrs() {
        document.documentElement.setAttribute('lang', this.locale);
        document.documentElement.setAttribute('dir', this.direction);
      },

      refreshIcons() {
        if (window.lucide) window.lucide.createIcons();
      },

      async cycleLang() {
        const idx = LANGS.indexOf(this.locale);
        await this.setLang(LANGS[(idx + 1) % LANGS.length]);
      },

      async setLang(lang) {
        if (!LANGS.includes(lang)) return;
        this.locale = lang;
        writePref('capsule.lang', lang);
        await this.loadBundle(lang);
        this.renderAll();
        this.applyHtmlAttrs();
        this.refreshIcons();
      },
    };
  };
})();
