"""manual_tests/hook_and_inject_smoketest.py -- hardware-in-the-loop smoke
test for input_hooks.py + input_inject.py.

Not an automated test -- requires a human at the keyboard/mouse to confirm a
low-level hook actually fires on a real key press and that injected input
actually does something in a real target application.

Usage: python manual_tests/hook_and_inject_smoketest.py

Two phases:
  1. CAPTURE   -- press real keys/buttons/scroll and watch the console.
                  Confirms hooks fire and physical input reports injected=False.
  2. INJECTION -- with explicit go-ahead at each step, synthesizes
                  keyboard/mouse input for visual confirmation.

No mouse-move phase: this project never synthesizes relative mouse movement
(hard rule -- no recoil compensation, no aim assist).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow running this script directly from manual_tests/ without installing
# the project as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import input_hooks as ih  # noqa: E402
import input_inject as ii  # noqa: E402


def _fmt_key(e: ih.KeyEvent) -> str:
    src = "SELF" if e.from_self else ("INJECTED" if e.injected else "physical")
    state = "UP  " if e.up else "DOWN"
    return f"[KEY  {state}] vk=0x{e.vk_code:02X} ({e.name!r:>10}) scan=0x{e.scan_code:02X} src={src}"


def _fmt_button(e: ih.MouseButtonEvent) -> str:
    src = "SELF" if e.from_self else ("INJECTED" if e.injected else "physical")
    state = "UP  " if e.up else "DOWN"
    return f"[MOUSE {state}] {e.button.name:<6} at ({e.x}, {e.y}) src={src}"


def _fmt_scroll(e: ih.MouseScrollEvent) -> str:
    src = "SELF" if e.from_self else ("INJECTED" if e.injected else "physical")
    axis = "H" if e.horizontal else "V"
    return f"[SCROLL {axis}] delta={e.delta} at ({e.x}, {e.y}) src={src}"


_move_count = 0


def _on_move(e: ih.MouseMoveEvent) -> None:
    global _move_count
    _move_count += 1
    # Mouse move fires constantly; only print occasionally so the console
    # stays readable, but still prove it's alive.
    if _move_count % 15 == 0:
        src = "SELF" if e.from_self else ("INJECTED" if e.injected else "physical")
        print(f"[MOVE ] ({e.x}, {e.y}) src={src}  [{_move_count} move events seen so far]")


def phase_capture(seconds: float = 15.0) -> None:
    print("=" * 70)
    print("PHASE 1: CAPTURE")
    print("=" * 70)
    print(f"Hooks are live for {seconds:.0f} seconds.")
    print("Press some keys, click mouse buttons, move the mouse, and scroll.")
    print("Every physical event below should show src=physical.")
    print("(ESC will not stop this early -- it's just another key to test.)")
    print()

    hm = ih.HookManager()
    hm.on_key_down(lambda e: print(_fmt_key(e)))
    hm.on_key_up(lambda e: print(_fmt_key(e)))
    hm.on_mouse_button(lambda e: print(_fmt_button(e)))
    hm.on_scroll(lambda e: print(_fmt_scroll(e)))
    hm.on_mouse_move(_on_move)

    hm.start()
    try:
        time.sleep(seconds)
    finally:
        hm.stop()

    print()
    print("Phase 1 done. CHECK: did every physical key/click/scroll you made")
    print("show up above with src=physical? If nothing printed at all, the")
    print("hook did not fire -- that's a real bug, not a display issue.")
    print()


def phase_injection() -> None:
    print("=" * 70)
    print("PHASE 2: INJECTION")
    print("=" * 70)

    input("Click into a text field (Notepad, browser address bar, this "
          "console, etc.) then press Enter here to start a 3s countdown...")
    for n in (3, 2, 1):
        print(n, "...")
        time.sleep(1)

    _type_text("Shattered Gaming Overlay 123")
    print("CHECK: did 'Shattered Gaming Overlay 123' appear in the field "
          "you focused?")
    print()

    do_click = input(
        "Run the click test? This will send a LEFT CLICK at the mouse's "
        "CURRENT position -- hover it over an empty/safe area first, then "
        "type 'y' + Enter: "
    ).strip().lower()
    if do_click == "y":
        ii.send_mouse_button(ii.MouseButton.LEFT, up=False)
        time.sleep(0.05)
        ii.send_mouse_button(ii.MouseButton.LEFT, up=True)
        print("CHECK: did a left click register wherever the cursor was?")
    else:
        print("Skipped click test.")
    print()

    do_scroll = input(
        "Run the scroll test? Hover the mouse over something scrollable "
        "(this console, a browser, a file explorer) then type 'y' + Enter: "
    ).strip().lower()
    if do_scroll == "y":
        ii.send_scroll(-120)  # one notch down
        time.sleep(0.2)
        ii.send_scroll(120)  # one notch up
        print("CHECK: did the view under the cursor scroll down then back up?")
    else:
        print("Skipped scroll test.")


_CHAR_TO_VK = {}
for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _CHAR_TO_VK[c] = ord(c)
for d in "0123456789":
    _CHAR_TO_VK[d] = ord(d)
_CHAR_TO_VK[" "] = 0x20  # VK_SPACE


def _type_text(text: str) -> None:
    """Small demo typist: uppercase letters, digits, and spaces only. Not a
    general text-injection utility -- real character->VK translation (shift
    state, keyboard layout, dead keys) is out of scope for this smoke test."""
    for ch in text.upper():
        vk = _CHAR_TO_VK.get(ch)
        if vk is None:
            continue
        ii.send_key(vk, key_up=False)
        time.sleep(0.02)
        ii.send_key(vk, key_up=True)
        time.sleep(0.03)


def main() -> None:
    print("Shattered Gaming Overlay -- input_hooks / input_inject smoke test")
    print("This is a MANUAL, hardware-in-the-loop test. Read each prompt.")
    print()
    try:
        phase_capture()
        phase_injection()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    print()
    print("Smoke test complete.")


if __name__ == "__main__":
    main()
