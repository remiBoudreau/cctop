#!/usr/bin/env python3
"""cctop — htop-style monitor for system load + Anthropic quotas across Claude Code profiles.

Displays CPU / Mem / Swp and the 5-hour + 7-day utilization of every Claude Code
profile under ~/.claude-profiles/, reading the real quota from Anthropic's
`/api/oauth/usage` endpoint (same data source as Claude Code's own `/usage`
command). Read-only — never refreshes OAuth tokens, never writes credentials.

Invocation:
    cctop              # TUI (textual)
    cctop --dump       # single plain-text snapshot, no TUI

Flags:
    --warn N           yellow threshold percent (default 60)
    --crit N           red threshold percent (default 85)
    --sort MODE        5h | 7d | name (default 5h)
    --interval-focus S poll seconds when any account is in-use (default 30)
    --interval-blur S  poll seconds when all accounts idle (default 120)

Name column colors (per-account state):
    green       in-use, both quotas below 100
    white       idle, both quotas below 100
    yellow      5h quota locked, 7d still has room
    bright_red  7d quota locked
    gray        data unavailable (token expired, API error, stale)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import psutil
from rich.console import Console
from rich.text import Text

# ─── configuration ─────────────────────────────────────────────────────────────

PROFILES_DIR = Path.home() / ".claude-profiles"
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

DEFAULT_WARN = 60.0
DEFAULT_CRIT = 85.0
# The /api/oauth/usage endpoint rate limits aggressively: a sustained burst
# trips Retry-After: 300 (5 minutes). Anthropic doesn't publish the budget,
# but empirically ~5 rapid calls is the burst limit. 60s focused / 300s idle
# keeps us well under that with 8 accounts (~8 req/min peak, ~1.6 avg).
DEFAULT_INTERVAL_FOCUS = 60
DEFAULT_INTERVAL_BLUR = 300

# System meter (CPU/Mem/Swp) refresh cadence. Decoupled from API polling
# so local stats update regardless of the 429-backoff state.
# Default 1 Hz (htop-style). Override with --system-hz.
DEFAULT_SYSTEM_HZ = 1

IN_USE_WINDOW_SEC = 120
HTTP_TIMEOUT = 10.0
BAR_WIDTH_SYSTEM = 20
BAR_WIDTH_ACCOUNT = 14

# ─── data models ───────────────────────────────────────────────────────────────


@dataclass
class Window:
    utilization: float
    resets_at: datetime


@dataclass
class UsageSnapshot:
    five_hour: Optional[Window] = None
    seven_day: Optional[Window] = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AccountState:
    name: str
    creds_path: Path
    project_dir: Path
    access_token: Optional[str] = None
    expires_at: Optional[int] = None
    snapshot: Optional[UsageSnapshot] = None
    last_error: Optional[str] = None
    last_success_at: Optional[datetime] = None
    in_use: bool = False
    # Monotonic timestamp — don't poll this account until time.monotonic()
    # passes this. Set from the server's `Retry-After` header on 429.
    cooldown_until: float = 0.0
    # Consecutive 429 count (reset to 0 on success). Drives progressive
    # backoff when the server keeps saying "rate limited".
    consecutive_429: int = 0

    @property
    def token_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at < time.time() * 1000

    @property
    def has_data(self) -> bool:
        return self.snapshot is not None

    @property
    def cooldown_remaining(self) -> float:
        """Seconds until this account can be polled again, or 0."""
        return max(0.0, self.cooldown_until - time.monotonic())


# ─── discovery / loading ───────────────────────────────────────────────────────


def discover_profiles() -> list[AccountState]:
    if not PROFILES_DIR.exists():
        return []
    accounts: list[AccountState] = []
    for child in sorted(PROFILES_DIR.iterdir()):
        if not child.is_dir():
            continue
        creds = child / ".credentials.json"
        if not creds.exists():
            continue
        accounts.append(
            AccountState(
                name=child.name,
                creds_path=creds,
                project_dir=child / "projects",
            )
        )
    return accounts


def load_token(account: AccountState) -> None:
    """Re-read the credential file. Mutates account in place."""
    try:
        data = json.loads(account.creds_path.read_text())
    except Exception as e:
        account.access_token = None
        account.last_error = f"cred read: {type(e).__name__}"
        return
    oauth = data.get("claudeAiOauth") or {}
    account.access_token = oauth.get("accessToken")
    account.expires_at = oauth.get("expiresAt")


def detect_in_use(account: AccountState) -> bool:
    """True if any .jsonl under the profile's projects dir was modified recently."""
    cutoff = time.time() - IN_USE_WINDOW_SEC
    if not account.project_dir.exists():
        return False
    try:
        for jsonl in account.project_dir.rglob("*.jsonl"):
            try:
                if jsonl.stat().st_mtime > cutoff:
                    return True
            except OSError:
                continue
    except Exception:
        pass
    return False


