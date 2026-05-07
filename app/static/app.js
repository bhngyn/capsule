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

  function relTime(iso) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return iso;
    const delta = Math.floor((Date.now() - t) / 1000);
    if (delta < 60) return 'just now';
    if (delta < 3600) return `${Math.floor(delta / 60)}m`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h`;
    return `${Math.floor(delta / 86400)}d`;
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
      pathCopied: false,

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
        this.parseHash();
        window.addEventListener('hashchange', () => this.parseHash());
        await this.refreshAll();
        await this.refreshProfile();
        await this.refreshRecentCaptures();
        this.renderAll();
        this.refreshIcons();
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
          // Clear the lingering error banner once a retry kicks off — the
          // backend re-emits 'running' / 'classifying' / etc. on retry.
          const mut = { status: data.status };
          if (data.status && !FAILED_STATUSES.has(data.status) && data.status !== 'retrying') {
            mut.error = null;
          }
          update(mut);
        });
        es.addEventListener('progress', e => {
          const data = safeJSON(e.data) || {};
          update({ progress_latest: data });
          this._startAggTick();
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
        // after a final state to avoid noise.
        es.onerror = () => {
          // Don't tear down a still-running stream just because the browser
          // briefly lost its socket — only close if the job has reached a
          // terminal state.
          const j = this.activeJobs.find(x => x.id === job_id);
          if (j && TERMINAL_STATUSES.has(j.status)) {
            es.close(); delete this._jobSources[job_id];
          }
        };
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
        const res = await fetch('/api/jobs/batch', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({ urls }),
        });
        if (!res.ok) {
          alert(this.t('common.error') + ': ' + res.statusText);
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

      async refreshRecentCaptures() {
        if (!this.quickCaseId) {
          try {
            const res = await fetch('/api/cases');
            if (res.ok) {
              const body = await res.json();
              const c = (body.cases || []).find(x => x.slug === 'quick-captures');
              if (c) this.quickCaseId = c.id;
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
        const path = paths.host_quick_captures_dir || paths.quick_captures_dir;
        try {
          const res = await fetch('/api/system/reveal', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({ relative_path: 'quick-captures' }),
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

      relTime,

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
