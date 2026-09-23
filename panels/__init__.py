"""panels -- one module per Companion window nav section. Each exposes a
single `render(ctx)` function taking a `PanelContext`. Exception:
window_select.py isn't a standalone nav tab -- it exposes `render_section(ctx)`,
called directly from settings.py.
"""
