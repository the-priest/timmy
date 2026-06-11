<p align="center">
  <img src="assets/icon.png" alt="Timmy" width="128" height="128">
</p>

<h1 align="center">Timmy</h1>

<p align="center"><b>An AI 2D game forge for Linux &amp; macOS.</b></p>

Describe a game in plain English — or tap a few options — and Timmy agrees on it
with you, forges a real, playable **pygame** game, lets you launch and play it on the
spot, fixes its own bugs from the run log, and — when it's right — packages it for
GitHub or builds it into a single-file executable. Everything runs locally; your API
keys never leave your machine.

Games come out **single-file and self-contained**: graphics drawn in code, content
embedded inline, no loose asset folders to ship. They run the same on a **Kali / KDE
Plasma / X11** desktop (ThinkPad X395) and on a **OnePlus 6 running NetHunter Pro /
Phosh / Wayland** — keyboard + mouse on the desktop, touch and on-screen controls on
the phone. Pick the **deep story** option and it builds the real thing: NPCs, branching
dialogue, quests, an inventory, multiple areas and a save file — not a stub.

It opens in its own desktop app window (via your browser engine) and talks to a small
local-only Python server. No cloud backend, no telemetry.

---

## What's new in 1.2.0 — the "actually good at games" release

The generation pipeline was rebuilt around one principle: **no game reaches you
untested or half-finished.**

- **Headless runtime playtest.** Every build is now actually *run* in a sandbox
  (SDL dummy drivers — no window opens), piloted with synthetic key / mouse / touch
  input for several seconds, then sent `QUIT` and required to exit cleanly. Crashes
  during play, instant exits, and loops that ignore `QUIT` are caught and fed back
  to the model as real tracebacks **before you ever see the game**. The old check
  only imported the file, so anything that broke after the window opened got through.
- **No more truncated games.** Requests now carry an explicit per-model completion
  budget (`max_tokens` was never sent before, so providers silently cut long games
  mid-file), and a reply that still gets cut is automatically continued from the
  exact cut point and stitched back together.
- **The Timmy kit.** Games are built on a hand-written, runtime-tested engine
  substrate (delta-time loop, scene machine, pooled particles, screen shake,
  easing, procedural sfx, an on-screen touch overlay) pasted verbatim into every
  game — the model writes game logic, not engine plumbing it keeps fumbling.
- **Design pass + genre craft notes.** Fresh builds first get a compact,
  authoritative design spec (title, palette, entities, tuned numbers, win/lose,
  juice list) plus genre-specific requirements (coyote time for platformers,
  pooled bullets for shooters, ...), and the code pass implements *that*.
- **Quality gate.** A fresh build that passes every check gets one playtest-critique
  round; genuine playability gaps (unreachable win, missing touch controls, dead
  restart) trigger a single improvement pass that's kept only if it also passes.
- **Analyzer fix.** The static analyzer falsely flagged `self.score = 0` +
  `self.score += 1` — the most common pattern in any game — as a serious bug,
  failing correct code and burning fix rounds on non-problems. Fixed; the intended
  typo catch (`self.valeu += 1` with no init) still works.

---

## Install

```
curl -fsSL https://raw.githubusercontent.com/the-priest/timmy/main/install.sh | bash
```

This installs into `~/.local/share/timmy`, drops a `timmy` launcher on your
`PATH`, and on Linux adds an app-menu entry with the icon. Running the command again
updates to the latest version.

Requires `python3` (3.8+). No root needed — everything lives under `$HOME`. On first
launch it installs **pygame** into its own managed virtualenv, so your system Python
stays clean.

---

## Set an API key

Timmy drives one of four model providers. Set a key for whichever you use, either as
an environment variable or in the in-app **Settings** panel:

| Provider          | Environment variable        | Notes                              |
|-------------------|-----------------------------|------------------------------------|
| SiliconFlow       | `SILICONFLOW_API_KEY=sk-...`| **Default** — DeepSeek V4 Flash    |
| Groq              | `GROQ_API_KEY=gsk_...`      | Fast fallback, free tier           |
| Google AI Studio  | `GOOGLE_API_KEY=AIza...`    |                                    |
| Novita AI         | `NOVITA_API_KEY=sk_...`     |                                    |

Keys are stored in a per-user config file on your machine and are never sent to the
browser. Each provider keeps its own key, and Timmy pulls the live model list from
the provider using your key, so the model dropdown shows exactly what your account can
call. If you already use the same keys elsewhere, they carry over automatically.

---

## Launch

```
timmy
```

Or pick **Timmy** from your app menu (Linux). It prints a local URL, starts the
server, and opens an app window.

---

## How it works

The flow is four steps, shown along the top of the workspace:

1. **Agree** — You describe the game, or tap a starter. Timmy either asks a few sharp
   multiple-choice questions (genre, scope & story depth, target — desktop / phone /
   both) or lays out a plan, so it builds exactly what you meant. You can skip the
   questions and just say "build it."
2. **Play** — It forges a **playable build** and you launch it right there with
   **▶ launch**. The game opens its own window on your desktop. Nothing runs on its
   own — you press the button. Use **■ stop** to close it.
3. **Iterate** — When something breaks, hit **⮐ send log to AI & fix** and it diagnoses
   the run log and patches the code, or run the **✦ auto-polish loop** to launch → fix →
   improve over several passes automatically.
