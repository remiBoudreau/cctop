# claude-token-refresh

Keep Claude Code OAuth tokens fresh for every profile so `cctop` never reports "expired" for accounts that actually have valid refresh tokens.

## The problem

cctop reads the static credential file at `~/.claude-profiles/<name>/.credentials.json` to check `expiresAt`. When that timestamp has passed, cctop marks the profile as **expired** and skips the usage API poll.

Claude Code itself refreshes access tokens automatically when you USE a profile (it exchanges the long-lived refresh token for a new short-lived access token). But dormant profiles never get used, so their access tokens expire (~8-24h TTL) and stay stale. The refresh tokens are still valid for weeks/months — nothing is actually wrong — but cctop's read-only check can't tell the difference between "access token expired but refreshable" and "genuinely dead account."

## What this script does

- Walks every directory under `~/.claude-profiles/`
- For each profile with a `.credentials.json`, reads `expiresAt`
- If the access token is expired or expiring within the next hour, runs:
  ```bash
  CLAUDE_CONFIG_DIR=<profile> claude mcp list
  ```
- `claude mcp list` is a free operation — it lists configured MCP servers. No model API call, no tokens consumed, no charges. It does make an authenticated request internally, which triggers Claude Code's built-in refresh flow as a side effect. The credential file is rewritten with a new access token.
- Logs every action to `~/.local/share/cctop/refresh.log`

## Install (systemd user timer)

```bash
mkdir -p ~/.config/systemd/user
cp ~/cctop/systemd/claude-token-refresh.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-token-refresh.timer
```

The timer fires 2 minutes after boot, then every hour with a 5-minute randomized jitter to avoid hammering Anthropic with concurrent refresh requests across machines.

### Verify

```bash
# Is the timer scheduled?
systemctl --user list-timers claude-token-refresh

# Last run status (should be Finished / status=0/SUCCESS)
systemctl --user status claude-token-refresh

# Live log
tail -f ~/.local/share/cctop/refresh.log
```

A healthy log looks like:
```
2026-04-16 22:29:38 --- refresh run start ---
2026-04-16 22:29:38 jmrb: token valid for 24283s, no refresh needed
2026-04-16 22:29:38 manbearpig-agent: token expires in -430s, refreshing...
2026-04-16 22:29:52 manbearpig-agent: REFRESHED, new token valid for 28800s
2026-04-16 22:29:52 --- refresh run done: 8 profiles, 0 failed ---
```

### Lingering (optional but recommended)

By default systemd user units run only while the user has an active login session. If you want the timer to keep running when you're not logged in (e.g., headless pentest workstation), enable lingering:

```bash
sudo loginctl enable-linger $USER
```

## Alternative: cron

If you don't use systemd:

```bash
crontab -e
# Add:
0 * * * * /home/YOUR_USER/cctop/claude-token-refresh.sh
```

## Manual run

Useful for one-off testing or when you want immediate refresh:

```bash
~/cctop/claude-token-refresh.sh
tail ~/.local/share/cctop/refresh.log
```

The script serializes concurrent runs via `flock` on `/tmp/claude-token-refresh.lock`, so running it manually while the timer is active is safe.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_PROFILES_DIR` | `$HOME/.claude-profiles` | Where profile dirs live |
| `REFRESH_THRESHOLD_SECS` | `3600` | Refresh if the token expires within this window. Set higher to refresh more aggressively, lower to refresh only when strictly expired. |
| `LOG_FILE` | `$HOME/.local/share/cctop/refresh.log` | Append-only log. Rotate with logrotate if it grows. |
| `CLAUDE_BIN` | `claude` | Override if `claude` isn't on PATH when systemd runs the service. The bundled service file sets a sane PATH. |

## When refresh fails

If the refresh_token itself has fully expired (typically after months of disuse, or after revocation), the script logs:

```
manager: claude ran but token NOT refreshed (refresh_token may be invalid — re-login required)
```

**This is not a script failure** — it's expected behavior when a profile has been truly abandoned. The script exits 0 so systemd doesn't flag the unit as failed for a user-side credential issue. The affected profile name is in the log.

To recover that profile, run the interactive re-login once:

```bash
CLAUDE_CONFIG_DIR=~/.claude-profiles/<profile-name> claude login
```

Follow the browser flow, and the next automated refresh will pick up the new credentials. Nothing to configure, no sudo needed.

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | Script ran to completion. Per-profile refresh failures are logged but not propagated (they're user-side credential issues, not script issues). |
| 1 | Script-level error: profiles dir missing, `claude` or `jq` not found on PATH, another refresh run is already in progress. |

## Design choices

- **Use `claude mcp list` instead of implementing OAuth directly.** The refresh endpoint, client_id, and beta header are Claude Code implementation details that can change. Shelling out to the official CLI means we automatically track whatever Anthropic changes.
- **`mcp list` is free.** It's a local config enumeration, not a model call. Tested: zero change in `/api/oauth/usage` response before and after invocation.
- **Serialize with `flock`.** Prevents the timer firing on top of a previous run that's still working through 8 profiles, or a user running it manually while the timer is active.
- **Per-profile failures don't propagate.** A single dormant account with an invalid refresh_token should not cause systemd to mark the whole unit as failed — systemd's retry logic and alerting would then misfire on a totally normal "please re-login to that account" situation.
- **Randomized timer jitter.** `RandomizedDelaySec=5min` so workstations across a team don't all hit Anthropic at minute zero.
