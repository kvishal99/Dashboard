# Ops Dashboard

Web dashboard for partner ingestion, spreadsheet reconciliation, website health
and PM2 processes.

## It is partner-centric

**You pick a partner, then see that partner and nothing else.** The work happens
on **Partners**, which is a two-pane workspace: a compact list on the left (name,
status, last run — three facts, so it can be scanned), and on the right six tabs
covering only the selected partner:

| Tab | What it shows for that partner |
|---|---|
| **Overview** | The counts, the comparison bar if one has been run, and the details |
| **Jobs** | Ingest freshness, then its crontab lines **grouped by what they are for** |
| **Process Logs** | That partner's activity — collections, feed reports, comparisons, exports, cron output |
| **Generated Files** | Generate and download the partner's real event CSV |
| **Issues** | Only that partner's open issues |

Everything on the right is fetched per partner, so selecting a different one
genuinely replaces what is on screen rather than filtering a page that already
loaded all 120.

Seven sections in the sidebar:

| Section | What it shows |
|---|---|
| **Overview** | Summary only: headline tiles, who needs attention, the worst issues. Hands you to a partner |
| **Partners** | The workspace above — the main page |
| **Issues** | Everything currently wrong: process, error, last run, status, retry |
| **Processes** | PM2 processes, the categorised crontab inventory, and website health |
| **Downloads** | Every file the dashboard can hand you: event CSVs, exports, logs, uploaded sheets |
| **Reports** | The management view — health split, worst offenders, printable |
| **Settings** | What this instance is configured to do, and where to change it |

A **Knowledge Base** slot sits at the bottom of the navigation, disabled and
marked *Soon*. The sidebar, its active states and its spacing already account
for it, so building that module later is a new template and a `NAV` entry — not
a redesign.

### There is no global Logs page

There used to be one, showing every line the process wrote, for every partner at
once — so answering *"what happened to WCities last night?"* meant reading past a
hundred lines about somebody else. Those lines now appear on the partner's own
**Process Logs** tab, assembled per partner by [activity.py](activity.py).

Nothing was removed from the back end: `/api/logs`, `/api/logs/files`,
`/download/log/{name}` and `/download/logs.csv` all still work, and the log
files are still listed on Downloads.

### Only the partners you look after

120 partners is too many to scan when eight of them are yours. Star a partner in
the list and **★ Mine** filters down to those. The list is stored server-side
(`partner_watchlist`), not in the browser, because it describes the team's work
rather than one laptop - it survives a different machine and everyone sees the
same short list.

### The spreadsheet comparison has been removed

Uploading a partner sheet and diffing it against the live database is gone from
the UI, along with the three issue types it raised (missing records, extra
records, weak match key). The endpoints and tables are still there and still
work; nothing links to them.

## The design rule

**The main dashboard shows a summary and nothing else.** `/api/overview` returns
twelve fields per partner and no history, no comparison rows and no crontab
lines. Opening a partner fetches that partner's detail at that moment, which is
why 112 partners cost the same to display as 12, and why a 113th costs the front
page nothing.

Everything below is the detail behind that front page.

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

## Spreadsheet comparison — matching, missing and extra

This is the one thing counts can never answer. `partner_counts` knows we hold
2,013 rows for bokun and the sheet says 85,000, but **a gap of 82,987 is
arithmetic, not a comparison**: two sides can hold the same number of records
and still not hold the same records. Matching / missing / extra require looking
at the rows.

Upload a partner's `.csv` or `.xlsx` on their page and press compare:

| | |
|---|---|
| **Total in spreadsheet** | rows in the uploaded sheet |
| **Total in database** | records we hold for that partner |
| **Matching** | present on both sides |
| **Missing** | in the sheet, with no match in our database |
| **Extra** | in our database, with no row in the sheet |

Missing and extra are not just counts — the rows themselves are stored, paged,
searchable, and downloadable as CSV, because "412 missing" is where the question
starts and the rows are what someone actually works from.

### It picks the match key by measurement, not assumption

A partner sheet might identify a tour by product URL, by an id, or by nothing
but its name. Rather than requiring the operator to know which, every applicable
strategy is scored against the real rows and the one that matches most wins:

| Strategy | Strength |
|---|---|
| Product URL | exact |
| Partner ID | exact |
| Title + start date | strong |
| Title only | weak |

The chosen strategy and its match rate are shown on the page. A comparison built
on a weak key says so, rather than quietly reporting thousands of false
"missing" rows. Below a 10% match rate it is flagged outright as probably
measuring a column mismatch.

