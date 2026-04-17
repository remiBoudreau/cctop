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
# Default 2 Hz (every 500 ms). Override with --system-hz.
DEFAULT_SYSTEM_HZ = 2

# Minimum terminal dimensions to show rich UI features.
# Below PANEL_MIN_HEIGHT rows: hide bottom panel (too short to be useful).
# Below PANEL_MIN_WIDTH cols: hide bottom panel AND the Engagement
# column (accounts table needs ~80 chars for readable bars alone).
DEFAULT_PANEL_MIN_HEIGHT = 25
DEFAULT_PANEL_MIN_WIDTH = 100

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
    """Poll accounts SEQUENTIALLY with a small delay between each.

    Anthropic's /api/oauth/usage endpoint rate-limits per-IP. Firing
    all N accounts via asyncio.gather in parallel produced burst 429s
    that applied to every account, even though each account was under
    its own per-account limit. Serializing with a ~1s gap between
    requests keeps us under Anthropic's burst threshold without
    meaningfully slowing the UI (N accounts = ~N seconds total, and
    accounts already in cooldown short-circuit instantly).
    """
    async with httpx.AsyncClient() as client:
        for i, account in enumerate(accounts):
            if i > 0:
                # 1s gap between polls keeps us under Anthropic's burst
                # rate limit. Skipped if account is in cooldown (fast path).
                if account.cooldown_remaining <= 0:
                    await asyncio.sleep(1.0)
            await poll_account(client, account)


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


def build_account_rows(
    accounts: list[AccountState],
    engagement_map: dict[str, list[str]],
    sort_mode: str,
) -> list[tuple[AccountState, str | None, int, int]]:
    """Return a flat list of virtual rows.

    Each row = (account, engagement_name_or_None, row_offset_in_group, group_size).
    A group is all rows that belong to one account — 1 row when the account has
    no active engagement, otherwise one row per active engagement.

    Row ordering respects `sort_mode`: accounts are sorted first, then each
    account's engagements are listed in the order returned by the detector.
    """
    def pct(w: Optional[Window]) -> float:
        return w.utilization if w else -1.0

    def key_5h(a: AccountState) -> float:
        return pct(a.snapshot.five_hour) if a.snapshot else -1.0

    def key_7d(a: AccountState) -> float:
        return pct(a.snapshot.seven_day) if a.snapshot else -1.0

    if sort_mode == "5h":
        ordered = sorted(accounts, key=lambda a: -key_5h(a))
    elif sort_mode == "7d":
        ordered = sorted(accounts, key=lambda a: -key_7d(a))
    elif sort_mode == "name":
        ordered = sorted(accounts, key=lambda a: a.name.lower())
    else:
        ordered = list(accounts)

    rows: list[tuple[AccountState, str | None, int, int]] = []
    for a in ordered:
        engs = engagement_map.get(a.name, [])
        if not engs:
            rows.append((a, None, 0, 1))
        else:
            for i, eng in enumerate(engs):
                rows.append((a, eng, i, len(engs)))
    return rows


