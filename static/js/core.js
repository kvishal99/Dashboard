/* Shared helpers. Loaded on every page, before anything else.
 *
 * No framework and no build step: the whole front end is a handful of small
 * modules on the page, which is what keeps deployment a file copy. What would
 * normally come from a component library lives here instead - formatting,
 * badges, polling, the slide-over - so pages stay declarative and never
 * hand-roll the same widget twice.
 */

/* ------------------------------------------------------------- fetching */

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    // FastAPI puts the readable reason in `detail`. Surfacing it instead of
    // "500 Internal Server Error" is the difference between a usable message
    // and a shrug.
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* not JSON - keep the status line */ }
    throw new Error(detail);
  }
  return res.json();
}

async function sendJSON(url, method = 'POST', body = null) {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const parsed = await res.json();
      if (parsed && parsed.detail) detail = parsed.detail;
    } catch (_) { /* keep the status line */ }
    throw new Error(detail);
  }
  return res.json();
}

const postJSON = (url, body) => sendJSON(url, 'POST', body);

/* ---------------------------------------------------------- formatting */

/* Indian grouping (1,00,000), which is how these counts get quoted here. */
function fmtNum(n) {
  if (n === null || n === undefined || n === '') return '—';
  return Number(n).toLocaleString('en-IN');
}

/* Shorthand under an exact figure: 1,00,000 -> "1 L". */
function fmtShort(n) {
  if (n === null || n === undefined) return '';
  if (n >= 1e7) return `${(n / 1e7).toFixed(2).replace(/\.?0+$/, '')} Cr`;
  if (n >= 1e5) return `${(n / 1e5).toFixed(2).replace(/\.?0+$/, '')} L`;
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')} K`;
  return '';
}

function fmtPct(n, digits = 1) {
  if (n === null || n === undefined) return '—';
  return `${Number(n).toFixed(digits)}%`;
}

