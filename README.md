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
curl -X POST http://localhost:5603/api/partners/feed-count \
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

### Login (HTTP Basic — the .htaccess equivalent)

Nothing sits in front of this process, so the password prompt comes from the
app. In `.env`:

```
OPS_AUTH_USER=admin
OPS_AUTH_PASSWORD=some-strong-password
```

- Auth turns on **as soon as `OPS_AUTH_PASSWORD` is set**. There is no separate
  on/off switch — one that could be left off while a password sits in `.env`
  would be worse than none. With no password the app logs
  `[auth] OPS_AUTH_PASSWORD is not set - dashboard is UNPROTECTED` and serves
  openly, which is fine on a laptop and wrong on anything reachable.
- It covers **every page, every API route and `/static`** — it is middleware
  ([auth.py](auth.py)), not a per-route dependency, so a route added later
  cannot forget it.
- The three agent push endpoints are **exempt**: `/api/pm2/report`,
  `/api/cron/report`, `/api/partners/feed-count`. They already authenticate
  with `x-agent-secret`, and `agent.py` runs unattended on every monitored
  server — requiring the browser password there would mean redeploying all of
  them whenever it changes.
- `OPS_AUTH_USER` may also be set as `app.auth_user` in config.yaml. The
  password is environment-only, same rule as MySQL and SMTP.

Curl against a protected instance: `curl -u admin:password http://…/api/jobs`.
Basic auth sends the password base64-encoded, not encrypted — behind the
Cloudflare tunnel (HTTPS) that's fine; over plain HTTP it is not.

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

### Query cost, and how often each thing runs

The scheduled sweep is **one** grouped query covering every partner (~75s over
2.9M rows), not 55 separate ones. It still scans the whole table, so it runs on
a strict hourly cron and nothing else touches MySQL on a timer.

| What | Cadence | Touches MySQL |
| --- | --- | --- |
| Partner counts | top of every hour — 24 runs/day | yes, 1 grouped query |
| Website health | every 30s | no |
| PM2 processes | pushed by `agent.py` every 5s | no |
| Server crontabs | pushed by each server every 6h | no |
| Every dashboard page | polls its own SQLite every 5–20s | no |

The pages poll often and feel live, but they read the dashboard's own SQLite —
opening a page, or leaving one open all day, adds **zero** MySQL queries. Only
the hourly sweep and the explicit *Refresh counts* / *Refresh this partner*
buttons do.

`counts_hourly` fires on the hour rather than every 3600s because an interval
timer can't hold 24-a-day: it starts each wait only after the previous run
finishes, so a 75s sweep pushes every later run deeper into the hour, and each
restart begins a fresh cycle. Restarting doesn't buy an extra sweep either — on
startup the dashboard collects only if the stored counts are already over an
hour old. The panel header on the Partners and Jobs pages shows the actual
number of runs in the last 24h, so the schedule is verifiable, not just claimed.

If you need the counts fresher than hourly, add an index on `partner` (and
ideally `(partner, published, enddates)`) first, then decide.

## Configuration

Everything lives in `config.yaml`.

```yaml
app:
  counts_hourly: true             # DB sweep on the top of every hour (24/day)
  counts_interval_seconds: 3600   # fallback, used only if counts_hourly is false
  health_interval_seconds: 30     # how often websites are polled (no DB)
  pm2_stale_seconds: 30           # PM2 server shown OFFLINE after this silence
  max_concurrent_queries: 4       # parallel MySQL queries (per-partner button only)
```

Set `counts_hourly: false` to go back to a plain interval timer on
`counts_interval_seconds`. Don't drop that to seconds — these are `COUNT()`
queries over a large table.

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

Eleven sites are monitored, checked every 30s. Seven of them are the public
products: `eventseeker.com`, `cityseeker.com`, `mvc`, `concierge`,
`experience`, `nearmyhotel` and `rtrlocal.com`. Verified on 2026-07-31 —
everything answers 200 except eventseeker/cityseeker, which sit behind the
same WAF and answer 403 from outside; `nearmyhotel` and `rtrlocal` redirect to
a generated path and take 2–4s, hence their 20s timeouts.

### Email alerts when a site goes down

```yaml
alerts:
  enabled: true
  recipients: ["farooque@wcities.com", "ozair@wcities.com",
               "mayur@wcities.com", "rahul@wcities.com"]
  from_address: "monitor@wcities.com"
  failures_before_alert: 2      # checks are 30s apart, so ~1 min down
  repeat_hours: 6               # reminder while still down; 0 = off
  smtp:
    host: "smtp.example.com"
    port: 587
    security: starttls          # starttls (587) | ssl (465) | none
    username: "monitor@wcities.com"
```