# ─── API poll ──────────────────────────────────────────────────────────────────


def parse_window(data: dict, key: str) -> Optional[Window]:
    v = data.get(key)
    if not v:
        return None
    util = v.get("utilization")
    resets = v.get("resets_at")
    if util is None or resets is None:
        return None
    try:
        return Window(
            utilization=float(util),
            resets_at=datetime.fromisoformat(str(resets).replace("Z", "+00:00")),
        )
    except (ValueError, AttributeError):
        return None


async def poll_account(client: httpx.AsyncClient, account: AccountState) -> None:
    """Fetch /api/oauth/usage for one account. Isolated: failure stays local.
    Respects per-account cooldown set from Retry-After on previous 429s."""
    load_token(account)
    account.in_use = detect_in_use(account)

    # Skip entirely if in cooldown — don't hammer a rate-limited endpoint.
    if account.cooldown_remaining > 0:
        return  # keep existing last_error + snapshot

    if not account.access_token:
        account.last_error = "no token"
        return
    if account.token_expired:
        account.last_error = "expired"
        return

    try:
        r = await client.get(
            USAGE_ENDPOINT,
            headers={
                "Authorization": f"Bearer {account.access_token}",
                "anthropic-beta": OAUTH_BETA,
            },
            timeout=HTTP_TIMEOUT,
        )
    except httpx.TimeoutException:
        account.last_error = "timeout"
        return
    except Exception as e:
        account.last_error = type(e).__name__[:10]
        return

    if r.status_code == 429:
        # Progressive backoff: Anthropic's Retry-After is inconsistent
        # (sometimes 300, sometimes 0). Build a local exponential-ish
        # schedule keyed on consecutive 429 count. Still honor a larger
        # server-provided value if present, and cap everything at 10 min.
        account.consecutive_429 += 1
        schedule = [60, 180, 300, 600]  # seconds for 1st, 2nd, 3rd, 4th+ miss
        base = schedule[min(account.consecutive_429 - 1, len(schedule) - 1)]
        try:
            server_retry = float(r.headers.get("retry-after", "0"))
        except (TypeError, ValueError):
            server_retry = 0.0
        retry_secs = min(600.0, max(base, server_retry))
        account.cooldown_until = time.monotonic() + retry_secs
        account.last_error = "429"
        return

    if r.status_code != 200:
        account.last_error = f"HTTP {r.status_code}"
        return

    try:
        data = r.json()
    except Exception:
        account.last_error = "bad JSON"
        return

    new_five = parse_window(data, "five_hour")
    new_seven = parse_window(data, "seven_day")

    # Only replace the snapshot if the new response has at least one usable
    # window. A degraded / empty response must NOT wipe previously-known
    # good data — we keep the old snapshot and surface the issue via
    # last_error so the user sees both "stale data" and "something is wrong".
    if new_five is None and new_seven is None:
        account.last_error = "empty resp"
        return

    account.snapshot = UsageSnapshot(five_hour=new_five, seven_day=new_seven)
    account.last_error = None
    account.last_success_at = account.snapshot.fetched_at
    account.cooldown_until = 0.0         # success clears any prior cooldown
    account.consecutive_429 = 0           # reset backoff ladder


async def poll_all(accounts: list[AccountState]) -> None:
    """Poll all accounts that aren't in cooldown, in parallel.
    Accounts in cooldown keep their existing state — no wipe, no retry."""
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(poll_account(client, a) for a in accounts))


# ─── rendering helpers ─────────────────────────────────────────────────────────


def cell_color(position: int, width: int, warn: float, crit: float) -> str:
    """htop-style gradient: each cell is colored by its position in the bar,
    not by the current fill level. A half-full bar shows all-green cells; a
    bar crossing 60% starts yellow at cell 60%; crossing 85% starts red."""
    pos_pct = (position + 0.5) / width * 100.0
    if pos_pct >= crit:
        return "red"
    if pos_pct >= warn:
        return "yellow"
    return "green"


