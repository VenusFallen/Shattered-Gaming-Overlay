# Privacy

Short version: Shattered Gaming Overlay runs on your computer and keeps your settings there. It has no account, no telemetry, and no analytics. The only thing it sends over the internet is a check for a newer version.

## What it stores on your computer

Everything lives in `%LOCALAPPDATA%\Shattered Gaming Overlay`:

| File | What's in it |
|---|---|
| `profiles.json` | Your saved profiles: remaps, macros, window targets, and overlay settings |
| `settings.json` | App preferences such as theme and whether to check for updates |
| `soundboard.json` | Your soundboard clips (name, hotkey, volume, and the path to each audio file) and your chosen output device |
| `logs\` | Installer logs written when the app updates itself |

Your audio files stay where they are; the soundboard only remembers their location. The paths and window targets in these files can include your Windows username and the names of games you play, so look them over before sharing any of them.

The uninstaller leaves this folder alone so a reinstall keeps your setup. Delete the folder to remove everything.

## What it sends over the internet

One thing: the update check. On launch, if **Check for updates on launch** is on (it is by default), the app asks GitHub's public API for the latest release of this repository. That request carries the usual network information GitHub sees on any request, such as your IP address, plus a fixed label saying it's the app's updater. If you choose to install an update, the app downloads the release from GitHub.

You can turn the check off in **Settings**. You can also check manually at any time.

Clicking a link inside the app, such as the one in the About panel, opens it in your normal web browser.

## What it does not do

- It does not collect, send, or store your keystrokes anywhere except in the settings you configure yourself. It watches the keyboard and mouse only so your remaps, macros, and soundboard hotkeys can work, and it does so entirely on your machine.
- It does not read or change a game's memory, and it doesn't install a driver.
- It does not upload your profiles, macros, or audio files.
- It has no advertising and shares nothing with third parties.

## Other software it uses

Live stats come from LibreHardwareMonitor and PresentMon, both bundled with the app and run locally. See their licence files in the install folder.

## Questions

Open an [issue](https://github.com/VenusFallen/Shattered-Gaming-Overlay/issues) if something here is unclear or you think it's out of date.