The SMTP password is **not** in `config.yaml` — put it in `.env` as
`OPS_SMTP_PASSWORD`, like the database password. `OPS_SMTP_HOST`,
`OPS_SMTP_USER`, `OPS_ALERT_FROM` and `OPS_ALERT_TO` override the file too.

Three mails, and no others:

| When | Subject |
|---|---|
| `failures_before_alert` checks fail in a row | `[DOWN] <site> is not responding` |
| every `repeat_hours` while it stays down | `[STILL DOWN] <site> — down for 2h 10m` |
| it answers twice in a row again | `[RECOVERED] <site> is back up` |

One failed check is deliberately **not** an outage — a single timeout or WAF
hiccup would otherwise mail four people at 3am. A flapping site produces one
DOWN and one RECOVERED per flap, never one mail per check.

Up/down state lives in SQLite (`site_alert_state`), so restarting the dashboard
cannot re-send a DOWN mail for an outage already reported, and "down since"
survives the restart. Every attempt — delivered or not — is written to
`alert_log`, so an alert that failed to send is still on record.

Check the setup without waiting for an outage:

```bash
curl -X POST http://localhost:5603/api/alerts/test    # mails all recipients
curl -s     http://localhost:5603/api/alerts          # config + what was sent
```

Until `smtp.host` and `from_address` are set, `/api/sites` reports
`alerts.ready: false` with the reason, alerts are logged as "not sent", and the
health checks carry on regardless — a mail server outage can never take the
monitoring down.

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
-R 8777:127.0.0.1:5603          server:8777       ->  this laptop's dashboard
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

Every route below needs the Basic-auth login once `OPS_AUTH_PASSWORD` is set,
except the two `x-agent-secret` POSTs, which never do.

| Endpoint | Purpose |
|---|---|
| `GET /api/partners` | All partner rows + summary + job status |
| `GET /api/partners/{name}` | One partner |
| `GET /api/partners/{name}/history` | Count history for a partner |
| `POST /api/partners/{name}/refresh` | Re-count one partner now |
| `POST /api/counts/refresh` | Re-count every partner now |
| `GET /api/sites` | Website health rows + alert status |
| `POST /api/sites/refresh` | Check sites now (optional `?url=`) |
| `GET /api/alerts` | Alert config + log of mails sent |
| `POST /api/alerts/test` | Send a test alert to every recipient |
| `GET /api/pm2/status` | PM2 state per server |
| `POST /api/pm2/report` | Agent heartbeat (needs `x-agent-secret`) |
| `GET /api/jobs` | Scheduler state |
| `GET /api/db/ping` | MySQL connectivity check |

## Files

