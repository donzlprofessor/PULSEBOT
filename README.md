# PULSEBOT — Phase 2

A Telegram bot for scheduled and on-demand website/API checks with incident alerts.
Endpoints belong to the
Telegram user who saved them and survive restarts in SQLite. Built with Python 3.12+,
aiogram 3, aiohttp, SQLAlchemy 2 async, aiosqlite and Pydantic Settings.

## Windows PowerShell setup

Install Python 3.12 or newer, then run from this folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
notepad .env
```

Create a bot using Telegram's @BotFather and set `BOT_TOKEN` in `.env`. Keep this
file private; it is ignored by Git. Never paste tokens into commands or logs.
Environment variables override `.env`. No shell activation is required.

If the Windows launcher does not find an installed Python, use its full path for
the first command, for example:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m app
```

Open a private Telegram chat with your bot. Stop locally with Ctrl+C. Run only one
polling process for a bot token.

## Commands

| Command | Behavior |
| --- | --- |
| `/start`, `/help` | Usage guide |
| `/add https://example.com/health` | Save an endpoint; returns its ID |
| `/list` | List your endpoints |
| `/remove 1` | Remove your endpoint |
| `/check 1` | Send an HTTPS GET and report status and latency |
| `/status` | Show the latest recorded state of your endpoints |
| `/history 1` | Show your endpoint's last 10 checks, newest first, in UTC |

An endpoint is healthy when its final response status is 200–399. HTTP 4xx/5xx
responses are unhealthy. Connection, DNS, TLS, timeout and blocked-destination
failures produce friendly messages. Latency is measured with a monotonic clock
through receipt of final response headers, including DNS, TLS and redirects; the
body is not downloaded. Queue time waiting for a check slot is excluded.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | required | Secret Telegram token |
| `DATABASE_URL` | `sqlite+aiosqlite:///./pulsebot.db` | SQLite database relative to current folder |
| `HTTP_TIMEOUT_SECONDS` | `10` | Total per-check deadline, including redirects (up to 60s) |
| `MAX_REDIRECTS` | `3` | Maximum redirect hops (0–10) |
| `MAX_CONCURRENT_CHECKS` | `10` | Simultaneous HTTP checks (1–100) |
| `MONITOR_INTERVAL_SECONDS` | `60` | Interval between cycle starts (1–86400 seconds) |

## Architecture

```text
app/
  __main__.py   configuration, database lifecycle, Telegram long polling
  config.py     validated environment settings; token stored as SecretStr
  handlers.py   command parsing, private-chat enforcement, friendly responses
  db.py         endpoint model and user-scoped async repository
  checker.py    reusable bounded HTTP checker, redirects and result model
  security.py   URL policy, validating DNS resolver and socket factory
  monitor.py    shared check coordination, scheduler and incident delivery
  instance.py   local OS lock for one process per SQLite database
tests/          offline configuration, security, checker, storage and handler tests
```

The dispatcher injects the repository and monitoring service into handlers. Every lookup and
deletion includes the Telegram user ID. A database constraint prevents duplicate
URLs for the same user. Schema creation happens at startup; no migration framework
is included in this phase. On startup, Phase 2 adds `checks` and `incidents` tables
without changing or deleting existing Phase 1 endpoint records. Tests cover this upgrade.

## Scheduling and incidents

The first cycle starts immediately; subsequent cycles start every 60 seconds by
default while the process runs. A slow cycle finishes before another starts; missed
ticks are not replayed. Endpoints are read in bounded batches. Manual `/check` and
scheduled checks share a concurrency limit and per-endpoint exclusion: a manual
request reports when a check is already running, and the scheduler skips a busy
endpoint. All use the same timeout, TLS verification and SSRF-protected checker.

Every completed check stores its UP/DOWN state, HTTP status (nullable for connection
failures), latency, safe error text, start time and completion time in UTC. Both manual
and scheduled checks update the same state. The first result establishes a baseline
(even if DOWN) and sends no alert. UP → DOWN sends an incident alert; DOWN → UP sends
a recovery alert. Repeated results in the same state send nothing. State and an outbox
event are committed together in a serialized SQLite transaction. History and pending
events survive restarts; restarting does not reset the baseline.

