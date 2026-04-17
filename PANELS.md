# cctop Panels

cctop has a modular bottom-pane system. Each "panel" is a Python module that
reacts to the engagement currently selected in the accounts pane and renders
a Rich renderable. You can swap the default panel out, turn it off, or write
your own.

## Running

```bash
# Default panel (chain_ready — shows CHAIN-READY findings.md entries)
cctop

# No bottom panel at all (accounts pane fills the window)
cctop --panel none

# Swap in a bundled panel by name
cctop --panel chain_ready

# Load a panel from an arbitrary Python file outside the repo
cctop --panel-path ~/private-panels/bounty_tracker.py
```

`--panel-path` is the escape hatch for private panels. Point it at any
Python file that exposes a `Panel` class and cctop will load it. The file
doesn't need to live inside the cctop package.

## Bundled panels

| Name | Purpose |
|---|---|
| `chain_ready` | Default. Parses `~/engagements/<selected>/findings.md`, extracts every `[CHAIN-READY]` entry, renders the title + metadata block + `Summary` / `The Attack` sections (with fallback to legacy section headers). Left/right cycles findings; PgUp/PgDn scrolls within a finding. |
| `none` | Null panel. Renders nothing and takes no vertical space. Use when you just want the accounts pane. |

## Writing your own panel

A panel is a Python class that inherits from `panels.base.Panel` and
implements a few methods. Minimum viable panel:

```python
# my_panel.py
from rich.text import Text
from panels.base import Panel as BasePanel


class Panel(BasePanel):
    name = "my_panel"

    def keybindings(self):
        return []  # no panel-specific keys

    def render(self, width, height):
        if self.engagement is None:
            return Text("no engagement selected", style="dim")
        return Text(f"hello from {self.engagement}", style="bold green")
```

Run it:

```bash
cctop --panel-path ./my_panel.py
```

## The `Panel` interface

```python
class Panel(ABC):
    name: str                                          # panel identifier
    engagement: str | None                             # set by cctop
    engagement_dir: Path | None                        # set by cctop

    def keybindings(self) -> list[tuple[str, str, str]]:
        """Keys this panel wants registered.
        Each tuple is (keys, action_name, display_label).
        Example: [('left,h', 'prev', 'Prev'), ('right,l', 'next', 'Next')]
        """

    def on_engagement_change(self, name: str | None, directory: Path | None) -> None:
        """Called when the cursor moves to a different engagement.
        Default implementation stores them on self.engagement / self.engagement_dir.
        """

    def on_tick(self) -> bool:
        """Optional per-tick hook (every 2s).
        Return True if state changed and a redraw is needed.
        Default: returns False.
        """

    def render(self, width: int, height: int) -> RenderableType:
        """Return a Rich renderable: Text, Panel, Group, Table, Columns, etc."""

    def action(self, name: str) -> bool:
        """Handle a panel-registered key action.
        Return True if the panel state changed and a redraw is needed.
        Default: returns False.
        """
```

### How cctop routes keys

If your `keybindings()` returns `[("left,h", "prev", "Prev")]`, cctop
registers `left` and `h` globally. When either is pressed, cctop calls
`your_panel.action("prev")`. Return `True` to trigger a redraw.

The `f10` / `q` quit binding is reserved by cctop itself. `↑` / `↓` /
`j` / `k` are reserved for engagement-row navigation. Avoid those.

### Lifecycle

1. cctop imports your panel and calls `__init__()` once at startup.
2. On first render (and whenever the user moves the cursor to a different
   engagement), `on_engagement_change(name, dir)` fires. Cache whatever
   you need, reset scroll state, etc.
3. `render(width, height)` is called on every redraw. Keep it fast — it
   runs on the Textual event loop.
4. `on_tick()` runs every 2 seconds from a background worker. Use it to
   check mtimes, poll external state, etc. Return `True` to request a
   redraw.
5. `action(name)` fires whenever the user presses a key you registered.

### Examples of useful panels you could write

- **Recent submissions** — parse `triage-log.md`, show S-XX entries with
  platform URLs and triage timeline. Left/right cycles submissions.
- **Credential registry** — parse `loot/credentials.md`, show status
  (valid/expired/rotated/revoked) per cred. Useful to spot creds that
  expired mid-engagement.
- **Bounty totals** — aggregate `triage-log.md` across ALL engagements,
  render a leaderboard (awarded/pending/rejected) and YTD sum.
- **Test account inventory** — list every test account across engagements
  with last-used timestamp. Useful for knowing which accounts to refresh.
- **Chain Priority Queue** — render the `## Chain Priority Queue` table
  from `state.md` so you can see your near-chains without leaving cctop.

## Keeping private panels out of the public repo

Write them outside `~/cctop/panels/` — typically in a separate directory
like `~/.local/share/cctop-private/`:

```
~/.local/share/cctop-private/
  submissions.py
  creds.py
```

Then launch with:

```bash
cctop --panel-path ~/.local/share/cctop-private/submissions.py
```

The cctop repo stays clean; your private panels never get committed.
This is the intended distribution model when cctop goes public.

## Parsing findings.md (chain_ready panel reference)

The default `chain_ready` panel parses `findings.md` by:

1. Splitting the file on H2 boundaries (`## ` at start of line).
2. Keeping only chunks whose header contains the literal `CHAIN-READY`.
3. Extracting the readable title by stripping leading `[F-XX]`, `[CHAIN-READY]`,
   `[SEVERITY]` tokens, leading `F-XX:` / `F-XX ` prefixes, and trailing
   `[CHAIN-READY]` tags.
4. Skipping template placeholders (title `Finding Title`, `CVSS: X.X`, or any
   metadata value containing `[SEVERITY]` / `[date]`).
5. Extracting `**Key:** value` metadata lines (Target, In-Scope, CVSS,
   Discovered, Reported, Eligible, RQG Passed).
6. Reading section bodies for `Summary`, `The Attack`, `What Was Found`,
   `Validation`, `Chain Notes`, `Impact`.
7. Watching the file's mtime and re-parsing on change.

If you want to write a panel that parses a different engagement file
(state.md, triage-log.md, notes.md), copy the mtime-watch and block-split
patterns from `panels/chain_ready.py`.