def make_bar(pct: Optional[float], width: int, warn: float, crit: float) -> Text:
    """htop-style `[|||||||      37.0%]` meter. Each filled cell's color is
    determined by its *position* in the bar, not the overall %, so the bar
    smoothly grades green -> yellow -> red as it fills past thresholds.
    Unfilled cells and brackets inherit the terminal's default colors.

    The percentage label adapts to bar width: 6-char " 37.4%" for wide bars,
    4-char " 37%" for narrow, omitted for very narrow. Bar width must be >= 3.
    """
    width = max(3, width)
    text = Text()
    text.append("[")  # no style — inherit terminal theme
    if pct is None:
        text.append(" " * max(0, width - 4) + " n/a"[:width], style="bright_black")
        text.append("]")
        return text
    pct = max(0.0, min(100.0, pct))
    fill = int(round(pct / 100 * width))
    if width >= 8:
        pct_str = f"{pct:5.1f}%"       # " 37.4%" — 6 chars
    elif width >= 5:
        pct_str = f"{int(round(pct)):3d}%"  # " 37%" / "100%" — 4 chars
    else:
        pct_str = ""                   # no room
    pct_start = width - len(pct_str)
    for i in range(width):
        if pct_str and pct_start <= i < width:
            text.append(pct_str[i - pct_start], style="bold")
        elif i < fill:
            text.append("|", style=cell_color(i, width, warn, crit))
        else:
            text.append(" ")  # inherit
    text.append("]")  # inherit
    return text


def format_countdown(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "  now  "
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m:2d}m{s:02d}s"
    if secs < 86400:
        h, rem = divmod(secs, 3600)
        m = rem // 60
        return f"{h:2d}h{m:02d}m"
    d, rem = divmod(secs, 86400)
    h = rem // 3600
    return f"{d:2d}d{h:02d}h"


def countdown_style(delta: timedelta) -> str:
    secs = delta.total_seconds()
    if secs < 300:
        return "red"
    if secs < 1800:
        return "yellow"
    return ""  # inherit terminal default


def name_color(account: AccountState) -> str:
    if account.last_error or account.token_expired or not account.has_data:
        return "bright_black"
    snap = account.snapshot
    assert snap is not None
    if snap.seven_day and snap.seven_day.utilization >= 100:
        return "bold bright_red"
    if snap.five_hour and snap.five_hour.utilization >= 100:
        return "yellow"
    if account.in_use:
        return "bright_green"
    return ""  # idle + below thresholds — inherit terminal default


# ─── top-level renderers ───────────────────────────────────────────────────────


