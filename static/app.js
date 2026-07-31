/* Shared helpers for every dashboard page. */

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/* 100000 -> "1,00,000" (Indian grouping, the way these counts get quoted). */
function fmtNum(n) {
  if (n === null || n === undefined) return '—';
  return Number(n).toLocaleString('en-IN');
}

/* 100000 -> "1 L", 12500000 -> "1.25 Cr". Shown under the exact number. */
function fmtShort(n) {
  if (n === null || n === undefined) return '';
  if (n >= 10000000) return (n / 10000000).toFixed(2).replace(/\.?0+$/, '') + ' Cr';
  if (n >= 100000) return (n / 100000).toFixed(2).replace(/\.?0+$/, '') + ' L';
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + ' K';
  return '';
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

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* Magnitude meter: ONE hue. The number beside it does the precise work, so the
 * bar never has to be read by colour. */
function meter(pct) {
  if (pct === null || pct === undefined) return '<span class="muted">—</span>';
  const width = Math.max(0, Math.min(100, pct));
  return `<div class="meter">
    <div class="track"><div class="fill" style="width:${width}%"></div></div>
    <span class="pct">${pct.toFixed(1)}%</span>
  </div>`;
}

/* Every status badge the dashboard can show, and what it actually means.
 *
 * One definition, used twice: badge() hangs the wording off the badge as a
 * hover tooltip, and statusLegend() prints the same wording under the table it
 * belongs to - so the meaning is readable without hovering, and on touch.
 *
 * Grouped by column rather than flattened by label, because the same word means
 * different things in different places: NOT CHECKED on Website health means no
 * HTTP check has run yet, while on Partners it means no COUNT query has run.
 *
 * Each entry is [badge class, label, meaning]. Order is worst-first, matching
 * how the tables sort.
 */
const STATUS_HELP = {
  sites: [
    ['down', 'DOWN', 'The last check failed - unexpected status code, timeout, DNS failure or TLS error. The exact reason is printed under the badge. Two failures in a row emails the alert recipients.'],
    ['unknown', 'NOT CHECKED', 'No check has completed for this site yet. Normal for the first few seconds after a restart; the health job runs every 30s.'],
    ['up', 'UP', 'The site answered within its timeout, with one of the HTTP status codes config.yaml expects for it (the "want ..." line in the HTTP column).'],
  ],
  partners: [
    ['down', 'QUERY FAILED', 'The last count query against MySQL errored, so every number on this row is stale. The error is shown beneath the badge.'],
    ['unknown', 'NOT CHECKED', 'No counts have been collected for this partner yet. The MySQL sweep runs on the top of every hour.'],
    ['down', 'NONE LIVE', 'We hold events for this partner but not one is published and still upcoming - everything has either ended or never went live. The ingest is landing, but nothing is reaching the site.'],
    ['warn', 'MOSTLY PAST', 'The "% still live" column is under 50% - fewer than half the events we hold are published and upcoming. Read it with the two columns to its left, because it covers two different problems: a big "Ended" number is a partner winding down or between seasons, which is usually fine, while a big "Not published" number means events inserted that never went live, which is the one worth chasing.'],
    ['up', 'OK', 'The "% still live" column is 50% or more - at least half the events we hold are published and still upcoming. It is a floor, not a grade: 51% and 99% both show OK.'],
  ],
  jobs: [
    ['down', 'STALLED', 'Nothing new has been inserted for more than twice the interval its frequency implies. The ingest job is very likely broken.'],
    ['down', 'NEVER RAN', 'Nothing has ever been inserted for this partner.'],
    ['warn', 'LATE', 'Overdue, but by less than twice its interval - a weekly job a couple of days behind reads as late rather than broken.'],
    ['unknown', 'NO SCHEDULE', 'The status sheet gives no frequency for this partner, so there is nothing to measure freshness against.'],
    ['unknown', 'DORMANT', 'No schedule, 20 or fewer live events, and untouched for over a year. A one-off import or a stray record, not a running job.'],
    ['unknown', 'RETIRED', "The sheet's note says this partner was switched off deliberately, so stale data is not a fault."],
    ['up', 'RUNNING', 'The newest event was inserted within the interval its cron frequency implies.'],
  ],
  cron_entry: [
    ['down', 'NO CRON', 'This partner\'s server did report its crontab, and no line for this partner appears in it.'],
    ['warn', 'COMMENTED OUT', 'A crontab line exists but is commented out, so cron never runs it.'],
    ['unknown', 'NOT COLLECTED', 'No crontab has been collected from this partner\'s server, so a missing line proves nothing either way.'],
    ['up', 'SCHEDULED', 'A crontab line for this partner was found on a server that reports its crontab. The schedule is shown beneath.'],
  ],
  cron: [
    ['unknown', 'DISABLED', 'The line is commented out in the crontab, so cron never runs it. Kept visible because a job switched off by accident looks identical to one switched off on purpose.'],
    ['up', 'ACTIVE', 'A live crontab line - cron runs this on the schedule shown.'],
  ],
  processes: [
    ['down', 'OFFLINE', "Server: no heartbeat from this server's agent recently, so the process list below it is stale. Usually the agent or its tunnel is down, not the app."],
    ['down', 'ERRORED', 'Process: PM2 reports this process as errored - it crashed or failed to start.'],
    ['warn', 'STOPPED / LAUNCHING / …', "Process: any other PM2 state, shown in PM2's own words."],
    ['up', 'LIVE', "Server: this server's agent reported within the staleness window, so its process list is current."],
    ['up', 'ONLINE', 'Process: PM2 reports this process as running.'],
  ],
  history: [
    ['down', 'FAILED', 'That collection errored - the counts for that timestamp were never recorded.'],
    ['up', 'OK', 'That collection succeeded.'],
  ],
};

function statusHelp(group, label) {
  const row = (STATUS_HELP[group] || []).find((r) => r[1] === label);
  return row ? row[2] : '';
}

/* Status badges always carry a text label; app.css adds a glyph via ::before,
 * so state is never communicated by colour alone. Pass `group` to attach the
 * explanation from STATUS_HELP as a tooltip. */
function badge(cls, label, group, help) {
  // `help` is for labels that vary at runtime - PM2 reports its own words for
  // any state that isn't online/errored, so those can't be looked up by label.
  const text = help || (group ? statusHelp(group, label) : '');
  return `<span class="badge ${cls}"${text ? ` title="${esc(text)}"` : ''}>${esc(label)}</span>`;
}

function statusBadge(ok, labels, group) {
  const l = Object.assign({ up: 'UP', down: 'DOWN', unknown: 'NO DATA' }, labels || {});
  if (ok === null || ok === undefined) return badge('unknown', l.unknown, group);
  return ok ? badge('up', l.up, group) : badge('down', l.down, group);
}

/* The same wording as the tooltips, printed under the table. Collapsed by
 * default so it costs no space once you know the words. */
function statusLegend(groups, summary) {
  const rows = [].concat(groups).flatMap((g) => (STATUS_HELP[g] || []));
  if (!rows.length) return '';
  return `<details class="legend">
    <summary>${esc(summary || 'What these statuses mean')}</summary>
    <dl>${rows.map(([cls, label, text]) =>
      `<dt>${badge(cls, label)}</dt><dd>${esc(text)}</dd>`).join('')}</dl>
  </details>`;
}

/* Exact number, with the lakh/crore shorthand underneath. */
function numCell(n) {
  const short = fmtShort(n);
  return `${fmtNum(n)}${short ? `<div class="muted" style="font-size:0.72rem">${short}</div>` : ''}`;
}

function toast(message, isError = false) {
  let el = document.querySelector('.toast');
  if (!el) {
    el = document.createElement('div');
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.toggle('is-bad', isError);
  el.classList.add('show');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), 3000);
}

