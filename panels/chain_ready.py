"""Chain-ready findings panel.

Parses findings.md in the engagement directory, extracts CHAIN-READY
entries, and renders them one at a time. Left/right cycles between
them, PgUp/PgDn scrolls inside a finding if it overflows the panel.

Only the fields the user wants are shown (title, metadata block,
Summary, The Attack). Everything else is dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from rich.console import Group
from rich.rule import Rule
from rich.text import Text

from .base import Panel

# Which metadata keys we display, in order.
_META_ORDER = [
    "Target",
    "In-Scope",
    "CVSS",
    "Discovered",
    "Reported",
    "Eligible",
    "RQG Passed",
]

# Section headers we render, in display order. First two are the new
# canonical spec (Summary / The Attack). The rest are legacy names from
# older findings.md files — shown as fallback if the canonical sections
# are missing. Everything not in this set is discarded.
_SECTION_ORDER = [
    "Summary",
    "The Attack",
    "What Was Found",
    "Validation",
    "Chain Notes",
    "Impact",
]
_SECTION_SET = set(_SECTION_ORDER)


@dataclass
class Finding:
    """One CHAIN-READY finding extracted from findings.md."""

    title: str
    metadata: dict[str, str] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)


def _cvss_color(score: str) -> str:
    """Map CVSS score to a Rich color."""
    try:
        value = float(score.split("—")[0].split()[0])
    except (ValueError, IndexError):
        return "white"
    if value >= 9.0:
        return "red"
    if value >= 7.0:
        return "bright_red"
    if value >= 4.0:
        return "yellow"
    return "green"


def _parse_title(h2: str) -> str:
    """Pull the readable vuln title out of a H2 like:
    '## [F-29] [CHAIN-READY] [CRITICAL] JWT signature bypass → API access'
    becomes 'JWT signature bypass → API access'.
    """
    text = h2.lstrip("#").strip()
    # Strip one or more leading bracketed tokens
    while text.startswith("["):
        close = text.find("]")
        if close < 0:
            break
        text = text[close + 1 :].strip()
    return text


def _parse_findings(findings_md: Path) -> List[Finding]:
    """Extract every CHAIN-READY finding from findings.md."""
    try:
        text = findings_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    findings: List[Finding] = []
    # Split on H2 boundaries (## at start of line). Keep the headers.
    chunks = re.split(r"(?m)^(?=##\s)", text)
    for chunk in chunks:
        lines = chunk.splitlines()
        if not lines:
            continue
        header = lines[0]
        if not header.lstrip("#").strip().startswith("["):
            continue
        if "CHAIN-READY" not in header:
            continue

        finding = Finding(title=_parse_title(header))
        current_section: str | None = None
        section_lines: list[str] = []

        def flush():
            nonlocal section_lines
            if current_section and current_section in _SECTION_SET:
                finding.sections[current_section] = "\n".join(section_lines).strip()
            section_lines = []

        for raw in lines[1:]:
            line = raw.rstrip()
            if line.startswith("## "):
                break
            # Section header
            m = re.match(r"^###\s+(.+?)\s*$", line)
            if m:
                flush()
                current_section = m.group(1).strip()
                continue
            # Metadata line: **Key:** value (only at top of finding, before sections)
            m = re.match(r"^\*\*([^*]+):\*\*\s*(.*)$", line)
            if m and current_section is None:
                key = m.group(1).strip()
                val = m.group(2).strip()
                if key in _META_ORDER:
                    finding.metadata[key] = val
                continue
            # Regular content line for the current section
            if current_section and current_section in _SECTION_SET:
                section_lines.append(line)
        flush()
        findings.append(finding)
    return findings


def _render_metadata(meta: dict[str, str]) -> Text:
    """Render the metadata block with per-value colors."""
    label_width = max((len(k) for k in _META_ORDER if k in meta), default=10) + 2
    out = Text()
    for key in _META_ORDER:
        if key not in meta:
            continue
        val = meta[key]
        out.append(f"{key + ':':<{label_width}}", style="bold")
        if key == "CVSS":
            parts = val.split("—", 1)
            score = parts[0].strip()
            vector = parts[1].strip() if len(parts) > 1 else ""
            out.append(score, style=f"bold {_cvss_color(score)}")
            if vector:
                out.append(f" — {vector}", style="dim")
        elif key == "Target":
            out.append(val, style="cyan")
        elif key in ("In-Scope", "Eligible"):
            lowered = val.lower()
            style = "green" if lowered.startswith("yes") else "red" if lowered.startswith("no") else "yellow"
            out.append(val, style=style)
        elif key == "Reported":
            lowered = val.lower()
            # "no" is noteworthy (work to do), "yes" is neutral
            style = "yellow" if lowered.startswith("no") else "green"
            out.append(val, style=style)
        elif key == "RQG Passed":
            out.append(val, style="green" if "✓" in val else "yellow")
        elif key == "Discovered":
            out.append(val, style="dim")
        else:
            out.append(val)
        out.append("\n")
    return out


def _render_section(heading: str, body: str) -> Text:
    """Render a ### section with a cyan ▸ header."""
    out = Text()
    out.append(f"▸ {heading}", style="bold cyan")
    out.append("\n")
    if body:
        out.append(body)
    return out


