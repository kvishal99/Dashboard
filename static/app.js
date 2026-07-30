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

/* Status badges always carry a text label; app.css adds a glyph via ::before,
 * so state is never communicated by colour alone. */
function statusBadge(ok, labels) {
  const l = Object.assign({ up: 'UP', down: 'DOWN', unknown: 'NO DATA' }, labels || {});
  if (ok === null || ok === undefined) return `<span class="badge unknown">${l.unknown}</span>`;
  return ok ? `<span class="badge up">${l.up}</span>` : `<span class="badge down">${l.down}</span>`;
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