function fmtBytes(n) {
  if (!n) return '0 B';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function timeAgo(ts) {
  if (!ts) return 'never';
  const secs = Math.max(0, Date.now() / 1000 - ts);
  if (secs < 60) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function timeUntil(ts) {
  if (!ts) return '—';
  const secs = ts - Date.now() / 1000;
  if (secs <= 0) return 'due now';
  if (secs < 60) return `in ${Math.round(secs)}s`;
  if (secs < 3600) return `in ${Math.round(secs / 60)}m`;
  return `in ${Math.round(secs / 3600)}h`;
}

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

function fmtDate(value) {
  if (!value) return '—';
  return value;
}

/* Everything interpolated into innerHTML goes through this. Partner names,
 * error strings and cron commands all reach the page from outside. */
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* --------------------------------------------------------------- badges */

/* Every status the dashboard can show, and what it means in words.
 *
 * One definition used twice: as the badge's hover title, and as the printed
 * legend under the table. Grouped by column, because the same word means
 * different things in different places - NOT CHECKED under Website health means
 * no HTTP check has run, while under Partners it means no COUNT query has.
 *
 * Each entry is [badge class, label, meaning], worst first - the same order the
 * tables sort in, and the order the badge rules are tested in.
 */
const STATUS_HELP = {
  partner: [
    ['bad', 'FAILED', 'At least one critical issue: the count query errored, nothing is live, the ingest job has stalled, or a large share of the spreadsheet is missing from the database.'],
    ['warn', 'WARNING', 'Something is degraded but not broken - the ingest is late, half the records never published, or the database holds rows the spreadsheet does not.'],
    ['running', 'RUNNING', 'A count is being collected for this partner right now.'],
    ['ok', 'SUCCESS', 'No open issues. Counts collected, the ingest job is inside its expected interval, and records are live.'],
  ],
  sites: [
    ['bad', 'DOWN', 'The last check failed - unexpected status code, timeout, DNS failure or TLS error. Two failures in a row emails the alert recipients.'],
    ['idle', 'NOT CHECKED', 'No check has completed for this site yet.'],
    ['ok', 'UP', 'The site answered within its timeout with a status code config.yaml expects for it.'],
  ],
  jobs: [
    ['bad', 'STALLED', 'Nothing new inserted for more than twice the interval its frequency implies. The ingest job is very likely broken.'],
    ['bad', 'NEVER RAN', 'Nothing has ever been inserted for this partner.'],
    ['warn', 'LATE', 'Overdue, but by less than twice its interval - a weekly job a couple of days behind reads as late rather than broken.'],
    ['idle', 'NO SCHEDULE', 'The status sheet gives no frequency, so there is nothing to measure freshness against.'],
    ['idle', 'DORMANT', 'No schedule, 20 or fewer live records, and untouched for over a year. A one-off import, not a running job.'],
    ['idle', 'RETIRED', 'The sheet says this partner was switched off deliberately, so stale data is not a fault.'],
    ['ok', 'RUNNING', 'The newest record was inserted within the interval its frequency implies.'],
  ],
  cron_entry: [
    ['bad', 'NO CRON', "This partner's server did report its crontab, and no line for this partner appears in it."],
    ['warn', 'COMMENTED OUT', 'A crontab line exists but is commented out, so cron never runs it.'],
    ['idle', 'NOT COLLECTED', "No crontab has been collected from this partner's server, so a missing line proves nothing."],
    ['ok', 'SCHEDULED', 'An active crontab line was found on a server that reports its crontab.'],
  ],
  cron: [
    ['idle', 'DISABLED', 'Commented out in the crontab, so cron never runs it. Kept visible because a job switched off by accident looks identical to one switched off on purpose.'],
    ['ok', 'ACTIVE', 'A live crontab line - cron runs this on the schedule shown.'],
  ],
  processes: [
    ['bad', 'OFFLINE', "Server: no heartbeat from this server's agent recently, so its process list is stale. Usually the agent or its tunnel is down, not the box."],
    ['bad', 'ERRORED', 'Process: PM2 reports this process as errored - it crashed or failed to start.'],
    ['warn', 'STOPPED', "Process: any other PM2 state, shown in PM2's own words."],
    ['ok', 'LIVE', "Server: this server's agent reported within the staleness window."],
    ['ok', 'ONLINE', 'Process: PM2 reports this process as running.'],
  ],
  compare: [
    ['ok', 'MATCHING', 'Present in both the partner spreadsheet and our database.'],
    ['bad', 'MISSING', 'In the spreadsheet, with no matching record in our database - the ingest never took it, or took it under a different identity.'],
    ['warn', 'EXTRA', 'In our database, with no row in the spreadsheet - usually a tour the partner withdrew that was never removed on our side.'],
  ],
  history: [
    ['bad', 'FAILED', 'That collection errored - the counts for that timestamp were never recorded.'],
    ['ok', 'OK', 'That collection succeeded.'],
  ],
};

function statusHelp(group, label) {
  const row = (STATUS_HELP[group] || []).find((r) => r[1] === label);
  return row ? row[2] : '';
}

function badge(cls, label, group, help) {
  // `help` is for labels that vary at runtime - PM2 reports its own words for
  // any state that isn't online/errored, so those can't be looked up by label.
  const text = help || (group ? statusHelp(group, label) : '');
  return `<span class="badge ${cls}"${text ? ` title="${esc(text)}"` : ''}>${esc(label)}</span>`;
}

/* The partner card / row status, which the server computes from its issues. */
const STATUS_BADGE = {
  success: ['ok', 'SUCCESS'],
  warning: ['warn', 'WARNING'],
  failed: ['bad', 'FAILED'],
  running: ['running', 'RUNNING'],
};

function partnerStatusBadge(status) {
  const [cls, label] = STATUS_BADGE[status] || ['idle', 'UNKNOWN'];
  return badge(cls, label, 'partner');
}

const JOB_BADGE = {
  ok: ['ok', 'RUNNING'], late: ['warn', 'LATE'], stalled: ['bad', 'STALLED'],
  never: ['bad', 'NEVER RAN'], unknown: ['idle', 'NO SCHEDULE'],
  dormant: ['idle', 'DORMANT'], retired: ['idle', 'RETIRED'],
};

function jobBadge(state) {
  const [cls, label] = JOB_BADGE[state] || ['idle', 'NO SCHEDULE'];
  return badge(cls, label, 'jobs');
}

const CRON_BADGE = {
  found: ['ok', 'SCHEDULED'], disabled: ['warn', 'COMMENTED OUT'],
  missing: ['bad', 'NO CRON'], unknown: ['idle', 'NOT COLLECTED'],
};

function cronBadge(status) {
  const [cls, label] = CRON_BADGE[status] || ['idle', 'NOT COLLECTED'];
  return badge(cls, label, 'cron_entry');
}

const SEVERITY_BADGE = {
  critical: ['bad', 'CRITICAL'], warning: ['warn', 'WARNING'], info: ['info', 'INFO'],
};

function severityBadge(severity) {
  const [cls, label] = SEVERITY_BADGE[severity] || ['idle', 'UNKNOWN'];
  return badge(cls, label);
}

function statusLegend(groups, summary) {
  const rows = [].concat(groups).flatMap((g) => (STATUS_HELP[g] || []));
  if (!rows.length) return '';
  return `<details class="legend">
    <summary>${esc(summary || 'What these statuses mean')}</summary>
    <dl>${rows.map(([cls, label, text]) =>
      `<dt>${badge(cls, label)}</dt><dd>${esc(text)}</dd>`).join('')}</dl>
  </details>`;
}

/* --------------------------------------------------------------- pieces */

function meter(pct) {
  if (pct === null || pct === undefined) return '<span class="muted">—</span>';
  const width = Math.max(0, Math.min(100, pct));
  return `<div class="meter">
    <div class="track"><div class="fill" style="width:${width}%"></div></div>
    <span class="pct">${pct.toFixed(1)}%</span>
  </div>`;
}

/* Exact figure with the lakh/crore shorthand beneath. */
function numCell(n) {
  const short = fmtShort(n);
  return `${fmtNum(n)}${short ? `<div class="sub">${short}</div>` : ''}`;
}

function tile({ label, value, sub, cls, onclick, id }) {
  return `<div class="tile ${cls || ''}${onclick ? ' clickable' : ''}"${id ? ` id="${id}"` : ''}${onclick ? ` onclick="${onclick}" role="button" tabindex="0"` : ''}>
    <div class="tile-label">${esc(label)}</div>
    <div class="tile-value">${value}</div>
    ${sub ? `<div class="tile-sub">${sub}</div>` : ''}
  </div>`;
}

function tiles(list) {
  return list.map(tile).join('');
}

function emptyState(title, detail) {
  return `<div class="empty"><div class="big">${esc(title)}</div>${detail ? esc(detail) : ''}</div>`;
}

/* Spreadsheet vs database, as one proportional bar plus its numbers. */
function comparisonBar(c) {
  if (!c || !c.ok) return '';
  const matching = c.matching || 0;
  const missing = c.missing || 0;
  const extra = c.extra || 0;
  const total = matching + missing + extra;
  if (!total) return '';

  const seg = (kind, n, label) => {
    const pct = (100 * n) / total;
    if (!n) return '';
    return `<div class="seg ${kind}${pct < 6 ? ' narrow' : ''}" style="flex:0 0 ${pct}%"
      title="${esc(label)}: ${fmtNum(n)} (${pct.toFixed(1)}%)">${fmtNum(n)}</div>`;
  };

  return `<div class="compare-bar">
      ${seg('matching', matching, 'Matching')}
      ${seg('missing', missing, 'Missing from database')}
      ${seg('extra', extra, 'Extra in database')}
    </div>
    <div class="compare-legend">
      <span class="item"><i class="swatch matching"></i>Matching <b class="n">${fmtNum(matching)}</b></span>
      <span class="item"><i class="swatch missing"></i>Missing <b class="n">${fmtNum(missing)}</b></span>
      <span class="item"><i class="swatch extra"></i>Extra <b class="n">${fmtNum(extra)}</b></span>
    </div>`;
}

/* ---------------------------------------------------------------- toast */

function toast(message, kind = '') {
  let el = document.querySelector('.toast');
  if (!el) {
    el = document.createElement('div');
    el.className = 'toast';
    // Announced to screen readers: a visual-only confirmation is no
    // confirmation for anyone not looking at that corner of the screen.
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), 3600);
}

/* Wire a button to an endpoint, then re-run the page's refresh. */
function wireAction(buttonId, url, onDone, { method = 'POST', busy = 'Working…' } = {}) {
  const btn = document.getElementById(buttonId);
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = busy;
    try {
      await sendJSON(url, method);
      toast('Done', 'ok');
      if (onDone) await onDone();
    } catch (err) {
      toast(err.message, 'bad');
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  });
}

