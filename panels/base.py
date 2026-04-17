"""Abstract base class for cctop panels.

A panel is a pluggable bottom-of-screen view that reacts to the
currently selected engagement in the accounts panel and renders
something useful about it. Examples: chain-ready findings (default),
recent submissions, test account inventory, bounty totals, etc.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from rich.console import RenderableType


class Panel(ABC):
    """Base class every panel must inherit from."""

    name: str = "abstract"

    def __init__(self) -> None:
        self.engagement: str | None = None
        self.engagement_dir: Path | None = None

    @abstractmethod
    def keybindings(self) -> List[Tuple[str, str, str]]:
        """Keybindings this panel wants registered.

        Returns a list of (keys, action_name, display_label) tuples —
        e.g., [('left,h', 'prev', 'Prev'), ('right,l', 'next', 'Next')].
        Action names must match method names on the panel (e.g.,
        action('prev') will be dispatched via panel.action('prev')).
        """

    def on_engagement_change(self, name: str | None, directory: Path | None) -> None:
        """Called when cctop cursor moves to a new engagement."""
        self.engagement = name
        self.engagement_dir = directory

    def on_tick(self) -> bool:
        """Optional per-tick hook. Return True to signal a redraw is needed.

        Called on the same cadence as the accounts-panel redraw.
        Default implementation returns False (no redraw needed).
        """
        return False

    @abstractmethod
    def render(self, width: int, height: int) -> "RenderableType":
        """Return a Rich renderable to display in the panel."""

    def action(self, name: str) -> bool:
        """Handle a panel-registered key action. Return True to request a redraw."""
        return False


class NoPanel(Panel):
    """Null panel — renders nothing and takes no vertical space."""

    name = "none"

    def keybindings(self) -> List[Tuple[str, str, str]]:
        return []

    def render(self, width: int, height: int) -> "RenderableType":
        from rich.text import Text

        return Text("")