```
dashboard.py    FastAPI app, routes, aggregation
auth.py         HTTP Basic auth middleware (the .htaccess equivalent)
config.py       config.yaml + .env loading
config.yaml     partners, queries, websites, intervals
mysql.py        read-only MySQL access + the SELECT-only guard
counts.py       runs both counts for one partner
health.py       one website check
alerts.py       down/recovery emails (state machine + SMTP)
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

`run-with-tunnel.sh` does this automatically at startup - no manual step. The
sheet holds both halves of the partner-side number, which populates 78 of the
119 partners without touching any PHP:

```bash
./venv/bin/python import-feed-counts.py --dry-run   # preview
./venv/bin/python import-feed-counts.py             # re-import by hand
```

The importer writes **straight to ops.db** rather than POSTing to the API. Going
via HTTP meant it had to run after the dashboard was up, and racing startup
silently failed all 85 rows more than once. Override the sheet location with
`OPS_PARTNER_CSV`.

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

## Cron tab — every crontab entry on every reporting server

**The servers push, the dashboard never logs in.** `agent.py` — the same agent
that reports PM2 — also reads its own `crontab -l`, stats its own redirect
targets, and POSTs both to `/api/cron/report` every `CRON_INTERVAL_SECONDS`
(6h by default). It retries on the 5s PM2 tick until one succeeds, so a
dashboard that was down at startup doesn't cost six hours of missing data.

This is set by `app.cron_source`:

| `cron_source` | How the data arrives | SSH? |
|---|---|---|
| `agent` (default) | servers POST to `/api/cron/report` | **no** |
| `ssh` | the dashboard logs into each host and reads them | yes |

In `agent` mode no SSH job is started at all — no stored SSH passwords, nothing
that can hang on a password prompt, and nothing to break when a box's
credentials change. It also works for servers the dashboard has no route to,
since the connection is outbound from them.

Parsing stays on the dashboard (`cron_parse.py`) rather than in the agent: the
agent has to remain a single dependency-free file that runs on old Pythons, and
`cron_parse` is the tested code. Rows are keyed by `SERVER_IP`, which
`deploy-agent.sh` sets to the host it deployed to — that must match the server
column in the partner sheet, or the Jobs tab can't tell that a partner's cron
lives on that box.

Requests are authenticated with `x-agent-secret`, the same shared secret the PM2
endpoint uses. A wrong secret is a 401.

### The old SSH collector

Still present and still works — set `cron_source: ssh` to use it, and it stays
the only option for a server you can't or won't run the agent on:

```bash
./venv/bin/python collect-crons.py            # all hosts in .env
./venv/bin/python collect-crons.py 3.94.49.56 # one host
./venv/bin/python collect-crons.py --dry-run
```

Hosts come from `SSH_HOST` + `AGENT_HOSTS`, with per-host credentials. A host
that can't be reached is reported and skipped, never blocking the others. Note
that password auth needs `setsid` on the box running it: ssh prompts on the
terminal otherwise, because `SSH_ASKPASS_REQUIRE` only exists in OpenSSH ≥ 8.4
and RHEL/Rocky 8 ships 8.0.

Current state — **425 jobs across 3 servers**:

| Server | Jobs |
|---|---|
| `44.198.210.209` (ip-10-0-0-153) | 268 |
| `3.94.49.56` (ip-10-0-0-242) | 145 |
| `100.52.8.134` (ip-10-0-0-117) | 12 |

307 active, 118 commented out, 66 matched to a known partner.

### "Last output" — did it actually run?

Where a cron line redirects (`> out.csv`, `>> run.log`), the collector stats
that file, so you get the last time the job wrote anything. **207 of 425 jobs
have one.** A `0 bytes` size shows as **empty** and a log untouched for over 30
days is highlighted — both are signs a job is firing but producing nothing.

This is inferred, not authoritative: the system cron log would be definitive but
needs root. A job with no redirect shows *no log redirect* rather than a guess.

### Storage

A collection **replaces** that server's rows wholesale, so a job deleted from a
crontab disappears here too rather than lingering as a phantom.

Parsing (`cron_parse.py`) handles the real shapes in these crontabs: 5-field
specs, `@daily`, multi-hour lists (`00 02,14 * * *` → "daily at 02:00 and
14:00"), `*/15` steps, commented-out jobs (kept, marked DISABLED), and
`MAILTO=`/`PATH=` env lines (skipped). Partner names are guessed from the path
and matched against the known partner list, so a wrong guess isn't invented.

## Note on layout

The code can live in a subdirectory (e.g. `Dashboard/`) with `.env` kept in the
parent, outside the git repo. `config.py` and the shell scripts look for `.env`
beside themselves first, then one level up; `OPS_ENV_FILE` overrides both.

### Jobs × Cron cross-reference

The Jobs tab carries a **Cron entry** column populated from the collected
crontabs, because the useful conclusion lives in the join:

| Cron entry | Meaning |
|---|---|
| **SCHEDULED** | an active crontab line exists (its cadence is shown beneath) |
| **COMMENTED OUT** | present in the crontab but disabled |
| **NO CRON** | that server *was* collected and no entry was found |
| **NOT COLLECTED** | we never read that server's crontab — absence proves nothing |

"Stopped inserting" alone means the job is broken. "Stopped inserting **and**
has no cron entry" means the job was *removed* — a different fix. The
**Stalled + no cron** tile counts those, and they sort to the top of the table
ordered by live-event count, so the biggest problem is the first row.

Current: **9 of the 14 stalled partners have no cron entry**, led by
`fareharbor` — 11,591 live events, weekly schedule, 114 days since its last
insert, nothing scheduled to refresh it.

The NO CRON / NOT COLLECTED split matters: `livenation-europe` sits on
`198.61.136.173`, which can't be reached, so it reports NOT COLLECTED rather
than claiming a missing cron.

### Job names

Each cron row carries a short **Job** label so it's identifiable in a list of
425 — derived from the last two path components, because the directory usually
carries the meaning (`marriott_mvc/mvc.sh` beats a bare `mvc.sh`, and there are
two different `yelp.sh` entries on one server).

Deriving it needed more than "find a .php": the crontabs also contain
extensionless executables (`.../events/partner_eventcron`), `cd X && …` wrappers
and `if [ -f … ]` conditionals. `cron_parse.primary_target()` therefore prefers
a path with a script extension, and otherwise takes the first absolute path that
is not:

- an interpreter or wrapper — matched as any `…/bin/{php,bash,python,flock,…}`,
  so a virtualenv's `…/venv/bin/python` is skipped as well as `/usr/bin/python`
- a lock or temp file (`/tmp/…`, `*.lock`)
- the output redirect target
- the working directory of a leading `cd`

All 307 active jobs resolve to a name; none is mistaken for its interpreter.

The **Full command** column shows the whole line, wrapped rather than truncated.
