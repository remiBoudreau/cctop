"""Detect which engagement(s) each Claude Code profile is running.

Scans running `claude` processes, reads /proc/<pid>/environ for
CLAUDE_CONFIG_DIR (identifies the profile) and CLAUDE_ENGAGEMENT
(identifies the engagement). Falls back to the per-PID marker files at
~/.claude/instance-engagements/<pid> for sessions that weren't launched
with CLAUDE_ENGAGEMENT set.

Returns a dict[profile_name] -> list[engagement_name], ordered by the
PID so the display order is stable across refreshes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

MARKER_DIR = Path.home() / ".claude" / "instance-engagements"


def _read_environ(pid: int) -> Dict[str, str]:
    """Parse /proc/<pid>/environ into a dict. Returns empty dict on any error."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return {}
    env = {}
    for entry in raw.split(b"\0"):
        if b"=" not in entry:
            continue
        k, _, v = entry.partition(b"=")
        try:
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        except Exception:
            continue
    return env


def _running_claude_pids() -> List[int]:
    """Scan /proc for processes whose comm is 'claude'."""
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if comm == "claude":
            pids.append(pid)
    return sorted(pids)


def _resolve_engagement(pid: int, env: Dict[str, str]) -> str | None:
    """Resolve engagement name for one claude PID."""
    eng = env.get("CLAUDE_ENGAGEMENT")
    if eng:
        return eng
    marker = MARKER_DIR / str(pid)
    if marker.is_file():
        try:
            name = marker.read_text().strip()
            if name and name != "_manager_":
                return name
        except OSError:
            pass
    return None


def _profile_name_from_config_dir(config_dir: str) -> str | None:
    """Extract profile name from CLAUDE_CONFIG_DIR path."""
    if not config_dir:
        return None
    parts = Path(config_dir).parts
    try:
        idx = parts.index(".claude-profiles")
    except ValueError:
        return None
    if idx + 1 < len(parts):
        return parts[idx + 1]
    return None


def active_engagements_by_profile() -> Dict[str, List[str]]:
    """Map profile_name -> list of engagement names currently being worked on.

    The 'default' profile (no CLAUDE_CONFIG_DIR set) uses the special
    key 'default' so the caller can distinguish it if needed. In cctop
    only profiles under ~/.claude-profiles/ are shown, so 'default' is
    typically ignored.
    """
    result: Dict[str, List[str]] = {}
    for pid in _running_claude_pids():
        env = _read_environ(pid)
        config_dir = env.get("CLAUDE_CONFIG_DIR", "")
        profile = _profile_name_from_config_dir(config_dir)
        if not profile:
            continue
        eng = _resolve_engagement(pid, env)
        if not eng:
            continue
        result.setdefault(profile, [])
        if eng not in result[profile]:
            result[profile].append(eng)
    return result


def engagement_dir(name: str) -> Path:
    """Return the absolute path to an engagement directory."""
    return Path.home() / "engagements" / name