Ids are matched against `partner_url` as well as against each other, because a
sheet often carries the bare product id (`a-1029384`) while our column carries
the whole URL ending in it.

### Column names are guessed, never demanded

Partners label the same column `Tour ID`, `product_id` or `Ref`. `sheets.py`
maps what it finds — punctuation and case stripped, longest pattern first — and
the UI shows which column became which field, so a wrong guess is caught before
it becomes a wrong comparison rather than after.

Dates are normalised from Excel serials and thirteen text formats. A date that
does not parse becomes blank rather than a guess: a date parsed wrong turns a
matching tour into a missing one, which is worse than an empty cell.

### It refuses rather than truncates

If a partner has more than `app.max_compare_rows` (100,000) records in MySQL, **no
comparison is produced at all**. A partial diff would report every unread row as
missing — a wrong answer that looks like a real finding. The query asks for
`cap + 1` rows precisely so overflow is detected rather than silently hit.

### Reading .xlsx without a dependency

An `.xlsx` is a zip of XML, and a sheet of tour rows uses none of the hard parts,
so `sheets.py` reads one with `zipfile` + `ElementTree` from the standard
library rather than adding openpyxl. It handles shared strings, rich-text runs,
inline strings, date-styled serials, sparse rows and a first sheet that is not
`sheet1.xml`. Old `.xls` is a different format (a BIFF binary, not a zip) and is
rejected with a message saying so.

The source file is kept on disk and downloadable, because a disputed "412
missing" cannot be settled without the sheet it came from.

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

Spreadsheet comparison and logging add:

```yaml
app:
  upload_dir: "uploads"       # where partner sheets are kept
  max_upload_mb: 25           # rejected at the door, before being read
  max_compare_rows: 100000    # a REFUSAL threshold, not a truncation point
  log_file: "dashboard.log"   # what the Logs page reads
  log_level: "INFO"
```

`max_compare_rows` is the one worth understanding: a partner with more records
than this produces **no comparison at all**, because a partial read would report
every unread row as missing. Raise it if you need to compare a very large
partner, remembering this is a row-by-row read of MySQL rather than a `COUNT()`.

Comparison also needs one query, which is the only one that returns individual
records:

```yaml
queries:
  partner_records: "SELECT id, title, dates, enddates, partner_url, published
                    FROM jos_eventlist_events WHERE partner = %s ORDER BY id LIMIT %s"
```

Column **order** is part of the contract — `compare.fetch_db_rows` unpacks it as
id, title, start, end, url, published. Change the columns if your schema differs,
but keep the six positions. If the key is missing entirely the rest of the
dashboard runs normally and the comparison route reports that it is unavailable,
so an older `config.yaml` will not stop the app from starting.

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

## One definition of "something is wrong"

Before the redesign each page decided for itself what counted as a problem: the
partners table had its own *Problems only* rule, the jobs page ranked by a
different one, and the number on a summary tile agreed with neither. Anyone
reading two pages got two answers.

Everything that can be wrong is now enumerated once, in `issues.py`. The Issues
page lists these, the partner cards count these, and the overview tiles sum
these — so a card reading "4 issues" and the four rows you get after clicking it
are the same four things by construction, not by careful maintenance.

The page shows two groups - **Broken now** and **Worth watching** - and five
columns: who, what happened, **what to do**, when it last ran, and a Run again
button where re-running can actually help. Both earlier versions were hard to
read for the same reason: they described problems in the system's own words
("Ingest job stalled", kind `job_stalled`, scope `partner`, value 118) and left
the reader to work out what that meant for them. Titles are now plain
("Stopped importing events") and every issue carries a one-sentence next step.

Three severities underneath, because a fourth invites arguments about whether
something is "medium" and the useful question is only *does this need me now,
today, or never*:

| | |
|---|---|
| **Critical** | query failed, nothing live, ingest job stalled or removed, a large share of the sheet missing |
| **Warning** | ingest late, half the records never published, extra records in the database |
| **Info** | never counted, comparison key too weak to trust |

The partner status badge is derived from these rather than from a second set of
rules, so a card can never read *Success* beside a red issue count.

## The partner event CSV

The download that carries the **actual event data** — one row per record we hold
for that partner, not one row per hourly collection. Generate it from a
partner's **Generated Files** tab, in one of three scopes: everything we hold,
live only, or never-published only.