/* -------------------------------------------------------------- polling */

/* Poll `fn` now and every `ms`, pausing while the tab is hidden.
 *
 * Pausing matters more than it looks: a dashboard is the page people leave open
 * all day, and a tab in the background polling every 5s is thousands of pointless
 * requests. It catches up immediately on return.
 */
function startPolling(fn, ms) {
  let inFlight = false;
  const stamp = document.getElementById('last-poll');

  const tick = async () => {
    if (document.hidden || inFlight) return;
    inFlight = true;
    try {
      await fn();
      if (stamp) stamp.textContent = `updated ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      console.error('poll failed', err);
      if (stamp) stamp.textContent = 'update failed';
    } finally {
      inFlight = false;
    }
  };

  tick();
  const timer = setInterval(tick, ms);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  return () => clearInterval(timer);
}

/* Cadence of a scheduled job, in the terms it is actually scheduled by. */
function jobCadence(job) {
  if (!job) return '';
  if (job.schedule === 'push') {
    const n = job.servers_reporting ?? 0;
    return `pushed by ${n} server${n === 1 ? '' : 's'}`;
  }
  if (job.schedule === 'hourly') {
    const runs = job.runs_24h ?? 0;
    return `on the hour · ${runs} run${runs === 1 ? '' : 's'} in 24h`;
  }
  const secs = job.interval_seconds;
  return secs < 60 ? `every ${secs}s` : `every ${Math.round(secs / 60)} min`;
}

function jobLine(job) {
  if (!job) return '';
  const bits = [
    job.running
      ? `running now${job.progress ? ` (${esc(job.progress)})` : ''}`
      : `last ${job.schedule === 'push' ? 'report' : 'run'} ${timeAgo(job.last_run)}`,
    ...(job.schedule === 'push' ? [] : [`next ${timeUntil(job.next_run)}`]),
    jobCadence(job),
  ];
  if (job.last_error) bits.push(`<span class="err">error: ${esc(job.last_error)}</span>`);
  return bits.join(' · ');
}

/* ----------------------------------------------------------- slide-over */

/* One panel, reused by every page that needs a detail view without a
 * navigation. Opening it pushes a history entry, so Back closes it - on a
 * phone that is the gesture people will reach for first. */
const SlideOver = {
  el: null,
  onClose: null,

  ensure() {
    if (this.el) return this.el;
    this.el = document.createElement('aside');
    this.el.className = 'slideover';
    this.el.setAttribute('role', 'dialog');
    this.el.setAttribute('aria-modal', 'true');
    this.el.hidden = true;
    this.el.innerHTML = `
      <div class="slideover-head">
        <h2 id="slideover-title"></h2>
        <button class="subtle" data-close aria-label="Close panel">✕</button>
      </div>
      <div class="slideover-body" id="slideover-body"></div>
      <div class="slideover-foot" id="slideover-foot" hidden></div>`;
    document.body.appendChild(this.el);
    this.el.setAttribute('aria-labelledby', 'slideover-title');
    this.el.querySelector('[data-close]').addEventListener('click', () => this.close());

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.el && this.el.classList.contains('open')) this.close();
    });
    window.addEventListener('popstate', () => {
      if (this.el && this.el.classList.contains('open')) this.close(false);
    });
    return this.el;
  },

  open(title, html, footer) {
    const el = this.ensure();
    el.hidden = false;
    el.querySelector('#slideover-title').textContent = title;
    el.querySelector('#slideover-body').innerHTML = html;
    const foot = el.querySelector('#slideover-foot');
    foot.innerHTML = footer || '';
    foot.hidden = !footer;
    // Two frames: the element has to be un-hidden and laid out before the
    // class change can be animated, or it simply appears.
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('open')));
    Shell.scrim(true, () => this.close());
    history.pushState({ slideover: true }, '');
    el.querySelector('[data-close]').focus();
  },

  setBody(html) {
    if (this.el) this.el.querySelector('#slideover-body').innerHTML = html;
  },

  close(popHistory = true) {
    if (!this.el) return;
    this.el.classList.remove('open');
    Shell.scrim(false);
    setTimeout(() => { if (this.el && !this.el.classList.contains('open')) this.el.hidden = true; }, 200);
    if (popHistory && history.state && history.state.slideover) history.back();
    if (this.onClose) this.onClose();
  },
};

/* --------------------------------------------------------------- shell */

const Shell = {
  scrimEl: null,

  init() {
    const btn = document.querySelector('.menu-btn');
    const sidebar = document.querySelector('.sidebar');
    if (btn && sidebar) {
      btn.addEventListener('click', () => {
        const open = sidebar.classList.toggle('open');
        btn.setAttribute('aria-expanded', String(open));
        this.scrim(open, () => {
          sidebar.classList.remove('open');
          btn.setAttribute('aria-expanded', 'false');
        });
      });
    }
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && sidebar && sidebar.classList.contains('open')) {
        sidebar.classList.remove('open');
        this.scrim(false);
      }
    });
    this.nameTheDatabase();
  },

  scrim(show, onClick) {
    if (!this.scrimEl) {
      this.scrimEl = document.createElement('div');
      this.scrimEl.className = 'scrim';
      document.body.appendChild(this.scrimEl);
    }
    this.scrimEl.onclick = onClick || null;
    this.scrimEl.classList.toggle('show', !!show);
  },

  /* Name the source of the numbers, always.
   *
   * The laptop copy and the production master both answer on 127.0.0.1:3306
   * with an `admin` schema but hold different data, so "which server is this?"
   * must never be a guess. */
  async nameTheDatabase() {
    const chip = document.getElementById('db-identity');
    if (!chip) return;
    try {
      const db = await getJSON('/api/db/identity');
      if (!db.ok) {
        chip.textContent = 'db unreachable';
        chip.style.color = 'var(--bad)';
        return;
      }
      chip.textContent = `db: ${db.hostname}`;
      chip.title = `${db.connected_to} · ${db.version || ''}`;
      if (db.hostname === 'vishal-konale') {
        chip.style.color = 'var(--warn)';
        const banner = document.getElementById('db-banner');
        if (banner) {
          banner.innerHTML = `<div class="notice warn" style="margin:0 0 var(--s5)">
            <div class="body"><strong>Reading the local copy, not the master.</strong>
            These counts come from <span class="mono">${esc(db.hostname)}</span>, which
            disagrees with production in both directions. Run
            <span class="mono">./run-with-tunnel.sh</span> for authoritative numbers.</div>
          </div>`;
        }
      }
    } catch (_) { /* the chip simply stays empty */ }
  },
};

document.addEventListener('DOMContentLoaded', () => Shell.init());
