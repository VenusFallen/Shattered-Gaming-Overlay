# Shattered Gaming Overlay

A gaming accessibility overlay for Windows. Built entirely on user-mode input APIs — no kernel driver, no virtual controller — so it can run alongside kernel-anti-cheat-protected games (Escape from Tarkov, Battlefield 6, Call of Duty).

---

## Why no driver

Its predecessor project (R9Tools) used the [Interception](https://github.com/oblitum/Interception) kernel driver for input. That's a kernel filter driver, visible to any kernel-level anti-cheat scan — fine for a personal aim-assist tool, not acceptable for an accessibility app meant to run alongside BattlEye/EAC-protected titles.

Shattered Gaming Overlay instead uses pure user-mode Win32 APIs — the same technique AutoHotkey uses:

- **Capture** — `SetWindowsHookEx` (`WH_KEYBOARD_LL` / `WH_MOUSE_LL`) or the Raw Input API.
- **Injection** — `SendInput`.
- **Never**: a kernel-mode driver, ViGEm/virtual-controller emulation, or any read/write into a game process's memory.

See `.claude/agents/engine-agent.md` for the full rationale and hard rules.

---

## Planned modules

- **Overlay** — always-on-top HUD: hardware/FPS stats and an accessibility crosshair/visual aid. Rendered as a separate, non-injecting DirectComposition window — never a hook into the game's own swap chain.
- **Remapper** — remap any key/button to any other key/button.
- **Macros** — record/playback sequences (Once / Hold / Toggle), with humanized timing on playback.
- **Profiles** — save/load/delete named per-game configurations.
- **Active window selection** — target modules at a specific running game, or leave global.
- **Updates** — in-app self-update against GitHub Releases.

---

## Architecture

Single UI stack: **DX11 + Dear ImGui + DirectComposition**, for both the HUD overlay and the settings panels — no PySide6/Qt/Tkinter. See `.claude/agents/ui-agent.md` for the compositing details.

## Agent team

This repo is developed with a small team of scoped Claude Code agents (`.claude/agents/`):

| Agent | Owns |
| --- | --- |
| `engine-agent` | Input capture/injection, remapper, macro engine, profiles, active-window selection |
| `ui-agent` | HUD overlay rendering, settings panels, theming |
| `build-agent` | Packaging, versioning, self-updater |
| `qa-agent` | Verification gate — tests, cross-file contracts, flags what needs live/manual confirmation |

## Status

Early scaffolding — architecture and agent team defined; module implementation not yet started.
