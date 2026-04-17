# cctop

htop-style monitor for system load and **live Anthropic quota** across every Claude Code profile on the machine.

Shows:
- CPU / Mem / Swp meters (like htop's top bar)
- Every Claude Code profile's **5-hour** and **7-day** usage quota, pulled from Anthropic's authoritative `/api/oauth/usage` endpoint — the same data the `/usage` slash command shows inside Claude Code
- Per-account state: green (in use), white (idle), yellow (5h locked), red (7d locked), dim (token expired or rate limited)

No JSONL parsing, no approximations, no cost estimates — this is the real number Anthropic counts against your plan.

## Install

```bash
pipx install git+https://github.com/remiBoudreau/cctop.git
```

Or clone and run directly:

```bash
git clone https://github.com/remiBoudreau/cctop.git
cd cctop
python3 -m venv venv
venv/bin/pip install textual httpx psutil rich
venv/bin/python cctop.py
```

Requires Python 3.11+.

## Usage

```bash
cctop                              # launch the TUI
cctop --dump                       # single plain-text snapshot, exit
cctop --sort 7d                    # sort by 7-day utilization descending
cctop --warn 50 --crit 80          # custom color thresholds
cctop --interval-focus 30          # faster polling when an account is active
```

### Keys

| Key           | Action                                  |
|---------------|-----------------------------------------|
| `F5` / `r`    | Manual refresh (15s cooldown)           |
| `F6` / `s`    | Cycle sort (5h → 7d → name)             |
| `F10` / `q`   | Quit                                    |

## How it works

1. **Profile discovery.** Scans `~/.claude-profiles/<name>/.credentials.json` for every profile on disk. Each profile gets one row.
2. **Token read.** Reads each profile's OAuth access token. Read-only — never rotates or writes the credential file.
3. **API poll.** Calls `GET https://api.anthropic.com/api/oauth/usage` with the profile's bearer token. Parses `five_hour` and `seven_day` utilization + reset time.
4. **In-use detection.** Checks `mtime` on `~/.claude-profiles/<name>/projects/**/*.jsonl`. If any file was touched in the last 120 seconds, the account is "in use" and its name shows green.
5. **Rate-limit backoff.** Anthropic's `/api/oauth/usage` rate limits aggressively with inconsistent `Retry-After` headers. cctop tracks `consecutive_429` per account and walks a local backoff schedule (60s → 180s → 300s → 600s). Stale data is preserved and shown alongside the cooldown timer so you still see the last-known utilization while waiting.
6. **Render.** `Textual` + `Rich` with the `textual-ansi` theme so your terminal's native palette (including background) shows through, like htop.

## Colors

Cell colors in each bar are **position-based**, not fill-based — the same gradient trick htop uses:

- Green  for cells at positions 0–`warn`% (default 60)
- Yellow for cells at positions `warn`–`crit`% (default 85)
- Red    for cells at positions `crit`–100%

A bar at 40% shows all green. A bar at 75% shows green up to 60%, then yellow for the 60–75% range. A bar at 95% shows green → yellow → red.

Account name colors (left column):

| Name color    | Meaning                                          |
|---------------|--------------------------------------------------|
| green         | in use **and** both quotas below 100             |
| white         | idle **and** both quotas below 100               |
| yellow        | 5-hour quota locked (100%), 7-day still has room |
| red           | 7-day quota locked (100%)                        |
| gray          | data unavailable (token expired / API error)    |

## Keeping dormant profiles from showing "expired"

cctop reads static credential files. OAuth access tokens have a short TTL (~8-24h) and only get refreshed when Claude Code is actively used — dormant profiles end up showing **expired** even though their refresh tokens are still valid.

The bundled `claude-token-refresh.sh` + systemd user timer fixes this. Quick install:

```bash
mkdir -p ~/.config/systemd/user
cp ~/cctop/systemd/claude-token-refresh.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-token-refresh.timer
```

**Full documentation: [REFRESH.md](REFRESH.md)** — covers cron alternative, environment variables, troubleshooting, and failure handling.

## Design notes

- **Read-only credentials.** Never writes `.credentials.json` — never races with a running Claude Code instance refreshing its own token.
- **Parallel polling, isolated failures.** All accounts poll concurrently via `asyncio.gather`. One failing account never affects the others.
- **Snapshot preservation.** A 429 or empty response never wipes previously-known good data. You see the last-known bars plus a `429 Nm` cooldown indicator in the reset column.
- **Responsive layout.** Bars scale with terminal width. Resize the window and the meters reflow.
- **No Sonnet/Opus/overage tracking** by design — the tool shows only `five_hour` and `seven_day` which are the two windows that matter for staying under your plan.

## Requirements

- Python 3.11+
- `textual`, `httpx`, `psutil`, `rich`
- A Claude Code installation with at least one profile under `~/.claude-profiles/`

## License

MIT — see [LICENSE](LICENSE).