4. **Release** — When it's right, **◆ get ready for GitHub** polishes a clean release
   version and assembles a full repo, or **⬛ build** packs it into a single-file binary.

---

## Features

**Building & iterating**
- Conversational build, or tap-to-pick intake (genre, scope, story depth, target device).
- **pygame** by default — SDL2 under the hood, so the same game runs on X11 and Wayland.
- **Playable** vs **release** build stages, plus a numeric version that auto-bumps
  (`1.0 → 1.1 → 1.2 …`) every time the game changes — shown on a badge and saved with it.
- **Auto-named**: the game takes its title from the window caption the model sets (click the
  title in the top bar to rename it yourself), and that name drives the file and the repo.
- **Auto-test**: after each build it silently imports/launches the game and fixes startup
  failures for up to 3 rounds before handing it back.
- **⮐ Send log to AI & fix** — diagnose and repair straight from the run output.
- **✦ Auto-polish loop** — automated launch → fix → improve cycles (safety-capped).
- Double-click a console error to send it straight to the model for a fix.

**Made for both your machines**
- Desktop: keyboard + mouse, resizable window, tuned for KDE Plasma on X11.
- OnePlus 6 / Phosh / Wayland: touch and drag input with an on-screen control overlay,
  display scaling that fits the phone, and a frame loop tuned to stay smooth there.
- No hardcoded video driver — it runs under whatever the session provides.

**Deep games, not stubs**
- The story option builds a full dialogue system, branching choices, NPCs, lore, quests,
  an inventory and save/load across multiple areas — real writing, embedded in the file.
- Save data lives under `~/.local/share/<game>` using XDG paths.

**Understanding the code**
- **🔍 Review** — combined AI + static (AST) analysis of the whole game: finds clashes,
  bugs and risks, rates them by severity, and offers a one-click "fix these issues."
- **⇄ Diff** — see exactly what changed between the previous and current version.
- **✎ Edit** — edit the code yourself in-pane; the model stays in sync.

**Running safely**
- Launches the real game window and captures stdout/stderr for debugging.
- **Danger guard** — if a draft matches destructive patterns, it's blocked until you
  read it and confirm. Everything runs locally as you, on `127.0.0.1` only.

**Dependencies & packaging**
- **⬇ Deps** — detects third-party imports (pygame and anything else) and pip-installs
  them into a managed virtualenv, so your system Python stays clean.
- **⬛ Build** — packages the game into a single-file executable for your current OS with
  PyInstaller (windowed). Copy that one file to another machine of the same
  OS/architecture and run it — no Python needed there.
- **◆ Get ready for GitHub** — polishes a release version and writes a complete repo:
  README with a one-line HTTPS install command, `install.sh`, `LICENSE` (MIT / GPLv3 /
  Apache-2.0 / none), `.gitignore`, and your push commands. Remotes use HTTPS, never SSH.

**Keeping your work**
- **In progress** — work auto-saves as you go; resume any game exactly where you left off.
- **★ Library** — save a finished game at its exact state (code + the whole build
  conversation + version + launch args); reopen it and keep iterating.
- **⤓ Save** / **⧉ copy** — drop the script to disk or the clipboard anytime. Finished
  games land in `~/timmy-games`.

**Bringing in your own files**
- Attach (or drag & drop) a `.py` to load it as the working game, or attach logs,
  notes, JSON, CSV, etc. as context for what to build or fix.

**Polish**
- Sound cues out of the box: short chimes play when Timmy opens, when a game lands in the
  editor, and when a build finishes. Override any of them by dropping your own `startup`,
  `done`, or `build` audio file into `~/.local/share/timmy/sounds/` (see that folder's README).

---

## What to make first

Not sure where to start? Tap a starter, or paste one of these into the build dialogue:

- **"A fast top-down arena shooter: I'm a little ship, waves of enemies close in, WASD to
  move and mouse to aim and shoot, score, drops/power-ups, and it ramps up the longer I
  survive. Keyboard + mouse on desktop and on-screen touch controls too."**

- **"A side-scrolling platformer: run, jump and double-jump, dodge spikes and patrolling
  enemies, collect coins, reach the flag. Three levels that get harder, with a short line
  of story between them. Keyboard on desktop, touch on the phone."**

- **"A cozy story RPG, top-down: a small village I can walk around, NPCs I can talk to
  with branching dialogue, two or three quests, an inventory, a couple of areas, and a
  save file. Give it real writing and a little lore — go deep, not a stub."**

- **"A falling-block puzzle like Tetris: pieces fall, rotate and drop, clear full lines,
  speed rises as I score, next-piece preview and a high-score table. Keyboard on desktop,
  tap/swipe on the phone."**

Start small, launch it, then tell Timmy what to change. Iterating is the whole point.

---

## Updating

Re-run the installer — it always pulls the latest:

```
curl -fsSL https://raw.githubusercontent.com/the-priest/timmy/main/install.sh | bash
```

## Uninstall

```
rm -rf ~/.local/share/timmy ~/.local/bin/timmy ~/.local/share/applications/timmy.desktop
```

(Icons under `~/.local/share/icons/hicolor/*/apps/timmy.*` can be removed too.)

---

## Privacy & safety

- Runs local-only on `127.0.0.1` — nothing is exposed to your network.
- API keys live in a local config file and are never sent to the browser.
- Generated games execute on your machine as you; the danger guard flags destructive
  patterns before anything runs, but you are always the one who presses launch.

---

*Built on Kali, at home anywhere with Python.*
