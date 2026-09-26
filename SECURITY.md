# Security policy

## Reporting a vulnerability

Please report security problems privately rather than in a public issue. Use GitHub's private reporting:

1. Open the [Security tab](https://github.com/VenusFallen/Shattered-Gaming-Overlay/security) of this repository.
2. Choose **Report a vulnerability** and describe what you found.

Include the app version, what you did, and what happened. A short proof of concept helps. This is a one-person project, so I'll acknowledge reports as soon as I can and keep you updated while I work on a fix. Once a fix is released, I'm happy to credit you in the release notes if you'd like.

## What counts

The parts of the app most worth a careful look, because they run with elevated rights:

- **The installer and self-updater.** The app checks GitHub Releases for a newer version, downloads the release zip, and runs the installer it contains with administrator rights.
- **The app itself.** It runs elevated because the FPS counter needs it, and it installs low-level keyboard and mouse hooks.
- **Profile import.** Importing a profile file someone else made must not be able to do anything beyond adding that profile.

Bugs that only affect your own settings, or that need an attacker to already have administrator access on your machine, are usually not security issues, but feel free to report them as normal bugs.

## Supported versions

Only the latest release gets fixes. Please update before reporting, from the **Updates** card in **Settings** or the [Releases page](https://github.com/VenusFallen/Shattered-Gaming-Overlay/releases).

## Download safely

Only download the app from this repository's [Releases page](https://github.com/VenusFallen/Shattered-Gaming-Overlay/releases). The installer is not code-signed yet, so Windows may show a SmartScreen warning. See the [FAQ](README.md#faq) for what to expect.