**It runs as a background job**, because these are not small. `wcities` is
708,221 rows and 120 MB and takes about five minutes; generating that inside the
request would hold a worker and time the browser out long before the file
existed. So:

```
POST /api/partners/{name}/export?scope=all   ->  {"export": {...}, "joined": false}
GET  /api/partners/{name}/export             ->  status, rows written, size
GET  /download/export/{id}                   ->  the file, once status is "done"
```

The page polls while it runs and shows `rows_written` against the expected
total. Pressing Generate twice **joins the run already in progress** rather than
starting a second scan of the same partner, which is also what a second person
opening the page gets.

Three details worth knowing:

- **Paged by keyset, not OFFSET.** `WHERE id > <last>` rather than `LIMIT n
  OFFSET m`: with OFFSET the server walks and discards every row before the
  window, so page N costs N pages of work. Measured on a real partner, the
  OFFSET form spent most of its time re-reading rows it had already written.
- **Venue names are resolved per batch, never joined.** `venue_details` holds one
  row per partner submission — `wid` 92158 has 56 of them, spelling the country
  four different ways — so joining it would multiply every event by the number
  of times its venue was ever described. Each batch collects its `locid`s and
  resolves them in one grouped lookup, and the lowest id per `wid` wins so all
  four venue fields come from the same submission. Choosing that row in SQL with
  `id IN (SELECT MIN(id) ... GROUP BY wid)` was **15× slower** (26.5s vs 1.7s
  for one partner) because `wid` is not indexed and the subquery scans twice; it
  is done in Python instead, for byte-identical output.
- **A failed export deletes its half-written file.** A partial CSV that downloads
  cleanly is worse than none, because nothing about it says it is short.

Generated files are deleted after `app.export_keep_days` (7) — they are always
reproducible from the database, so keeping them only costs disk.

### Matching your spreadsheet

The columns are **configured, not coded**, so the file can be lined up with
whichever sheet the team reconciles against without touching Python:

```yaml
exports:
  event_columns:
    - [id, "Event ID"]
    - [title, "Title"]
    - [venue, "Venue"]
    # ...
```

Each key is either a column named in `queries.partner_events` or one of the four
filled in from the venue lookup (`venue`, `venue_city`, `venue_state`,
`venue_country`). Columns are matched **by name in the SELECT list**, not by
position, so reordering the query cannot silently shift every value one column
to the left.

> The default column set covers the event fields this database holds. The team's
> per-partner sheets live in Dropbox and are not readable from this machine, so
> they have not been matched field-for-field — set `event_columns` to the sheet's
> own headers once you have one to hand.

## Logs

The dashboard's own log, read per partner on the partner's **Process Logs** tab
(see [activity.py](activity.py)). The reading and filtering below is still what
backs it, and still what `/api/logs` and the log downloads serve — search, filter
by level (a floor, not an exact match — *Warning* means warnings and worse), by
HTTP status class, and by date.

The partner feed drops HTTP access lines that succeeded, and 401/403 as well:
the partner's name appears in the dashboard's *own* request URLs
(`GET /api/partners/venuepilot/logs 200`), so without that filter the feed fills
up with a record of the page that is displaying it.

Lines are read **backwards from the end of the file**, so a log left running for
a month opens as fast as a fresh one — cost is set by how much is displayed, not
by how much has accumulated.

Uvicorn's default output carries no timestamp at all, which makes filtering by
date impossible, so `applog.configure()` installs a formatter that puts one on
every future line. The parser reads both formats, so existing logs stay readable
instead of the page starting empty after the change. An HTTP 5xx is treated as
an ERROR and a 4xx as a WARNING regardless of the level uvicorn logged it at —
otherwise a page of 500s reads as INFO and filtering by level hides the one
thing worth finding.

The file handler is attached to the root logger only, with uvicorn's loggers
made to propagate up to it. Attaching it to both — the obvious thing, since
uvicorn configures its own loggers — writes every line twice.

## Tables

Every table in the dashboard is one component (`static/js/table.js`) with sticky
headers, search, sorting, filters, pagination and expandable rows. State
survives a re-render, which matters because these pages poll: a naive table
would throw away your search, sort and page position every few seconds.

### Sticky headers, and why the old ones weren't

The previous version documented giving up on this. Its table wrapper set
`overflow-x: auto`, which makes the wrapper a scroll container on *both* axes —
so a sticky `<th>` sticks to the top of that container rather than the viewport,
and since the container itself scrolled with the page, the header scrolled away
regardless.