def render_accounts(
    accounts: list[AccountState],
    warn: float,
    crit: float,
    sort_mode: str,
    term_width: int,
    engagement_map: dict[str, list[str]] | None = None,
    selected_row: int = -1,
    show_engagement_column: bool = True,
) -> Text:
    now = datetime.now(timezone.utc)

    def pct(w: Optional[Window]) -> float:
        return w.utilization if w else -1.0

    def key_5h(a: AccountState) -> float:
        return pct(a.snapshot.five_hour) if a.snapshot else -1.0

    def key_7d(a: AccountState) -> float:
        return pct(a.snapshot.seven_day) if a.snapshot else -1.0

    # Build the flat row list. If the engagement column is hidden
    # (narrow terminal), collapse each account to a single row so
    # multi-instance accounts don't produce blank rows without context.
    eng_for_layout: dict[str, list[str]] = (engagement_map or {}) if show_engagement_column else {}
    rows = build_account_rows(accounts, eng_for_layout, sort_mode)
    if not rows:
        return Text("(no accounts)\n", style="dim")

    name_w = max((len(a.name) for a in accounts), default=8)
    name_w = max(name_w, 8)

    # Engagement column: fit the longest active engagement name + padding,
    # clamped to a sensible range. Set to 0 when hidden.
    if show_engagement_column:
        max_eng_len = max(
            (len(e) for engs in (engagement_map or {}).values() for e in engs),
            default=0,
        )
        ENG_W = max(12, min(24, max_eng_len + 2))
        eng_section_width = ENG_W + 1  # leading space before engagement
    else:
        ENG_W = 0
        eng_section_width = 0
    RESET_W = 8  # reset/error column width

    # Bar width: divide remaining space between the two bars, but cap so
    # very wide terminals don't produce 100+-char bars that look stretched.
    MAX_BW = 40
    fixed = name_w + 30 + eng_section_width
    avail = term_width - fixed - 1
    bw = max(5, min(MAX_BW, avail // 2))
    # Extra unused width (when terminal is wider than our max layout) goes
    # to trailing blanks on each row so the header stripe still spans the
    # whole terminal.
    used_cols = fixed + 2 * bw
    trailing_pad = max(0, term_width - used_cols - 1)

    out = Text()
    HEADER_STYLE = "black on cyan bold"
    header_text = (
        f" # {'Account':<{name_w}}  "
        f"{'5-hour':<{bw + 2}} {'reset':>{RESET_W}}"
        f"  {'7-day':<{bw + 2}} {'reset':>{RESET_W}}"
    )
    if show_engagement_column:
        header_text += f" {'Engagement':<{ENG_W}}"
    pad_count = max(0, term_width - len(header_text))
    out.append(header_text + (" " * pad_count), style=HEADER_STYLE)
    out.append("\n")

    # Account-number counter only increments when we enter a new group.
    account_number = 0

    for flat_idx, (a, eng, row_offset, group_size) in enumerate(rows):
        is_group_start = row_offset == 0
        if is_group_start:
            account_number += 1
        is_selected = flat_idx == selected_row

        # `#` column: only on the first row of each group
        if is_group_start:
            out.append(f"{account_number:2d} ")
        else:
            out.append("   ")

        # Account name + bars columns always on the group's first row
        if is_group_start:
            out.append(f"{a.name:<{name_w}}  ", style=name_color(a))

            cd_secs = int(a.cooldown_remaining)
            cd_display = ""
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
        else:
            # Continuation rows (engagements 2..N of a multi-instance
            # account) leave the Account/bars/resets region blank so the
            # Engagement column alignment stays intact.
            blank_w = name_w + 2 + (bw + 2) + 1 + RESET_W + 2 + (bw + 2) + 1 + RESET_W
            out.append(" " * blank_w)

        # Engagement column (if visible). Selected cell = green text
        # + trailing ◀ marker. Background stays terminal default.
        if show_engagement_column:
            eng_text = eng if eng else "—"
            eng_display = (eng_text[: ENG_W - 1] + "…") if len(eng_text) > ENG_W else eng_text
            if is_selected:
                eng_style = "bold green"
            elif eng:
                eng_style = "white"
            else:
                eng_style = "bright_black"
            out.append(" ")
            out.append(f"{eng_display:<{ENG_W}}", style=eng_style)
            if is_selected:
                out.append(" ◀", style="bold green")
            else:
                out.append("  ")
        if trailing_pad > 0:
            out.append(" " * trailing_pad)
        out.append("\n")

    return out


# ─── --dump mode ───────────────────────────────────────────────────────────────


def dump_mode(warn: float, crit: float, sort_mode: str) -> int:
    from engagements import active_engagements_by_profile

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
    eng_map = active_engagements_by_profile()
    console.print(render_accounts(accounts, warn, crit, sort_mode, width, eng_map, -1))
    return 0


# ─── Textual TUI ───────────────────────────────────────────────────────────────


def run_tui(
    warn: float,
    crit: float,
    sort_mode: str,
    interval_focus: int,
    interval_blur: int,
    system_hz: int,
    panel_name: str,
    panel_path: Optional[str],
) -> int:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Footer, Static

    import panels as panels_module
    from engagements import active_engagements_by_profile, engagement_dir

    accounts = discover_profiles()
    if not accounts:
        print(f"No profiles with credentials under {PROFILES_DIR}", file=sys.stderr)
        return 2

    # Load the panel class (from registry or --panel-path file)
    if panel_path:
        PanelCls = panels_module.load_panel_from_path(Path(panel_path))
    else:
        PanelCls = panels_module.load_panel(panel_name)
    panel = PanelCls()

    # Build Textual bindings from the panel's keybindings
    panel_bindings = []
    for keys, action, label in panel.keybindings():
        panel_bindings.append(Binding(keys, f"panel_{action}", label))

    # Whether we COULD show the panel (non-null panel class loaded).
    # Actual visibility also depends on terminal height — the panel is
    # hidden at runtime when the window is too short. Manual override
    # via F3 toggle.
    panel_enabled = not isinstance(panel, panels_module.base.NoPanel)

    class CctopApp(App):
        CSS = """
        Screen { layout: vertical; }
        #system   { height: 5; padding: 1 2; }
        #accounts { height: auto; max-height: 50%; padding: 1 2; }
        #panel    { height: 1fr; padding: 1 2; }
        #panel.hidden    { display: none; }
        #accounts.fullheight { height: 1fr; max-height: 100%; }
        """
        ansi_color = True
        BINDINGS = [
            ("f10,q", "quit", "Quit"),
            ("up,k", "cursor_up", "Up"),
            ("down,j", "cursor_down", "Down"),
            ("f3", "toggle_panel", "Panel"),
        ] + panel_bindings
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
            self.panel = panel
            self.panel_enabled = panel_enabled
            # Manual override state. None = auto (based on height).
            # True = force show. False = force hide.
            self._panel_override: bool | None = None
            self.engagement_map: dict[str, list[str]] = {}
            self.selected_row = 0 if panel_enabled else -1
            self._virtual_rows: list = []

        @property
        def show_panel(self) -> bool:
            """Whether the bottom panel is currently visible."""
            if not self.panel_enabled:
                return False
            if self._panel_override is not None:
                return self._panel_override
            h = self.size.height or 30
            w = self.size.width or 120
            return h >= DEFAULT_PANEL_MIN_HEIGHT and w >= DEFAULT_PANEL_MIN_WIDTH

        @property
        def show_engagement_column(self) -> bool:
            """Whether the accounts table includes the Engagement column.
            Dropped on narrow terminals where it steals too much bar space."""
            w = self.size.width or 120
            return w >= DEFAULT_PANEL_MIN_WIDTH

        def compose(self) -> ComposeResult:
            yield Static(id="system")
            yield Static(id="accounts")
            if self.panel_enabled:
                yield Static(id="panel")
            yield Footer()

        def on_mount(self) -> None:
            self.theme = "textual-ansi"
            self.title = "cctop"
            self.sub_title = f"{len(self.accounts)} accounts"
            psutil.cpu_percent(interval=0.1)
            # Initial engagement scan + selection sync before first render
            self._refresh_engagement_map()
            self._sync_panel_engagement()
            self.call_after_refresh(self._apply_panel_visibility)
            self.call_after_refresh(self.update_display)
            self.run_worker(self._refresh_loop(), exclusive=True, group="api")
            self.run_worker(self._ui_refresh_loop(), exclusive=True, group="ui")
            self.run_worker(self._engagement_loop(), exclusive=True, group="eng")

        async def _refresh_loop(self) -> None:
            while True:
                try:
                    await poll_all(self.accounts)
                except Exception:
                    pass
                self.call_after_refresh(self.update_accounts_only)
                any_in_use = any(a.in_use for a in self.accounts)
                interval = self.interval_focus if any_in_use else self.interval_blur
                await asyncio.sleep(interval)

        async def _ui_refresh_loop(self) -> None:
            period = 1.0 / max(1, self.system_hz)
            while True:
                self.call_after_refresh(self.update_system_only)
                await asyncio.sleep(period)

        async def _engagement_loop(self) -> None:
            # Re-scan /proc for active engagements every 2 seconds. Cheap
            # (reads /proc/*/environ); no API involvement.
            while True:
                try:
                    changed = self._refresh_engagement_map()
                except Exception:
                    changed = False
                # Panel may also have per-tick state changes (findings.md mtime)
                try:
                    panel_changed = self.panel.on_tick()
                except Exception:
                    panel_changed = False
                if changed:
                    self._sync_panel_engagement()
                if changed or panel_changed:
                    self.call_after_refresh(self.update_accounts_only)
                    if self.show_panel:
                        self.call_after_refresh(self.update_panel_only)
                await asyncio.sleep(2.0)

        def _refresh_engagement_map(self) -> bool:
            """Rescan /proc for running claude processes. Return True if changed."""
            new_map = active_engagements_by_profile()
            if new_map == self.engagement_map:
                return False
            was_empty = not self.engagement_map
            self.engagement_map = new_map
            # Rebuild virtual rows and clamp selection
            self._virtual_rows = build_account_rows(
                self.accounts, self.engagement_map, self.sort_mode
            )
            if self._virtual_rows:
                # On first population, seek to the first row that actually
                # has an engagement so the panel shows useful content.
                if was_empty and self.panel_enabled:
                    for i, (_, eng, _, _) in enumerate(self._virtual_rows):
                        if eng:
                            self.selected_row = i
                            break
                    else:
                        self.selected_row = 0
                else:
                    self.selected_row = max(0, min(self.selected_row, len(self._virtual_rows) - 1))
            else:
                self.selected_row = -1
            return True

        def _sync_panel_engagement(self) -> None:
            """Inform the panel which engagement is currently selected.

            Runs independently of panel visibility — we still update the
            panel's engagement state so that when the panel becomes
            visible (resize, F3 toggle, or first layout) it already has
            data to render. Early-exiting on !show_panel caused an
            'on-load shows no engagement' bug because self.size.height
            is 0 before the first layout pass.
            """
            if not self.panel_enabled:
                return
            if not self._virtual_rows or self.selected_row < 0:
                self.panel.on_engagement_change(None, None)
                return
            _, eng, _, _ = self._virtual_rows[self.selected_row]
            if eng:
                self.panel.on_engagement_change(eng, engagement_dir(eng))
            else:
                self.panel.on_engagement_change(None, None)

        def update_display(self) -> None:
            self.update_system_only()
            self.update_accounts_only()
            if self.show_panel:
                self.update_panel_only()

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
                # Keep virtual rows in sync for cursor clamping even when
                # the map hasn't changed (first render before eng_loop ticks)
                self._virtual_rows = build_account_rows(
                    self.accounts, self.engagement_map, self.sort_mode
                )
                if self._virtual_rows:
                    self.selected_row = max(
                        0, min(self.selected_row, len(self._virtual_rows) - 1)
                    )
                else:
                    self.selected_row = -1
                # Hide the cursor marker when the panel isn't visible —
                # selecting an engagement has no purpose if there's no
                # panel to update.
                effective_selected = self.selected_row if self.show_panel else -1
                self.query_one("#accounts", Static).update(
                    render_accounts(
                        self.accounts, self.warn, self.crit, self.sort_mode,
                        usable, self.engagement_map, effective_selected,
                        show_engagement_column=self.show_engagement_column,
                    )
                )
                self.sub_title = f"{len(self.accounts)} accounts · panel={self.panel.name}"
            except Exception as e:
                self.sub_title = f"accounts render error: {type(e).__name__}"

        def update_panel_only(self) -> None:
            if not self.show_panel:
                return
            try:
                term_w = self.size.width or 80
                term_h = self.size.height or 24
                usable_w = max(40, term_w - 2)
                # Account pane ~half of screen, panel ~half.
                panel_h = max(10, term_h // 2 - 2)
                self.query_one("#panel", Static).update(
                    self.panel.render(usable_w, panel_h)
                )
            except Exception as e:
                self.sub_title = f"panel render error: {type(e).__name__}"

        def on_resize(self, event) -> None:
            self._apply_panel_visibility()
            self.call_after_refresh(self.update_display)

        def _apply_panel_visibility(self) -> None:
            """Toggle panel/accounts CSS classes to match show_panel state.
            Also refreshes the footer so disabled bindings disappear."""
            if not self.panel_enabled:
                return
            try:
                panel_widget = self.query_one("#panel", Static)
                accounts_widget = self.query_one("#accounts", Static)
            except Exception:
                return
            if self.show_panel:
                panel_widget.remove_class("hidden")
                accounts_widget.remove_class("fullheight")
            else:
                panel_widget.add_class("hidden")
                accounts_widget.add_class("fullheight")
            # Nudge Textual to re-check action availability so the Footer
            # reflects which bindings are live.
            try:
                self.refresh_bindings()
            except Exception:
                pass

        def action_toggle_panel(self) -> None:
            """F3: cycle through auto / force-show / force-hide."""
            if not self.panel_enabled:
                return
            # auto → force-hide → force-show → auto
            if self._panel_override is None:
                self._panel_override = False
            elif self._panel_override is False:
                self._panel_override = True
            else:
                self._panel_override = None
            self._apply_panel_visibility()
            self.update_display()

        # ── cursor navigation ──────────────────────────────────────────

        def check_action(self, action: str, parameters) -> bool | None:
            """Gate cursor + panel actions on panel visibility.

            Returning False removes the binding from the footer and
            prevents the action from firing. When the panel is hidden
            (narrow or short terminal), selecting an engagement has no
            visible effect, so there's no point exposing the keys.

            The F3 toggle always works so the user can force-show the
            panel even when it would auto-hide.
            """
            if action in ("cursor_up", "cursor_down") or action.startswith("panel_"):
                return self.show_panel
            return True

        def action_cursor_up(self) -> None:
            if not self.show_panel or not self._virtual_rows:
                return
            self.selected_row = (self.selected_row - 1) % len(self._virtual_rows)
            self._sync_panel_engagement()
            self.update_accounts_only()
            self.update_panel_only()

        def action_cursor_down(self) -> None:
            if not self.show_panel or not self._virtual_rows:
                return
            self.selected_row = (self.selected_row + 1) % len(self._virtual_rows)
            self._sync_panel_engagement()
            self.update_accounts_only()
            self.update_panel_only()

        # ── panel action dispatch ──────────────────────────────────────

        def __getattr__(self, name: str):
            # Route action_panel_<x> to the loaded panel's action(x) method.
            if name.startswith("action_panel_"):
                action = name[len("action_panel_"):]
                def handler() -> None:
                    if self.panel.action(action):
                        self.update_panel_only()
                return handler
            raise AttributeError(name)

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
    p.add_argument("--panel", default="chain_ready",
                   help="bottom panel: chain_ready | none (default: chain_ready)")
    p.add_argument("--panel-path", default=None,
                   help="load a user-written panel from a Python file (overrides --panel)")
    p.add_argument("--dump", action="store_true", help="print one snapshot and exit (no TUI)")
    args = p.parse_args()

    if args.dump:
        sys.exit(dump_mode(args.warn, args.crit, args.sort))
    sys.exit(run_tui(
        args.warn, args.crit, args.sort,
        args.interval_focus, args.interval_blur, args.system_hz,
        args.panel, args.panel_path,
    ))


if __name__ == "__main__":
    main()
