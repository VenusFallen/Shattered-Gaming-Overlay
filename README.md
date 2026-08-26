# Shattered Gaming Overlay

A gaming accessibility overlay for Windows. Built entirely on user-mode input APIs — no kernel driver, no virtual controller — so it stays a pure accessibility tool, compatible with a much wider range of games than its predecessor.

---

## Why no driver

Its predecessor project (R9Tools) used the [Interception](https://github.com/oblitum/Interception) kernel driver for input. A kernel-mode driver is a heavier, more invasive approach than an accessibility tool actually needs, and it narrows the games it can be used with.

Shattered Gaming Overlay instead uses pure user-mode Win32 APIs — the same technique AutoHotkey uses:

- **Capture** — `SetWindowsHookEx` (`WH_KEYBOARD_LL` / `WH_MOUSE_LL`) or the Raw Input API.
- **Injection** — `SendInput`.
- **Never**: a kernel-mode driver, ViGEm/virtual-controller emulation, or any read/write into a game process's memory.

This keeps the project scoped to what it actually is — an accessibility tool, not a low-level system hook — and it's what lets Shattered Gaming Overlay be used with a much wider range of games than R9Tools could.

See `.claude/agents/engine-agent.md` for the full rationale and hard rules.

---

## Planned modules

- **Overlay** — toggleable, passive HUD elements over the game: hardware/FPS stats, an accessibility crosshair/visual aid, and module status indicators. Rendered as a separate, non-injecting, click-through DirectComposition window — never a hook into the game's own swap chain, and never interactive.
- **Remapper** — remap any key/button to any other key/button.
- **Macros** — record/playback sequences (Once / Hold / Toggle), with humanized timing on playback.
- **Profiles** — save/load/delete named per-game configurations.
- **Active window selection** — target a specific running game, or leave global. When a process is selected, Remapper and Macros go inert the instant that process loses focus (so they never reach into the rest of your PC while you're alt-tabbed away) — but the HUD overlay stays visible regardless of focus, so you can tune it live from the Companion window.
- **Updates** — in-app self-update against GitHub Releases.

---

## Architecture

This is companion software, not an in-game menu — unlike R9Tools, no configuration UI ever draws on top of the game. Two windows, one UI toolkit:

- **Companion window** — the main application window. All configuration lives here (Remapper, Macros, Profiles, Window Select, Overlay toggles, Updates, theming) — a normal desktop window with normal OS chrome, the way you'd interact with Discord's client or MSI Afterburner's main window.
- **HUD overlay** — a separate, click-through, non-interactive layer that only renders whatever's been toggled on in the Companion window's Overlay panel. It has no menus and never receives input.

Both windows are built with the same toolkit family — **Dear ImGui** — no PySide6/Qt/Tkinter. The Companion window currently runs on `imgui_bundle`'s OpenGL3 backend (the published wheel doesn't ship DX11 support; a from-source rebuild with a C++ toolchain is the known path to real DX11 later, if OpenGL3 ever proves insufficient). The HUD overlay will need its own hand-rolled DX11 + DirectComposition swap chain regardless, since that's outside what any Hello ImGui backend provides. See `.claude/agents/ui-agent.md` for the full detail.

## Agent team

This repo is developed with a small team of scoped Claude Code agents (`.claude/agents/`):

| Agent | Owns |
| --- | --- |
| `engine-agent` | Input capture/injection, remapper, macro engine, profiles, active-window selection |
| `ui-agent` | Companion window (all settings panels), HUD overlay rendering, theming |
| `build-agent` | Packaging, versioning, self-updater |
| `qa-agent` | Verification gate — tests, cross-file contracts, flags what needs live/manual confirmation |

## Status

Early scaffolding — architecture and agent team defined; module implementation not yet started.
