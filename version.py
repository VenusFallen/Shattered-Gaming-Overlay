VERSION = "1.1.4"

# Shared with main.py (sets the OS window title) and titlebar.py (looks the
# window up by this exact title via ctypes/FindWindowW as a fallback path),
# so the two can never drift out of sync.
WINDOW_TITLE = f"Shattered Gaming Overlay  v{VERSION}"