The fix is to let the container own the vertical scroll too: with a `max-height`
on `.table-scroll` the table scrolls inside it, and `position: sticky; top: 0`
is then relative to something that is actually standing still. Horizontal
scrolling still works, and the header scrolls sideways with its columns as it
should. The first column is sticky on the horizontal axis for the same reason —
scrolling a wide table right otherwise leaves rows with no idea which partner
they belong to.

The header's bottom border is a `box-shadow` rather than `border-bottom`,
because a border on a sticky element is painted at its original position and
tears away as the body scrolls under it.

Large row sets are paged **server-side** (`RemoteTable`): a partner can produce
80,000 missing records, and sending them all so the browser can slice out 50
would be slow to fetch, heavy to hold and pointless to render.

## HTTP API

Every route below needs the Basic-auth login once `OPS_AUTH_PASSWORD` is set,
except the `x-agent-secret` POSTs, which never do.

| Endpoint | Purpose |
|---|---|
| `GET /api/overview` | The main dashboard: summary + one card per partner |
| `GET /api/partners` | All partner rows + summary + issue counts |
| `GET /api/partners/{name}` | One partner, in full |
| `GET /api/partners/{name}/history` | Count history for a partner |
| `POST /api/partners/{name}/refresh` | Re-count one partner now |
| `POST /api/counts/refresh` | Re-count every partner now |
| `POST /api/partners/{name}/upload` | Upload a spreadsheet (raw body, `X-Filename` header) |
| `POST /api/partners/{name}/compare` | Diff that sheet against the database |
| `GET /api/partners/{name}/comparison` | The stored comparison — counts only |
| `GET /api/comparisons/{id}/rows` | A page of missing/extra rows (`?side=&q=&offset=`) |
| `DELETE /api/uploads/{id}` | Remove a sheet and everything derived from it |
| `GET /api/partners/{name}/jobs` | That partner's crontab lines, grouped by category |
| `GET /api/partners/{name}/logs` | That partner's process log |
| `GET /api/partners/{name}/files` | That partner's files: event CSVs, sheets, comparison rows |
| `POST /api/partners/{name}/export` | Start generating the event CSV (`?scope=all\|live\|unpublished`) |
| `GET /api/partners/{name}/export` | Newest export for that partner, plus history |
| `GET /api/exports/{id}` | One export's status, polled while it generates |
| `DELETE /api/exports/{id}` | Remove a generated file |
| `GET /api/watchlist` | The partners someone has starred |
| `POST /api/watchlist/{name}` | Star a partner (`DELETE` to unstar, `PUT` to replace the list) |
| `GET /api/issues` | Every open issue (`?severity=&scope=&kind=&q=`) |
| `GET /api/logs` | Filtered, paged log lines |
| `GET /api/logs/files` | Which logs can be read |
| `GET /api/downloads` | The Downloads catalogue |
| `GET /api/reports/summary` | The management report |
| `GET /api/settings` | What this instance is configured to do |
| `GET /api/sites` | Website health rows + alert status |
| `POST /api/sites/refresh` | Check sites now (optional `?url=`) |
| `GET /api/alerts` | Alert config + log of mails sent |
| `POST /api/alerts/test` | Send a test alert to every recipient |
| `GET /api/pm2/status` | PM2 state per server |
| `POST /api/pm2/report` | Agent heartbeat (needs `x-agent-secret`) |
| `POST /api/cron/report` | Server crontab push (needs `x-agent-secret`) |
| `GET /api/jobs` | Scheduler state |
| `GET /api/db/ping` | MySQL connectivity check |

Uploads are sent as the **raw request body** with the filename in a header,
rather than as a multipart form. Multipart would pull in `python-multipart` — a
seventh dependency for a single button — and `fetch(url, {body: file})` does
this natively in the browser, so nothing is lost on the client side.

### Downloads

| Route | Contents |
|---|---|
| `/download/partners.csv` | Every partner: counts, job state, cron status, issue count |
| `/download/issues.csv` | Every open issue |
| `/download/sites.csv` | Website health |
| `/download/cron.csv` | The whole crontab inventory |
| `/download/partner/{name}/history.csv` | One partner's count history |
| `/download/comparison/{id}/missing.csv` | The rows behind a comparison (also `extra.csv`) |
| `/download/export/{id}` | A generated partner event CSV |
| `/download/cron/{id}/output` | One cron job's own output file (tail by default) |
| `/download/cron-fetch/{id}` | A big output file fetched in the background |
| `/download/log/{name}` | A whole log file |
| `/download/logs.csv` | The log as currently filtered |
| `/download/upload/{id}` | An uploaded spreadsheet, as it was uploaded |

