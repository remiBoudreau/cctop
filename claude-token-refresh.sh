#!/bin/bash
# ============================================================================
# claude-token-refresh — Background OAuth refresh for Claude Code profiles
# ============================================================================
# cctop reports "expired" for accounts whose OAuth access_token has passed
# its expiresAt timestamp. Claude Code CLI auto-refreshes tokens when in
# USE, but dormant profiles never get refreshed, so their stored tokens
# expire (~8-24h TTL) and stay stale.
#
# This script iterates ~/.claude-profiles/*/ and, for each profile with
# an expired or soon-to-expire token, invokes `claude mcp list` in that
# profile. That command is:
#   - Fast (completes in ~1s)
#   - Free (no model API call, no tokens consumed)
#   - Triggers the refresh_token -> access_token exchange as a side
#     effect, rewriting .credentials.json with a fresh access_token
#
# Intended to run on a systemd timer or cron every 1-4 hours.
#
# Environment variables:
#   CLAUDE_PROFILES_DIR    Default: $HOME/.claude-profiles
#   REFRESH_THRESHOLD_SECS Refresh if token expires within this window
#                          Default: 3600 (1 hour)
#   LOG_FILE               Default: $HOME/.local/share/cctop/refresh.log
#   CLAUDE_BIN             Override the claude binary path. Default: claude
#
# Exit codes:
#   0  script ran to completion (per-profile failures are logged, not propagated
#      — a profile with an invalid refresh_token is a user issue, not a script
#      issue, so we don't want systemd marking the unit as failed for that)
#   1  script-level error (profiles dir missing, claude/jq not installed,
#      another instance already running, etc.)
# ============================================================================

set -euo pipefail

PROFILES_DIR="${CLAUDE_PROFILES_DIR:-$HOME/.claude-profiles}"
REFRESH_THRESHOLD_SECS="${REFRESH_THRESHOLD_SECS:-3600}"
LOG_FILE="${LOG_FILE:-$HOME/.local/share/cctop/refresh.log}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
LOCK_FILE="/tmp/claude-token-refresh.lock"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

refresh_profile() {
    local profile_dir="$1"
    local name="$(basename "$profile_dir")"
    local creds="$profile_dir/.credentials.json"

    [ -f "$creds" ] || { log "$name: no credentials.json, skipping"; return 0; }

    local expires_at
    expires_at=$(jq -r '.claudeAiOauth.expiresAt // empty' "$creds" 2>/dev/null || echo "")
    if [ -z "$expires_at" ] || [ "$expires_at" = "null" ]; then
        log "$name: no expiresAt field, skipping"
        return 0
    fi

    local refresh_token
    refresh_token=$(jq -r '.claudeAiOauth.refreshToken // empty' "$creds" 2>/dev/null || echo "")
    if [ -z "$refresh_token" ] || [ "$refresh_token" = "null" ]; then
        log "$name: no refreshToken, cannot refresh"
        return 1
    fi

    local now_ms
    now_ms=$(date +%s%3N)
    local remaining_secs=$(( (expires_at - now_ms) / 1000 ))

    if [ "$remaining_secs" -gt "$REFRESH_THRESHOLD_SECS" ]; then
        log "$name: token valid for ${remaining_secs}s, no refresh needed"
        return 0
    fi

    log "$name: token expires in ${remaining_secs}s, refreshing..."

    # `claude mcp list` triggers refresh_token -> access_token exchange as a
    # side effect without consuming model API credits. Output discarded.
    if CLAUDE_CONFIG_DIR="$profile_dir" timeout 20 "$CLAUDE_BIN" mcp list >/dev/null 2>&1; then
        local new_expires
        new_expires=$(jq -r '.claudeAiOauth.expiresAt // empty' "$creds" 2>/dev/null || echo "")
        if [ -n "$new_expires" ] && [ "$new_expires" != "$expires_at" ]; then
            local new_remaining=$(( (new_expires - now_ms) / 1000 ))
            log "$name: REFRESHED, new token valid for ${new_remaining}s"
            return 0
        else
            log "$name: claude ran but token NOT refreshed (refresh_token may be invalid — re-login required)"
            return 1
        fi
    else
        log "$name: claude command FAILED (timeout or error)"
        return 1
    fi
}

main() {
    if [ ! -d "$PROFILES_DIR" ]; then
        echo "Profiles dir not found: $PROFILES_DIR" >&2
        log "FATAL: profiles dir not found: $PROFILES_DIR"
        exit 1
    fi

    if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
        echo "claude binary not found: $CLAUDE_BIN" >&2
        log "FATAL: claude binary not in PATH"
        exit 1
    fi

    if ! command -v jq >/dev/null 2>&1; then
        echo "jq not found in PATH" >&2
        log "FATAL: jq not installed"
        exit 1
    fi

    log "--- refresh run start ---"
    local failed=0 total=0
    for profile in "$PROFILES_DIR"/*/; do
        [ -d "$profile" ] || continue
        total=$((total + 1))
        if ! refresh_profile "$profile"; then
            failed=$((failed + 1))
        fi
    done

    log "--- refresh run done: $total profiles, $failed failed ---"
    if [ "$failed" -gt 0 ]; then
        # Per-profile refresh failures are expected when a refresh_token
        # has fully expired (requires interactive re-login). This is NOT
        # a script failure - we still exit 0 so systemd doesn't flag the
        # unit as failed. The affected profile name is in the log.
        log "(non-fatal: $failed profile(s) need interactive re-login - see log)"
    fi
    exit 0
}

# Serialize concurrent runs (cron + manual, or overlapping timers).
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "another refresh run is already in progress, skipping"
    exit 0
fi

main "$@"