class Panel(Panel):  # type: ignore[misc]
    """Chain-ready findings panel."""

    name = "chain_ready"

    def __init__(self) -> None:
        super().__init__()
        self._findings: List[Finding] = []
        self._index: int = 0
        self._scroll: int = 0
        self._findings_path: Path | None = None
        self._findings_mtime: float = 0.0

    def keybindings(self) -> List[Tuple[str, str, str]]:
        return [
            ("left,h", "prev", "Prev"),
            ("right,l", "next", "Next"),
            ("pageup", "scroll_up", "Scroll ↑"),
            ("pagedown", "scroll_down", "Scroll ↓"),
        ]

    def on_engagement_change(self, name: str | None, directory: Path | None) -> None:
        super().on_engagement_change(name, directory)
        self._scroll = 0
        self._index = 0
        if directory:
            self._findings_path = directory / "findings.md"
        else:
            self._findings_path = None
        self._findings_mtime = 0.0
        self._reload_if_stale()

    def on_tick(self) -> bool:
        """Re-parse findings.md if its mtime changed."""
        return self._reload_if_stale()

    def _reload_if_stale(self) -> bool:
        if not self._findings_path or not self._findings_path.is_file():
            if self._findings:
                self._findings = []
                return True
            return False
        try:
            mtime = self._findings_path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._findings_mtime:
            return False
        self._findings_mtime = mtime
        self._findings = _parse_findings(self._findings_path)
        if self._index >= len(self._findings):
            self._index = 0
        return True

    def action(self, name: str) -> bool:
        if not self._findings:
            return False
        if name == "prev":
            self._index = (self._index - 1) % len(self._findings)
            self._scroll = 0
            return True
        if name == "next":
            self._index = (self._index + 1) % len(self._findings)
            self._scroll = 0
            return True
        if name == "scroll_up":
            self._scroll = max(0, self._scroll - 5)
            return True
        if name == "scroll_down":
            self._scroll += 5
            return True
        return False

    def render(self, width: int, height: int) -> Group:
        # Header row: engagement · title  <pos>/<total>  ◀ ▸
        header = Text()
        if self.engagement is None:
            header.append("— no engagement selected", style="dim")
            return Group(header)
        if not self._findings:
            header.append(f"{self.engagement}", style="bold")
            header.append(" · ", style="dim")
            header.append("no chain-ready findings yet", style="dim italic")
            return Group(header)

        cur = self._findings[self._index]
        header.append(self.engagement, style="bold")
        header.append(" · ", style="dim")
        header.append(cur.title, style="italic")
        pos = f"{self._index + 1}/{len(self._findings)}"
        pad = max(1, width - len(self.engagement) - len(" · ") - len(cur.title) - len(pos) - 4)
        header.append(" " * pad)
        header.append(pos, style="bold")
        header.append(" ◀ ▸", style="dim")

        body = Text()
        body.append_text(_render_metadata(cur.metadata))

        # Prefer Summary + The Attack (user's spec). If neither is
        # present, fall back to whichever legacy sections exist.
        preferred = [s for s in ("Summary", "The Attack") if s in cur.sections]
        if preferred:
            shown = preferred
        else:
            shown = [s for s in _SECTION_ORDER if s in cur.sections]

        for section in shown:
            body.append("\n")
            body.append_text(_render_section(section, cur.sections[section]))
            body.append("\n")

        if not shown:
            body.append("\n(no Summary / The Attack / What Was Found sections in this finding)\n", style="dim italic")

        # Apply scroll offset by dropping the first N lines
        if self._scroll > 0:
            lines = body.split("\n")
            body = Text("\n").join(lines[self._scroll :])

        return Group(header, Rule(style="dim"), body)
