"""panels -- one module per Companion window nav section.

Each module exposes a single `render(ctx)` function, where `ctx` is a
`shell.PanelContext` bundling the shared `AppState`, active `Theme`, and the
`key_capture.capture_service` singleton. Panels own their own local state
slice (see app_state.py) -- no backend engine exists yet, so nothing here
does real remapping/macro execution/profile persistence/process targeting.

Exception: window_select.py is no longer a standalone nav tab (folded into
Settings as a card) -- it exposes `render_section(ctx)` instead, called
directly from settings.py.
"""