An independent task sends outbox events only to the endpoint owner's private chat,
using a 15-second send timeout. Events are claimed atomically before contacting
Telegram, so the same event is never automatically resent after an ambiguous timeout
or crash. Telegram's explicit rate-limit rejection is retried after its requested
delay. Other failed/uncertain sends remain recorded without automatic retries.
This is **at-most-once delivery**, not guaranteed delivery: Telegram has no idempotency
key, so preventing duplicate alerts means a crash between claiming and sending can
lose an alert. Pending, unclaimed events are delivered after restart. Incident IDs
appear in messages for correlation. The outbox stores `pending`, `attempted`, `sent`
and `uncertain` delivery states; `attempted` may mean interrupted delivery.

A local OS file lock beside the SQLite database prevents two processes from running
schedulers against the same resolved database path. The lock is released by the OS
on process exit; its file may remain and should not be deleted while running. This
supports one host and a local file-backed SQLite database, not distributed workers,
network filesystems or separate database files for the same bot. Do not run Phase 1
and Phase 2 processes together. Ctrl+C stops scheduling, cancels and awaits active
checks/alert delivery, then closes Telegram and database resources. Incomplete checks
are not saved as DOWN. Removing an endpoint also deletes its history and pending events.

## SSRF policy

Only HTTPS on port 443 is accepted. Credentials, control characters, backslashes,
local names, private, loopback, link-local, shared, multicast, reserved and other
non-global IP addresses are blocked. IPv6 transition/mapped addresses are also
blocked. Domain names must have public-style DNS labels. URL validation happens
before saving and again before checking. Saving a hostname does not resolve it or
promise reachability: DNS is evaluated at connection time.

The custom resolver checks **all** returned addresses and rejects mixed public and
private answers. DNS caching is disabled and connections are not reused. A socket
factory independently validates the exact numeric destination immediately before
socket creation, including IP literals which bypass the resolver. The connector
connects to those validated numeric addresses, avoiding a second unchecked DNS lookup.
Redirects are followed manually with the same policy and a hop limit. TLS certificate
verification stays enabled; environment proxies and cookies are disabled.
The checker uses Python's default TLS context (including system trust) supplemented
with certifi's CA bundle. This supports Windows Python installations whose local
issuer store is incomplete, while retaining certificate and hostname verification.
See the [aiohttp connector documentation](https://docs.aiohttp.org/en/stable/client_advanced.html)
for the underlying resolver and socket-factory interfaces.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Tests mock HTTP, DNS and Telegram interactions, use temporary SQLite databases and
block real socket connections/DNS. No Telegram token, `.env` or network is needed.

To diagnose connectivity without reading `.env`, using Telegram, or touching the
database, run the fixed-target diagnostic:

```powershell
.\.venv\Scripts\python.exe -m scripts.diagnose_https
```

This contacts only `https://example.com` (no redirects), compares verified system
TLS trust with system trust plus certifi, then runs the application checker. It prints
only status, exception types and numeric error codes. A code 20 certificate failure
with system defaults followed by HTTP 200 with certifi identifies a missing local
issuer trust problem. Network sandbox permission failures are separate from TLS failures.

## Verify scheduled monitoring locally

Restart the bot with the existing `.env` and database (do not copy `.env.example`
over your configuration):

```powershell
.\.venv\Scripts\python.exe -m app
```

In your private bot chat, use `/list`, `/check 1`, `/status` and `/history 1` (replace
`1` with your endpoint ID). Wait at least one interval and run `/history 1` again;
another scheduled result should appear. For a transition test, use a public HTTPS
endpoint you control: establish UP, change it to return 503, wait for DOWN and one
alert, keep it DOWN for another cycle (no new alert), then restore 200 and wait for
one UP alert. Restart and confirm history persists and unchanged state sends no alert.
Starting a second process with the same database should exit with an already-running
message. No Telegram/network access is used by the automated tests.

## Limitations

- No AI, dashboard or cloud deployment.
- Only public HTTPS on port 443; no authentication headers, custom request methods,
  payloads, response-body assertions or internal endpoints.
- No uptime percentages, general migration framework, user quotas or history retention
  policy. History grows until its endpoint is removed. No checks run while the process
  is stopped, and no missed intervals are backfilled. Alert delivery has the at-most-once
  limitation described above. The concurrency bound limits active checks, not queued
  Telegram updates.
- URLs are stored as plaintext and visible to their owner in Telegram. Avoid secrets
  in paths/query parameters. SQLite and local file access must be protected by the operator.
- Checks reflect one network vantage point and header availability, not application
  correctness. Application filtering cannot protect against unusual routing or NAT
  configurations that remap public addresses; network egress controls are an additional
  production safeguard. No deployment configuration is included.
- Dependencies use compatible version ranges rather than a reproducible lockfile.
  Review and lock resolved dependencies before a production release.
