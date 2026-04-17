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
from rich.markdown import Markdown
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


@dataclass
class Finding:
    """One CHAIN-READY finding extracted from findings.md.

    Captures the full body of the finding verbatim (including any
    ### sub-sections, code blocks, prose) so the panel can render
    whatever the author wrote regardless of template.
    """

    title: str
    metadata: dict[str, str] = field(default_factory=dict)
    body: str = ""


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
    """Pull the readable vuln title out of a H2.

    Handles many real-world formats:
      '## [F-29] [CHAIN-READY] [CRITICAL] JWT signature bypass'
      '## F-56 ESCALATION: DYNAMICALLY VERIFIED — OAuth Code Leak [CHAIN-READY]'
      '## F-74: Stored XSS via SVG File Upload [CHAIN-READY]'
    """
    text = h2.lstrip("#").strip()
    # Strip leading bracketed tokens (e.g. [F-29] [CHAIN-READY] [CRITICAL])
    while text.startswith("["):
        close = text.find("]")
        if close < 0:
            break
        text = text[close + 1 :].strip()
    # Strip a bare leading F-code prefix like 'F-56 ESCALATION:' or 'F-74:'
    m = re.match(r"^F-\d+\s*[:—-]\s*", text)
    if m:
        text = text[m.end() :]
    elif re.match(r"^F-\d+\s+", text):
        text = re.sub(r"^F-\d+\s+", "", text)
    # Strip trailing classification tags like '[CHAIN-READY]', '[HIGH]'
    text = re.sub(r"\s*\[[^\]]+\]\s*$", "", text)
    return text.strip()


def _is_template_placeholder(title: str, meta: dict) -> bool:
    """Detect the boilerplate template entry used in findings.md examples."""
    if title == "Finding Title":
        return True
    if meta.get("CVSS", "").lstrip().startswith("X.X"):
        return True
    # Common placeholder patterns in the template block
    for val in meta.values():
        if "[SEVERITY]" in val or "[date]" in val:
            return True
    return False


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
        # Accept any H2 (not just bracket-prefixed). We filter on the
        # CHAIN-READY substring — real findings include it regardless of
        # whether the header is '## [F-XX] [CHAIN-READY] ...' or
        # '## F-56 ESCALATION ... [CHAIN-READY]'.
        if not header.startswith("## "):
            continue
        if "CHAIN-READY" not in header:
            continue

        finding = Finding(title=_parse_title(header))
        # Two phases:
        # 1. Metadata phase: consume '**Key:** value' lines at the top.
        #    Ends when we hit any non-metadata line (blank, section
        #    header, prose, code block, etc.).
        # 2. Body phase: everything from there to end-of-block goes
        #    verbatim into finding.body. No section filtering.
        in_metadata = True
        body_lines: list[str] = []
        for raw in lines[1:]:
            line = raw.rstrip()
            if line.startswith("## "):
                break
            if in_metadata:
                m = re.match(r"^\*\*([^*]+):\*\*\s*(.*)$", line)
                if m and m.group(1).strip() in _META_ORDER:
                    finding.metadata[m.group(1).strip()] = m.group(2).strip()
                    continue
                # Blank lines during the metadata phase are just
                # visual separators — skip them without ending the phase.
                if not line.strip():
                    continue
                # Non-blank, non-**known-key:** content ends metadata
                # phase and this line becomes the first body line.
                in_metadata = False
            body_lines.append(line)
        # Trim leading/trailing blank lines from the body
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        finding.body = "\n".join(body_lines)
        if _is_template_placeholder(finding.title, finding.metadata):
            continue
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


def _render_body(body: str) -> "Markdown | Text":
    """Render the raw finding body as Rich Markdown, or plain Text if empty.

    Using Rich's Markdown gets us for free:
      - '### Section' -> styled headers
      - '**bold**' / '*italic*' / '`code`'
      - Code fences with syntax highlighting
      - Bullet lists
    """
    if not body.strip():
        return Text("(empty body)", style="dim italic")
    return Markdown(body, code_theme="ansi_dark")


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
        pos = f"{self._index + 1}/{len(self._findings)}"
        # Budget the header to exactly one row. Layout:
        #   <engagement> · <title> <pad> <pos> ◀ ▸
        # Reserved fixed chars: " · " (3) + " " (1) + " ◀ ▸" (4) = 8
        reserved = len(self.engagement) + 3 + 1 + len(pos) + 4
        avail_for_title = max(10, width - reserved)
        title = cur.title
        if len(title) > avail_for_title:
            title = title[: avail_for_title - 1].rstrip() + "…"
        pad = max(1, width - len(self.engagement) - 3 - len(title) - 1 - len(pos) - 4)
        header.append(self.engagement, style="bold")
        header.append(" · ", style="dim")
        header.append(title, style="italic")
        header.append(" " * pad)
        header.append(pos, style="bold")
        header.append(" ◀ ▸", style="dim")

        metadata = _render_metadata(cur.metadata)
        body_rendered = _render_body(cur.body)

        # Scroll is applied by splitting the body text into lines and
        # dropping the first N. For Markdown, we fall back to the raw
        # string for scrolling (losing some styling on the cut portion).
        if self._scroll > 0 and cur.body:
            remaining_lines = cur.body.split("\n")[self._scroll :]
            body_rendered = Markdown("\n".join(remaining_lines), code_theme="ansi_dark") if remaining_lines else Text("")

        return Group(header, Rule(style="dim"), metadata, Text(""), body_rendered)