def render_system(warn: float, crit: float, term_width: int) -> Text:
    # Non-blocking sample: returns the CPU delta since the last call.
    # The app primes psutil with a real sample at startup and calls this
    # function at a high cadence (default 60 Hz) from the UI loop, so the
    # delta window is always recent. A blocking `interval=0.05` would
    # stall each render by 50 ms, which caps the effective UI rate at
    # ~20 Hz and wastes 50 ms of CPU per tick even when nothing changed.
    cpu = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    load1, load5, load15 = os.getloadavg()
    up_secs = time.time() - psutil.boot_time()
    days = int(up_secs // 86400)
    hours = int((up_secs % 86400) // 3600)
    minutes = int((up_secs % 3600) // 60)
    uptime = f"{days}d {hours:02d}h {minutes:02d}m" if days else f"{hours}h {minutes:02d}m"

    def gb(n: float) -> str:
        return f"{n / (1024**3):5.1f}G"

    # Row structure (Swp is the longest):
    #   "Swp " + "[" + bar + "]" + "   " + "XX.XG" + " / " + "XX.XG" + "   "
    #   + "Up " + "Xd XXh XXm"
    #   = 4 + 1 + bw + 1 + 3 + 5 + 3 + 5 + 3 + 3 + 10 = bw + 38
    # Use 40 for a 2-char safety margin (handles Xd +1 digit etc).
    bar_w = max(3, term_width - 40)

    out = Text()
    # CPU
    out.append("CPU ", style="cyan bold")
    out.append_text(make_bar(cpu, bar_w, warn, crit))
    out.append(f"   Load {load1:.2f} {load5:.2f} {load15:.2f}\n")
    # Mem
    out.append("Mem ", style="cyan bold")
    out.append_text(make_bar(vm.percent, bar_w, warn, crit))
    out.append(f"   {gb(vm.used)} / {gb(vm.total)}\n")
    # Swp
    out.append("Swp ", style="cyan bold")
    swap_pct = swap.percent if swap.total > 0 else 0.0
    out.append_text(make_bar(swap_pct, bar_w, warn, crit))
    out.append(f"   {gb(swap.used)} / {gb(swap.total)}   Up {uptime}")
    return out


def render_accounts(
    accounts: list[AccountState],
    warn: float,
    crit: float,
    sort_mode: str,
    term_width: int,
) -> Text:
    now = datetime.now(timezone.utc)

    def pct(w: Optional[Window]) -> float:
        return w.utilization if w else -1.0

    def key_5h(a: AccountState) -> float:
        return pct(a.snapshot.five_hour) if a.snapshot else -1.0

    def key_7d(a: AccountState) -> float:
        return pct(a.snapshot.seven_day) if a.snapshot else -1.0

    if sort_mode == "5h":
        accounts = sorted(accounts, key=lambda a: -key_5h(a))
    elif sort_mode == "7d":
        accounts = sorted(accounts, key=lambda a: -key_7d(a))
    elif sort_mode == "name":
        accounts = sorted(accounts, key=lambda a: a.name.lower())

    name_w = max((len(a.name) for a in accounts), default=8)
    name_w = max(name_w, 8)

    # Row structure (char counts):
    #   " #  "   = 4       (index + pad)
    #   name_w             (name column, left-pad)
    #   "  "    = 2        (pad)
    #   "[" + bw + "]"     (5-hour bar)
    #   " "     = 1        (pad)
    #   RRRRRRRR = 8       (reset column — 8 chars to fit " HTTP 429" etc)
    #   "  "    = 2        (pad)
    #   "[" + bw + "]"     (7-day bar)
    #   " "     = 1        (pad)
    #   RRRRRRRR = 8       (reset column)
    # Total = name_w + 30 + 2*bw
    # Solve for bw: bw = (term_width - name_w - 31) / 2
    # (-31 leaves a 1-char safety margin)
    # No lower-bound floor on avail: narrow terminals get tiny bars rather
    # than overflow. No upper cap: wide terminals get huge bars.
    fixed = name_w + 30
    avail = term_width - fixed - 1
    bw = max(5, avail // 2)

    RESET_W = 8  # width of the reset/error column after each bar

    out = Text()
    # htop-style separator: a full-width colored bar with black text that
    # visually separates the system meters (above) from the account list
    # (below). We render it as a single styled span and pad to term_width
    # with background-colored spaces so the stripe extends across the
    # whole terminal.
    HEADER_STYLE = "black on cyan bold"
    header_text = (
        f" #  {'Account':<{name_w}}  "
        f"{'5-hour':<{bw + 2}} {'reset':>{RESET_W}}"
        f"  {'7-day':<{bw + 2}} {'reset':>{RESET_W}}"
    )
    # Pad with styled spaces so the stripe fills the full row
    pad_count = max(0, term_width - len(header_text))
    out.append(header_text + (" " * pad_count), style=HEADER_STYLE)
    out.append("\n")

    for i, a in enumerate(accounts, start=1):
        out.append(f"{i:2d}  ")
        out.append(f"{a.name:<{name_w}}  ", style=name_color(a))

        # Cooldown state: if rate-limited, render "429 Ns/Nm/Nh" in the 5h
        # reset column instead of the reset time, but KEEP showing the
        # last-known good bars so the user sees stale data while waiting.
        cd_secs = int(a.cooldown_remaining)
        if cd_secs > 0:
            if cd_secs < 60:
                cd_display = f"429 {cd_secs}s"
            elif cd_secs < 3600:
                cd_display = f"429 {cd_secs // 60}m"
            else:
                cd_display = f"429 {cd_secs // 3600}h"

        snap = a.snapshot
        # 5-hour bar
        if snap and snap.five_hour:
            out.append_text(make_bar(snap.five_hour.utilization, bw, warn, crit))
            out.append(" ")
            if cd_secs > 0:
                out.append(f"{cd_display:>{RESET_W}}", style="bright_black")
            else:
                delta = snap.five_hour.resets_at - now
                out.append(f"{format_countdown(delta)[:RESET_W]:>{RESET_W}}",
                           style=countdown_style(delta))
        else:
            out.append("[" + " " * bw + "]", style="bright_black")
            err_text = (cd_display if cd_secs > 0 else (a.last_error or "-"))[:RESET_W]
            out.append(f" {err_text:>{RESET_W}}", style="bright_black")
        out.append("  ")
        # 7-day bar
        if snap and snap.seven_day:
            out.append_text(make_bar(snap.seven_day.utilization, bw, warn, crit))
            out.append(" ")
            delta = snap.seven_day.resets_at - now
            out.append(f"{format_countdown(delta)[:RESET_W]:>{RESET_W}}",
                       style=countdown_style(delta))
        else:
            out.append("[" + " " * bw + "]", style="bright_black")
            out.append(f" {'-':>{RESET_W}}", style="bright_black")
        out.append("\n")

    return out


# ─── --dump mode ───────────────────────────────────────────────────────────────


def dump_mode(warn: float, crit: float, sort_mode: str) -> int:
    accounts = discover_profiles()
    if not accounts:
        print(f"No profiles with credentials under {PROFILES_DIR}", file=sys.stderr)
        return 2
    # Prime psutil (first call is 0.0)
    psutil.cpu_percent(interval=None)
    asyncio.run(poll_all(accounts))
    time.sleep(0.2)
    console = Console()
    width = console.width
    console.print(render_system(warn, crit, width))
    console.print()
    console.print(render_accounts(accounts, warn, crit, sort_mode, width))
    return 0


# ─── Textual TUI ───────────────────────────────────────────────────────────────


def run_tui(
    warn: float,
    crit: float,
    sort_mode: str,
    interval_focus: int,
    interval_blur: int,
    system_hz: int,
) -> int:
    from textual.app import App, ComposeResult
    from textual.widgets import Footer, Static

    SORT_MODES = ["5h", "7d", "name"]
    MANUAL_REFRESH_COOLDOWN = 15.0  # seconds — prevents rate-limit from refresh spam

    accounts = discover_profiles()
    if not accounts:
        print(f"No profiles with credentials under {PROFILES_DIR}", file=sys.stderr)
        return 2

    class CctopApp(App):
        # The built-in `textual-ansi` theme defers ALL colors to the
        # terminal's palette, including the background — so we inherit
        # whatever bg the user's terminal is set to (exactly like htop).
        CSS = """
        Screen { layout: vertical; }
        #system { height: 3; padding: 0 1; }
        #accounts { height: 1fr; padding: 0 1; }
        """
        ansi_color = True
        BINDINGS = [
            ("f5,r", "refresh", "Refresh"),
            ("f6,s", "sort", "Sort"),
            ("f10,q", "quit", "Quit"),
        ]
        # Textual auto-adds a command palette (ctrl+p) that shows as "palette"
        # in the footer. We don't need it for a single-screen monitor tool.
        ENABLE_COMMAND_PALETTE = False

        def __init__(self) -> None:
            super().__init__()
            self.accounts = accounts
            self.warn = warn
            self.crit = crit
            self.sort_mode = sort_mode
            self.interval_focus = interval_focus
            self.interval_blur = interval_blur
            self.system_hz = system_hz
            self._last_manual_refresh = 0.0

        def compose(self) -> ComposeResult:
            yield Static(id="system")
            yield Static(id="accounts")
            yield Footer()

        def on_mount(self) -> None:
            # Switch to the ANSI theme so the terminal's native background
            # + palette show through.
            self.theme = "textual-ansi"
            self.title = "cctop"
            self.sub_title = f"{len(self.accounts)} accounts · sort={self.sort_mode}"
            # Prime psutil with a real sample interval so the first render
            # has a baseline (bare `interval=None` returns 0.0 on first call).
            psutil.cpu_percent(interval=0.1)
            # Initial render deferred — widget sizes aren't populated until
            # after the first layout pass. call_after_refresh waits for that.
            self.call_after_refresh(self.update_display)
            self.run_worker(self._refresh_loop(), exclusive=True, group="api")
            self.run_worker(self._ui_refresh_loop(), exclusive=True, group="ui")

        async def _refresh_loop(self) -> None:
            # API polling loop: hits Anthropic's /usage endpoint, updates
            # account snapshots, then refreshes the accounts display.
            # Interval governed by focus/blur and 429 backoff — slow by design.
            while True:
                try:
                    await poll_all(self.accounts)
                except Exception:
                    pass  # never kill the loop
                self.call_after_refresh(self.update_accounts_only)
                any_in_use = any(a.in_use for a in self.accounts)
                interval = self.interval_focus if any_in_use else self.interval_blur
                await asyncio.sleep(interval)

        async def _ui_refresh_loop(self) -> None:
            # System meter (CPU/Mem/Swp) fast-refresh loop. Decoupled from
            # the API poller so local stats animate in real time regardless
            # of 429 backoff or account poll cadence. Default 60 Hz.
            period = 1.0 / max(1, self.system_hz)
            while True:
                self.call_after_refresh(self.update_system_only)
                await asyncio.sleep(period)

        def update_display(self) -> None:
            # Full redraw: system bar + accounts table. Called after API polls
            # and on resize. The fast _ui_refresh_loop calls update_system_only
            # for high-Hz refresh without re-rendering the accounts table.
            self.update_system_only()
            self.update_accounts_only()

        def update_system_only(self) -> None:
            try:
                term_w = self.size.width or 80
                usable = max(40, term_w - 2)
                self.query_one("#system", Static).update(
                    render_system(self.warn, self.crit, usable)
                )
            except Exception as e:
                self.sub_title = f"system render error: {type(e).__name__}"

        def update_accounts_only(self) -> None:
            try:
                term_w = self.size.width or 80
                usable = max(40, term_w - 2)
                self.query_one("#accounts", Static).update(
                    render_accounts(
                        self.accounts, self.warn, self.crit, self.sort_mode, usable
                    )
                )
                self.sub_title = f"{len(self.accounts)} accounts · sort={self.sort_mode}"
            except Exception as e:
                self.sub_title = f"accounts render error: {type(e).__name__}"

        def on_resize(self, event) -> None:
            # Re-render when the terminal is resized so bars reflow.
            self.call_after_refresh(self.update_display)

        def action_refresh(self) -> None:
            now = time.monotonic()
            if now - self._last_manual_refresh < MANUAL_REFRESH_COOLDOWN:
                remaining = MANUAL_REFRESH_COOLDOWN - (now - self._last_manual_refresh)
                self.sub_title = f"cooldown: wait {remaining:.1f}s"
                return
            self._last_manual_refresh = now
            # exclusive=True so only one manual refresh runs at a time.
            self.run_worker(self._manual_refresh(), exclusive=True, group="manual_refresh")

        async def _manual_refresh(self) -> None:
            try:
                await poll_all(self.accounts)
            except Exception:
                pass
            self.update_display()

        def action_sort(self) -> None:
            idx = SORT_MODES.index(self.sort_mode) if self.sort_mode in SORT_MODES else 0
            self.sort_mode = SORT_MODES[(idx + 1) % len(SORT_MODES)]
            self.update_display()

    CctopApp().run()
    return 0


# ─── entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(
        prog="cctop",
        description="htop-style monitor for system load + Anthropic quotas across Claude Code profiles.",
    )
    p.add_argument("--warn", type=float, default=DEFAULT_WARN, help="yellow threshold %% (default: 60)")
    p.add_argument("--crit", type=float, default=DEFAULT_CRIT, help="red threshold %% (default: 85)")
    p.add_argument("--sort", default="5h", choices=["5h", "7d", "name"], help="sort mode (default: 5h)")
    p.add_argument("--interval-focus", type=int, default=DEFAULT_INTERVAL_FOCUS,
                   help="poll seconds when any account in use (default: 30)")
    p.add_argument("--interval-blur", type=int, default=DEFAULT_INTERVAL_BLUR,
                   help="poll seconds when idle (default: 120)")
    p.add_argument("--system-hz", type=int, default=DEFAULT_SYSTEM_HZ,
                   help=f"CPU/Mem/Swp meter refresh rate in Hz (default: {DEFAULT_SYSTEM_HZ})")
    p.add_argument("--dump", action="store_true", help="print one snapshot and exit (no TUI)")
    args = p.parse_args()

    if args.dump:
        sys.exit(dump_mode(args.warn, args.crit, args.sort))
    sys.exit(run_tui(args.warn, args.crit, args.sort, args.interval_focus, args.interval_blur, args.system_hz))


if __name__ == "__main__":
    main()