A requested log name is matched against the known list rather than joined onto a
directory: a name is user input, and `../../etc/passwd` must not resolve to
anything.

## Files

```
dashboard.py    FastAPI app, routes, aggregation, downloads
auth.py         HTTP Basic auth middleware (the .htaccess equivalent)
config.py       config.yaml + .env loading
config.yaml     partners, queries, websites, intervals, upload limits
mysql.py        read-only MySQL access + the SELECT-only guard
counts.py       runs the counts for one partner
sheets.py       CSV/XLSX reading and column detection, standard library only
compare.py      spreadsheet vs database diff, and choosing the match key
issues.py       the one definition of "something is wrong"
activity.py     one partner's process log, assembled from what we already store
applog.py       log formatting, tail-first reading, filtering
exports.py      CSV generation for every download
event_export.py the partner event CSV - batched read, background job
health.py       one website check
alerts.py       down/recovery emails (state machine + SMTP)
scheduler.py    the background loops
store.py        SQLite history, uploads and comparisons (ops.db)
check_db.py     pre-flight connectivity/query check
agent.py        PM2 + crontab reporter, runs on each target server

templates/      partners.html is the workspace; base.html is the shell
static/css/     tokens.css (palette and scale), layout.css, components.css
static/js/      core.js (helpers, badges, polling, slide-over), table.js
uploads/        partner spreadsheets, kept so a comparison can be checked
exports/        generated partner event CSVs, pruned after export_keep_days
```

## Notes on the UI

The whole palette and scale live in `static/css/tokens.css`. Light surfaces,
two border weights, one accent hue, and no gradients or heavy shadows — depth is
carried by borders, and shadow is kept for things that genuinely float (the
slide-over, a toast).

- **Colour never carries meaning alone.** Every status badge has a text label
  *and* a glyph, because green and red sit close together for the ~8% of men
  with deuteranopia. Colour only reinforces a label that already says what the
  state is. Text pairs measure 4.5:1 or better, badge pairs 7:1 or better.
- **A meter is one hue.** It encodes magnitude, not state; a red/amber/green bar
  would say "bad" about a number that is merely small. The figure beside it does
  the precise work.
- **The comparison bar is the exception**, and deliberately: matching / missing /
  extra are a composition that adds up to the whole sheet, and they do map onto
  how much attention each deserves. Below ~6% a segment drops its label rather
  than squeezing it, because the legend underneath carries every number anyway.
- **Tiles tint the figure, not the tile.** A wall of coloured blocks is harder to
  read at a glance than one coloured number among plain ones.
- Numbers use Indian grouping (`9,26,934`) with lakh/crore shorthand beneath,
  and tabular numerals everywhere, so columns align and a live-updating figure
  does not jitter.
- **The status legend is printed, not just hovered.** A tooltip is invisible on
  a touch screen and unfindable if you don't know to hover, so the same wording
  exists in a collapsible legend under the table it belongs to. Both come from
  one definition in `core.js`.
- The overview sorts **worst first** by default. On a page meant to answer "who
  needs me?" in a few seconds, alphabetical order buries the answer somewhere
  around letter M.
- Polling **pauses while the tab is hidden**. This is the page people leave open
  all day; a background tab polling every 5s is thousands of pointless requests.
  It catches up immediately on return.
- Responsive: the sidebar becomes an overlay below 1080px, tiles go two-up below
  720px and one-up below 480px, and wide tables scroll inside their own
  container so the page body never scrolls sideways.

### Settings is read-only, on purpose

`config.yaml` and `.env` are the record of how an instance is configured. A
settings page that could write them would put the running process and the file
it was loaded from permanently out of step — and the file is what survives a
restart, gets committed, and gets copied to the next server. So the page shows
what is in force, the SQL behind every number, and where to change it.

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

Current state — **431 jobs across 4 servers**, both main boxes re-collected
over SSH on 2026-08-04:

| Server | Jobs | Collected |
|---|---|---|
| `44.198.210.209` (ip-10-0-0-153) | 272 | 2026-08-04 |
| `3.94.49.56` (ip-10-0-0-242) | 145 | 2026-08-04 |
| `100.52.8.134` (ip-10-0-0-117) | 12 | 2026-07-30 |
| `10.0.0.117` (vishal-konale) | 2 | 2026-07-30 |