/* Wire a button to a POST endpoint, then re-run the page's refresh function. */
function wireRefresh(buttonId, url, onDone) {
  const btn = document.getElementById(buttonId);
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Running…';
    try {
      await postJSON(url);
      toast('Refresh complete');
      if (onDone) await onDone();
    } catch (err) {
      toast(`Refresh failed: ${err.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });
}

/* Poll `fn` now and every `ms`, skipping while the tab is hidden. */
function startPolling(fn, ms) {
  const tick = async () => {
    if (document.hidden) return;
    try {
      await fn();
      const el = document.getElementById('last-poll');
      if (el) el.textContent = `updated ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      console.error('poll failed', err);
      const el = document.getElementById('last-poll');
      if (el) el.textContent = 'poll failed';
    }
  };
  tick();
  setInterval(tick, ms);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
}

/* How often a job fires, in the terms it is actually scheduled by. The hourly
 * job reports its real 24h run count too - that number is the whole point of
 * scheduling it on the hour, so it shouldn't have to be taken on trust. */
function jobCadence(job) {
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

/* One-line job summary for a panel header. */
function jobLine(job) {
  if (!job) return '';
  const bits = [
    job.running ? `running now${job.progress ? ` (${esc(job.progress)})` : ''}`
                : `last ${job.schedule === 'push' ? 'report' : 'run'} ${timeAgo(job.last_run)}`,
    // A pushed job has no "next" this side knows about - the server decides.
    ...(job.schedule === 'push' ? [] : [`next ${timeUntil(job.next_run)}`]),
    jobCadence(job),
  ];
  if (job.last_error) bits.push(`<span class="err">error: ${esc(job.last_error)}</span>`);
  return bits.join(' · ');
}

/* Click-to-sort table headers. Reads data-sort-key off each <th>. */
function makeSortable(tableId, state, render) {
  document.querySelectorAll(`#${tableId} th.sortable`).forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sortKey;
      if (state.key === key) {
        state.dir = state.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.key = key;
        state.dir = th.dataset.sortDefault || 'desc';
      }
      render();
    });
  });
}

function sortRows(rows, key, dir) {
  const sign = dir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const x = a[key], y = b[key];
    // Nulls always sort to the bottom, whichever direction we're going.
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    if (typeof x === 'string') return sign * x.localeCompare(y);
    return sign * (x - y);
  });
}

function sortArrow(state, key) {
  if (state.key !== key) return '<span class="arrow">↕</span>';
  return `<span class="arrow">${state.dir === 'asc' ? '↑' : '↓'}</span>`;
}
