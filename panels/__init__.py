"""panels -- one module per Companion window nav section.

Each module exposes a single `render(ctx)` function, where `ctx` is a
`PanelContext` bundling the shared `AppState`, active `Theme`, and the
`key_capture.capture_service` singleton.

Exception: window_select.py isn't a standalone nav tab -- it exposes
`render_section(ctx)`, called directly from settings.py.
"""
