# Ops Dashboard

Web dashboard for partner event ingestion, website health and PM2 processes.

Three tabs:

| Tab | What it shows |
|---|---|
| **Partners** | Per partner: events in feed vs events in database (future only), the gap between them, and the % still live |
| **Website Health** | Scheduled up/down checks for each site, with 24h uptime, latency and a recent-checks sparkline |
| **Processes** | Live PM2 process status per server, fed by `agent.py` |

## The numbers

All counts run live against MySQL on a schedule — nothing is read from a
spreadsheet.

**Total inserted** is everything we hold for the partner (this is the number
that matches "Partner Total Count" in the Monday sheet). It splits three ways:

| Column | Meaning |
|---|---|
| **Live in database** | published, and not yet ended — today forward |
| **Already ended** | published and inserted fine, just in the past |
| **Not published** | inserted but never went live — usually the number worth chasing |

```sql
-- Total inserted
SELECT COUNT(id) FROM jos_eventlist_events WHERE partner = %s;

-- Live (today forward, past events excluded)
SELECT COUNT(id) FROM jos_eventlist_events
WHERE published = '1' AND enddates >= CURRENT_DATE AND partner = %s;

-- Already ended
SELECT COUNT(id) FROM jos_eventlist_events
WHERE published = '1' AND enddates < CURRENT_DATE AND partner = %s;
```

`Not published` is derived (`total − live − ended`), so it costs no extra query.

All three live in `queries:` in `config.yaml`, so you can adjust them without
touching code. `%s` is bound as a parameter — the partner name is never
formatted into the SQL string.

### What is NOT yet tracked: partner-side "not inserted"

Every number above counts rows that **made it into our database**. The events a
partner sent that never inserted at all — duplicates, venue not found, fuzzy
no-match, expired — are the "Not inserted count" column of your Monday sheet,
and they are **not in this database**:

- the `not_inserted` schema exists on this server but has **zero tables**
- no table in `admin` carries a per-partner not-inserted count
- that data currently lives in the per-partner Dropbox `.xlsx` files listed in
  the sheet's `dropbox path` column

So "actual count the partner sent" = `total inserted` + `not inserted`, and the
second term needs a source before it can be shown.

### The "In partner feed" column

The table's first number column is **In partner feed** — what the partner has on
*their* side, next to **Total inserted** (ours). It shows `—` until something
reports it, because guessing would be worse than admitting we don't know.

Report it from the end of an ingest run. Copy `report_to_dashboard.php` to the
partner server, then add two lines to any `insertEvent*.php`:

```php
require_once '/home/fcampbell/report_to_dashboard.php';
ops_report('bokun', $total_in_feed, $inserted_count);
```

`$total_in_feed` is however many records the feed actually had — the script
already knows it, it's the size of the array it looped over. Reporting is
best-effort with a 5s timeout, so a dashboard that's down can never delay or
fail an ingest run.

Once reported, the column shows the partner total, the **% ingested**, and the
detail view exposes how many are missing:

| Partner | In partner feed | Total inserted | % ingested | Missing |
|---|---|---|---|---|
| bokun | 85,000 | 2,013 | 2.4% | 82,987 |

Anything can post it — the endpoint takes a `source` of `script`, `file`, `api`
or `manual`:

```bash
curl -X POST http://localhost:8000/api/partners/feed-count \
  -H 'Content-Type: application/json' \
  -H "x-agent-secret: $OPS_AGENT_SECRET" \
  -d '{"partner":"bokun","feed_count":85000,"inserted":1900,"source":"script"}'
```

**Why it can't be automatic.** The number is in no database. Of 119 partners,
only 29 have a `Source File` pointing at a real feed file, 10 have a usable
`Api link`, and `not_inserted.partner_event_not_inserted` covers 8. The ingest
script is the only thing that always knows.

**Read-only.** `mysql.py` rejects any statement that is not a bare `SELECT`
(and blocks stacked statements like `SELECT 1; DROP TABLE x`), and every query
runs inside a `READ ONLY` transaction. This dashboard cannot INSERT, UPDATE,
DELETE, DROP or ALTER anything. Its own history lives in a separate SQLite file
(`ops.db`).

## Partners included

**The partner list is discovered from the database, not configured.** Each run
does `SELECT partner, ... FROM jos_eventlist_events GROUP BY partner` and keeps
every partner with at least one live event, minus the exclusion list. A new
partner therefore appears on its own, and nothing gets silently dropped.

```yaml
partner_discovery:
  min_live_events: 1     # 0 shows every partner, live or not
  exclude: [bw, brownpaper, digitick, draisgroup, etix, college-sports]
```

That currently yields **112 partners** out of 149 in the table.

### Why not the CSV

Earlier versions built the list from `Monday Partner Status - Final.csv` using
`type = "feed"`. That was wrong, and quietly so:

- `active` has a **blank** `type`, so a live weekly partner with ~900 live
  events never appeared
- the same rule dropped `wcities` (41,865 live), `ticketnetwork` (24,430),
  `adticket` (20,599), `viator` (17,998) and 60 others
- `holibob` (30,661 live), `vivid` and `fareharbord` are **not in the sheet at
  all**, so no CSV rule could ever have found them

The `Api link` column is worse — only 13 rows have one and most are dead
(`grooveticket` = 1 event, `ticketon` = 0, `universe` marked "API is not
working").

The sheet is still used, but only for cosmetics: `partner_meta:` in
`config.yaml` supplies the Server / Frequency / path / note shown per row. A
partner missing from it still appears, just without those extras.

Exclusions are matched case-insensitively, because the sheet writes `Vegas`
while MySQL stores `vegas`.

## Setup

```bash
cd /home/vishal/ops-dashboard

# 1. Dependencies (the venv is already built; to rebuild it:)
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# 2. Database password — .env is gitignored, config.yaml holds no secrets
cp .env.example .env      # then fill in OPS_DB_PASSWORD

# 3. Verify connectivity and both queries before starting
./venv/bin/python check_db.py            # first 3 partners
./venv/bin/python check_db.py fever      # one named partner

# 4. Run
./venv/bin/python dashboard.py           # http://localhost:8000
```

### ⚠ Which MySQL it talks to — read this

**It currently queries the MySQL on this laptop** (`127.0.0.1`), and that copy
**disagrees with the master**. For partner `active`:

| | Master (`44.198.210.209`) | This laptop |
|---|---|---|
| Total | 3,155 | 3,941 |
| Live | **1,764** | 898 |

The master agrees with your Monday sheet; the laptop does not, and it's off in
*both* directions — so these are different datasets, not merely a stale
snapshot. **Treat the numbers as indicative until this is pointed at the
master.**

You already have the access: you `ssh fcampbell@44.198.210.209` and run
`mysql -u webuser -p` there. Two ways to fix it:

**1. Run the dashboard on the server** (best — no tunnel, no firewall change):

```bash
ssh fcampbell@44.198.210.209
# copy this directory over, then
./venv/bin/python dashboard.py
```

**2. SSH tunnel from the laptop** — one command, no firewall change:

```bash
./run-with-tunnel.sh
```

It opens the tunnel (asks for the SSH password for `fcampbell@44.198.210.209`
once), verifies it reached the master, starts the dashboard, and closes the
tunnel on Ctrl-C. Override with `SSH_USER=`, `SSH_HOST=`, `LOCAL_PORT=`,
`APP_PORT=`.

Equivalent by hand:

```bash
ssh -L 3307:127.0.0.1:3306 fcampbell@44.198.210.209   # leave running
# then, in .env:  OPS_DB_HOST=127.0.0.1 / OPS_DB_PORT=3307
```

### Knowing which database you're looking at

Because the laptop copy and the master both answer on `127.0.0.1:3306` with an
`admin` schema, the dashboard now **names its source**: a `db: <hostname>` chip
sits in the top bar, and an amber banner appears whenever it's reading the known
local copy. `check_db.py` prints the same thing (`>>> answered by: ...` with the
server UUID), so you can never silently read the wrong numbers again.

To check from your own `mysql>` session:

```sql
SELECT @@hostname, @@port, @@server_uuid;
```

Direct connection to `44.198.210.209:3306` from this machine times out — port
3306 is filtered outbound/inbound, while HTTP works fine. Note that error
`2003 "Can't connect"` is a *TCP timeout*: it fires before any password is
sent, so it never means the credential is wrong.

The remote hosts (`44.198.210.209`, `content.wcities.com`, the partner servers)
are **not reachable from here** — port 3306 times out on all of them, while HTTP
works fine, so it's a firewall/whitelist issue rather than a bad password. Note
that error `2003 "Can't connect"` is a *TCP timeout*: it happens before any
password is sent, so it never means the credential is wrong.

`OPS_DB_PASSWORD` is the **MySQL** account password for `webuser` — not an SSH
or server login.

To point at the master once this machine's IP is whitelisted, no code change is
needed — just add to `.env`:

```
OPS_DB_HOST=44.198.210.209
```

**Is the local copy current?** Spot-checked against the Monday CSV: the numbers
are close but not identical (`fever` 862,976 local vs 926,934 in the sheet;
`fnac` 74,070 vs 67,285), and the newest `created` timestamp locally is
2026-05-06. So this box is a somewhat stale copy. Fine for building and reading
trends; switch `OPS_DB_HOST` to the master when you want authoritative numbers.

Run with `OPS_DISABLE_SCHEDULER=1` to serve the UI without any background jobs.

### Query cost

A full cycle re-counts all 55 partners, 4 at a time. Most partners take ~1s, but
the largest (`fever`, ~863k rows) takes ~45s, so a full pass is a few minutes.
That's why `counts_interval_seconds` defaults to 1800 (30 min). If you want it
faster, add an index on `partner` (and ideally `(partner, published, enddates)`)
rather than lowering the interval.

## Configuration

Everything lives in `config.yaml`.

```yaml
app:
  counts_interval_seconds: 1800   # how often all 55 partners are re-counted
  health_interval_seconds: 120    # how often websites are polled
  max_concurrent_queries: 4       # parallel MySQL queries
```

`counts_interval_seconds` defaults to 30 minutes because these are `COUNT()`
queries over a large table. Don't drop it to seconds.

Secrets come from `.env` (or the environment) and override `config.yaml`:
`OPS_DB_HOST`, `OPS_DB_PORT`, `OPS_DB_USER`, `OPS_DB_PASSWORD`, `OPS_DB_NAME`.

### Website checks

```yaml
websites:
  - name: "wcities.com"
    url: "https://www.wcities.com"
    expect_status: 200
    timeout: 15
    keyword: "optional string that must appear in the body"
```

`expect_status` accepts a list. That's the honest way to describe a host that
legitimately answers 401 or 403 — the server is up, and a change *away* from
that code is what's worth alerting on. Two are configured that way already,
based on a real run:

- `eventseeker.com` → `[200, 403]` — a WAF blocks this machine even with a
  browser User-Agent. Tighten to plain `200` once you run from an allowed host.
- `content.wcities.com` → `[200, 401]` — HTTP auth sits in front of it.

Three partner servers (`198.61.136.172/173/174`) timed out on port 80 and are
commented out rather than left permanently red. Uncomment once you know what
they should answer on.

## PM2 processes

```bash
./deploy-agent.sh                    # default SSH_HOST from .env
./deploy-agent.sh 3.94.49.56         # a specific server
./deploy-agent.sh --stop 3.94.49.56  # remove it again
```

This copies `agent.py` to `~/ops-dashboard-agent/` on the target and starts it
under pm2 as **`ops-dashboard-agent`**, so it restarts with the machine once
you've run `pm2 save` there. Redeploying is safe — it replaces the running one.

The agent posts a heartbeat every 5s; a server shows OFFLINE after 30s of
silence, which usually means the agent stopped rather than the box being down.

### It only reports while the tunnel is up

This laptop has no public address, so the agent cannot POST to it directly.
`run-with-tunnel.sh` therefore opens a **reverse** forward as well:

```
-L 3307:127.0.0.1:3306          this laptop:3307  ->  server's MySQL
-R 8777:127.0.0.1:8000          server:8777       ->  this laptop's dashboard
```

The agent posts to `http://127.0.0.1:8777/api/pm2/report` on its own machine,
which comes back down the tunnel. **Stop `run-with-tunnel.sh` and the agent
keeps running but can't reach the dashboard**, so the server goes stale. That's
expected while the dashboard lives on a laptop; deploy the dashboard to a server
with a reachable address and the reverse tunnel becomes unnecessary — point
`DASHBOARD_URL` straight at it.

Authentication is the `OPS_AGENT_SECRET` in `.env` (generated, 43 chars). The
deploy script passes it to the agent, so both sides always match. A `401` in the
agent log means they've drifted apart.

### More than one server

The reverse forward only exists on a single SSH connection, so each extra
server needs its own. List them in `.env`:

```
AGENT_HOSTS=3.94.49.56 34.197.195.248
SSH_PASSWORD_3_94_49_56=...
```

`run-with-tunnel.sh` opens a reverse tunnel per host (reporting `up`, `FAILED`
or `SKIPPED` for each), then `./deploy-agent.sh <host>` installs the agent.
Host variables are the IP with dots as underscores; `SSH_USER_<host>` and
`SSH_KEY_<host>` work too when the login differs.

**Status of the partner servers, tested from this machine:**

| Server | SSH | Note |
|---|---|---|
| `44.198.210.209` | works | agent deployed, 7 processes reporting |
| `3.94.49.56` | port open, **password rejected** | needs `SSH_PASSWORD_3_94_49_56` |
| `34.197.195.248` | port open, **password rejected** | needs `SSH_PASSWORD_34_197_195_248` |
| `198.61.136.172/173/174` | **port 22 times out** | no route from here — needs a firewall rule or jump host; no password will help |

### Target requirements

`agent.py` is standard library only — no pip install on the server. It's written
for **Python 3.6**, which is what these boxes run, so it avoids `capture_output`
and `text=` (both 3.7+).

## HTTP API

| Endpoint | Purpose |
|---|---|
| `GET /api/partners` | All partner rows + summary + job status |
| `GET /api/partners/{name}` | One partner |
| `GET /api/partners/{name}/history` | Count history for a partner |
| `POST /api/partners/{name}/refresh` | Re-count one partner now |
| `POST /api/counts/refresh` | Re-count every partner now |
| `GET /api/sites` | Website health rows |
| `POST /api/sites/refresh` | Check sites now (optional `?url=`) |
| `GET /api/pm2/status` | PM2 state per server |
| `POST /api/pm2/report` | Agent heartbeat (needs `x-agent-secret`) |
| `GET /api/jobs` | Scheduler state |
| `GET /api/db/ping` | MySQL connectivity check |

## Files

```
dashboard.py    FastAPI app, routes, aggregation
config.py       config.yaml + .env loading
config.yaml     partners, queries, websites, intervals
mysql.py        read-only MySQL access + the SELECT-only guard
counts.py       runs both counts for one partner
health.py       one website check
scheduler.py    the two background loops
store.py        SQLite history (ops.db)
check_db.py     pre-flight connectivity/query check
agent.py        PM2 reporter, runs on each target server
templates/      page shells
static/         CSS + JS
```

## Notes on the UI

- Numbers use Indian grouping (`9,26,934`) with lakh/crore shorthand beneath.
- The `% still live` bar is a **single hue** — it encodes magnitude, and the
  number beside it does the precise work.
- Status badges always carry a text label *and* a glyph, never colour alone:
  the green/red pair is hard to separate for deuteranopic readers, so colour
  only reinforces a label that already says what the state is. All
  foreground/background pairs measure at 4.5:1 or better.
- The **Problems only** toggle filters to partners where the query failed, the
  count is missing, nothing is live, or under half the events are still live.

### Filling it from the Monday sheet

The sheet already holds both halves of the partner-side number, so you can
populate 78 of the 119 partners in one go without touching any PHP:

```bash
./venv/bin/python import-feed-counts.py --dry-run   # preview
./venv/bin/python import-feed-counts.py             # import
```

```
partner side  =  "Partner Total Count"  +  "Not inserted count"
                 (what we took)            (what we rejected)
```

Rows land tagged `source="sheet"`. They are a **point-in-time snapshot** — both
halves come from whenever the sheet was last updated, so they agree with each
other but drift from the live database. A later `script` report for the same
partner supersedes the sheet value automatically, so wiring up
`report_to_dashboard.php` progressively is safe.

Re-run it whenever the sheet is refreshed. 52 partners have no
"Not inserted count" cell and are skipped rather than guessed.

## Jobs tab — is each partner's cron still working?

Covers **all 119 partners on every server**, including the boxes that don't run
PM2 and the ones unreachable over SSH. It needs no server access at all.

The signal is `MAX(created)` — when a partner last had anything inserted —
compared against how often its cron is supposed to run (the `Frequency` column
of the sheet). A partner set to *Daily* whose newest row is three weeks old has
a broken job, and that conclusion needs nothing but the database.

`MAX(created)` is folded into the existing grouped scan, so it costs **no extra
query time** (still ~7.4s for all partners).

| State | Meaning |
|---|---|
| **RUNNING** | inserted within its expected interval |
| **LATE** | overdue, but inside the 2× grace window |
| **STALLED** | well past due — the job is very likely broken |
| **RETIRED** | the sheet says it was switched off on purpose |
| **DORMANT** | no schedule and ≤20 live events — a one-off import, not a job |
| **NO SCHEDULE** | Frequency gives no cadence, and it's still recent |

Only STALLED / LATE / NEVER count as problems. The first cut reported **26
stalled**, but only 17 were real: 5 were partners someone had deliberately
stopped (`hellotickets` — *"Stop this partner ASAP and delete all their
events"*), and 9 were stray one-off records with a single event and no server.
Flagging those trains people to ignore the tab, so they're now separated out.

After that correction: **14 stalled, 13 late, 46 running, 14 retired/dormant**.
The ones that matter are the big ones — `fareharbor` (11,591 live, weekly, 114
days since its last insert) and `venuepilot` (1,028 live, Daily, 7 days).

Frequency parsing lives in `jobs.py` and is deliberately forgiving, because the
sheet is free text: `Daily`, `Alternate weekly`, `wed-sat`, `Monthly(manual)`,
`weekly (manual)` all resolve. Values that describe *who* rather than *when*
("Mannual (Provide by ramesh/sudhan)", "New Partner Need to add in details
file") return no schedule rather than a guess — except that anything untouched
for over a year is flagged stalled regardless.

Tune the grace window with `GRACE_MULTIPLIER` in `jobs.py`.

### What this does NOT tell you

It shows whether inserts are *landing*, not whether the script *ran*. A cron
that runs nightly and correctly finds nothing new looks identical to one that
died. To distinguish them you need the script to report in — see
`report_to_dashboard.php`, which records the feed total on every run.
