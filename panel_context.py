"""panel_context.py -- the small bundle every panel's render(ctx) receives.

Split out from shell.py to avoid a shell <-> panels import cycle (shell
dispatches to panels, panels only need this context, not the shell itself).
"""

from __future__ import annotations

from dataclasses import dataclass

from app_state import AppState
from key_capture import KeyCaptureService
from theme import Theme


@dataclass
class PanelContext:
    state: AppState
    theme: Theme
    capture: KeyCaptureService