311 active, 120 commented out, 54 matched to a known partner across 31 partners.

Both main servers answer password auth, so `collect-crons.py` works against
them directly:

```bash
./venv/bin/python collect-crons.py 44.198.210.209 3.94.49.56
```

The partner script directories those crontabs run out of are:

```
44.198.210.209:/var/www/html/admin/administrator/components/com_events_venue/   172 dirs
3.94.49.56:/home/fcampbell/eventPartner_174/                                     78 dirs
3.94.49.56:/home/fcampbell/eventPartner/                                         72 dirs
```

Those three paths are exactly the markers `cron_parse.guess_partner()` looks
for, which is why a partner name can be recovered from a cron line at all.

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

### Not every cron job is a data insertion

They were all presented as though they were. Of the 431 lines collected, only
about a fifth actually insert events; the rest watch processes, generate CSV
feeds, unpublish duplicates or push images to a CDN — different work, different
people, and a different reaction when one breaks.

`cron_parse.categorise()` files each line into one of six:

| Category | What lands there | Count |
|---|---|---|
| **Website Health** | watchdogs, process killers, connectivity probes | 13 |
| **Scrapers** | scrapers, crawlers, feed downloads | 17 |
| **Import Jobs** | anything that inserts or updates event records | 84 |
| **CSV & Reports** | feed exports, counts, mailed reports | 43 |
| **Maintenance** | unpublishing duplicates, expiry, image and index upkeep | 100 |
| **Other** | genuinely miscellaneous one-offs | 174 |

(431 lines, collected from both servers on 2026-08-04.)

Two things make this work on real data:

- **It classifies the script path, not the command line.** 75 of these lines end
  in `> something.csv`, so matching the whole command would file three quarters
  of the crontab under "CSV".
- **Order is by specificity, not by display order.** Partner ingest lives under
  `com_events_venue/`, but so does `com_events_venue/UnpublishDuplicates/`, which
  is housekeeping — so the maintenance rule is tested before the directory-wide
  import rule.

The category is stored on each row and re-derived for rows collected before the
column existed, so an old database does not show everything as "Other". The
Processes page filters by it, and a partner's **Jobs** tab groups by it.

### Partner names in crontabs are matched, not invented

`guess_partner()` used to return the raw directory when it matched no known
partner, which invented three: `UnpublishDuplicates` (with 20-odd jobs),
`alternateCron`, and `daily-event-cron.sh >` — and every one of them then
appeared as a partner in the cross-reference. Now a candidate must look like a
directory name (no spaces, no redirect, no script extension) and must not be one
of the shared-machinery directories in `cron_parse.NOT_A_PARTNER`.

Directories that *are* partners but appear in neither MySQL nor the status sheet
are still kept — `fandango`, `ticketsnow` and `reservix` all have crons while
having no rows and no sheet line, which is worth seeing.

Two further fixes came out of reading the real crontabs:

- **The parser is told about every partner we know of**, not just the ones in
  the status sheet. It used to be passed `partner_meta` alone, which discarded
  the 120 partners already discovered from MySQL.
- **Wrapper scripts in the partner root are matched by filename.** 16 lines are
  shaped like `com_events_venue/viagogo_weekly_event_cron.sh` — a script rather
  than a partner directory. The partner name is taken from the filename, but
  only ever by matching against the known list, longest name first, so
  `daily_event_cron_eventim_ticketcity_eventim-uk.sh` resolves to `eventim-uk`
  rather than `eventim`.

Net effect on real data: 3 invented partners gone, and attribution up from 18
partners / 37 lines to **31 partners / 54 lines**. The 34 lines still
unattributed under a partner root are correct: 17 are `UnpublishDuplicates`,
and the rest are generic multi-partner wrappers (`weekly-event-cron.sh`,
`not_inserted_cron_185.sh`) that name no partner at all.

### Partners with cron jobs and no data

Nine of the 31 have scripts on disk and **no rows in MySQL** — `sportsradar`
has 18 scheduled jobs and nothing in the database, and `fandango`,
`ticketsnow`, `reservix`, `bemyguest`, `Black_Widow`, `stats_sports`,
`ticketpoint_nl` and `eventim-uk` are the same shape.

They are named on the Processes page but **not linked**, because the partner
list is discovered from MySQL and `/partners/<name>` 404s for them. `/api/cron`
carries `partner_known` per row so the UI can tell the difference. A job
running for a partner we hold nothing for is one of the more useful things on
that page — it should be visible, and it should not be a dead link.

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
