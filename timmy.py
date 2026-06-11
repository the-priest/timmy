#!/usr/bin/env python3
"""
Timmy  -  AI 2D game forge for Linux
=======================================
A local workshop for building real, playable 2D GAMES by talking to a model — native
pygame games that run on the Linux desktop (tuned for Kali on KDE Plasma / X11, the X395
ThinkPad) and on a OnePlus 6 running NetHunter Pro / Phosh (Wayland, touchscreen) from a
single self-contained Python file. You describe a game — any genre — and Timmy either
asks a few sharp questions or lays out a plan, writes a TESTING build you PLAY right here
on YOUR box, you iterate on how it actually feels, and only when you ask does it package a
RELEASE build. One button can also pack the game into a single-file Linux binary via
PyInstaller. Say the word and it'll forge a deep, story-driven game — full dialogue,
characters, multiple areas — not a stub.

Built for these machines from the ground up:
  - pygame games that run under BOTH X11 (KDE desktop) and Wayland (Phosh on the OnePlus 6)
  - keyboard + mouse on the desktop, touch (FINGERDOWN/MOTION + on-screen controls) on the phone
  - self-contained: graphics drawn procedurally, content embedded — no missing-asset crashes,
    the game plays the instant it launches with nothing but pygame installed
  - delta-time game loops, scene/state machines, save data under ~/.local/share via XDG dirs
  - the engine (pygame, or arcade/pyglet on request) is pip-installed into a managed venv
  - forged games ship an install.sh (curl|bash) and a .desktop entry with an icon

This file is a tiny local HTTP server (standard library only). It:
  - serves the workshop UI to your browser
  - keeps your API key on THIS machine (never sent to the browser)
  - LAUNCHES the forged game locally so "play it" is real
  - is engine-aware: detects pygame/arcade/pyglet, runs with the right interpreter,
    surfaces startup errors, doesn't block waiting for the game window you left open
  - never auto-runs anything: you click play, and a destructive-pattern scan guards it

Run:
    export SILICONFLOW_API_KEY="sk-..."   # primary provider (DeepSeek V4 Flash)
    export GROQ_API_KEY="gsk_..."         # fallback provider
    python3 timmy.py                   # opens http://127.0.0.1:8765 in your browser

License: MIT
"""

import os
import re
import sys
import json
import time
import shlex
import shutil
import signal
import socket
import platform
import tempfile
import threading
import webbrowser
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "1.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# PLATFORM DETECTION  -- every cross-platform branch in this file reads these
# --------------------------------------------------------------------------
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = not IS_WIN and not IS_MAC

def detect_desktop_env():
    """Classify the running Linux session for the UI. Returns a dict:

      {"de": "kde"|"gnome"|"xfce"|"cinnamon"|"other", "form": "desktop",
       "session": "wayland"|"x11"|"unknown", "raw": "<XDG_CURRENT_DESKTOP>"}

    Timmy targets the Linux desktop, so `form` is always "desktop"; `de` and
    `session` are informational. Detection reads the freedesktop env vars every
    session sets (XDG_CURRENT_DESKTOP, XDG_SESSION_TYPE/WAYLAND_DISPLAY) and
    degrades gracefully to "other"/"desktop" when nothing is set.
    """
    raw = os.environ.get("XDG_CURRENT_DESKTOP", "") or ""
    desk = raw.lower()
    sess = (os.environ.get("XDG_SESSION_TYPE", "") or "").lower()
    if not sess:
        sess = "wayland" if os.environ.get("WAYLAND_DISPLAY") else (
            "x11" if os.environ.get("DISPLAY") else "unknown")

    if "kde" in desk or "plasma" in desk:
        de = "kde"
    elif "gnome" in desk:
        de = "gnome"
    elif "xfce" in desk:
        de = "xfce"
    elif "cinnamon" in desk:
        de = "cinnamon"
    else:
        de = "other"

    return {"de": de, "form": "desktop", "session": sess, "raw": raw}

def app_data_dir():
    """Per-OS app data dir (writes that should persist + survive)."""
    if IS_WIN:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Timmy"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "Timmy"
    return Path.home() / ".local" / "share" / "timmy"

def config_dir():
    """Per-OS config dir (small settings file)."""
    if IS_WIN:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Timmy"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "Timmy"
    return Path.home() / ".config" / "timmy"

def tools_dir():
    """Where forged/saved games live, under the user's home (visible, not hidden)."""
    return Path.home() / "Timmy-games" if IS_WIN else Path.home() / "timmy-games"

# ==========================================================================
# CONFIG  -- yours to edit
# ==========================================================================

# --------------------------------------------------------------------------
# PROVIDERS
# --------------------------------------------------------------------------
# Timmy can call several providers. You pick one per session in the UI; if a
# call fails it falls through that provider's own model chain (biggest first).
# Keys are read from env vars (below) or pasted in Settings. Nothing is sent to
# the browser; keys persist to an owner-only config file.
#
# The "models" lists below are only FALLBACKS. Timmy fetches each provider's
# live catalog from its OpenAI-compatible /models endpoint ("models_url") using
# your key, so the dropdown shows exactly what your account can actually call —
# no more guessing at names that 404 with "model unavailable on your plan".
PROVIDERS = {
    "groq": {
        "label": "Groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models_url": "https://api.groq.com/openai/v1/models",
        "env": "GROQ_API_KEY",
        "kind": "openai",
        "models": [
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "gemma2-9b-it",
            "llama-3.1-8b-instant",
        ],
    },
    "siliconflow": {
        "label": "SiliconFlow",
        # SiliconFlow runs TWO separate platforms whose keys are NOT interchangeable:
        #   - International: cloud.siliconflow.COM  -> api.siliconflow.com
        #   - China:         cloud.siliconflow.CN   -> api.siliconflow.cn
        # A key made on one returns 401 on the other. We target .com because that's
        # where cloud.siliconflow.com keys are issued. If your key is from the .cn
        # site instead, change both URLs below back to .cn.
        "url": "https://api.siliconflow.com/v1/chat/completions",
        "models_url": "https://api.siliconflow.com/v1/models?sub_type=chat",
        "env": "SILICONFLOW_API_KEY",
        "kind": "openai",
        # V4 Flash first — your chosen primary: 1M context, fast, far cheaper than Pro.
        # The rest are fallbacks only; a live /models fetch overrides this list.
        "models": [
            "deepseek-ai/DeepSeek-V4-Flash",
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen2.5-72B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "Qwen/Qwen2.5-7B-Instruct",
        ],
    },
    "google": {
        "label": "Google AI Studio",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "env": "GOOGLE_API_KEY",
        "kind": "openai",   # google exposes an OpenAI-compatible endpoint
        "models": [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
        ],
    },
    "novita": {
        "label": "Novita AI",
        "url": "https://api.novita.ai/v3/openai/chat/completions",
        "models_url": "https://api.novita.ai/v3/openai/models",
        "env": "NOVITA_API_KEY",
        "kind": "openai",
        "models": [
            "deepseek/deepseek-v3",
            "qwen/qwen-2.5-72b-instruct",
            "meta-llama/llama-3.1-70b-instruct",
            "openai/gpt-oss-120b",
            "meta-llama/llama-3.1-8b-instruct",
        ],
    },
}

# default provider on first launch: SiliconFlow primary, Groq is the fallback.
DEFAULT_PROVIDER = "siliconflow"
# when no model is explicitly chosen, prefer this one on the default provider.
# DeepSeek V4 Flash is the primary: 1M context, fast, and far cheaper than V4 Pro.
DEFAULT_MODEL_BY_PROVIDER = {
    "siliconflow": "deepseek-ai/DeepSeek-V4-Flash",
}
# providers tried in order if the primary provider's whole chain fails outright.
FALLBACK_PROVIDERS = ["groq"]

# auto-test loop: after the model writes code, Timmy silently checks it and
# feeds failures back to the model up to this many times before showing you.
AUTOTEST_MAX_ROUNDS = 3

# temperature used ONLY for code generation / auto-fix. A touch above bare-minimum
# determinism: low enough to avoid hallucinated APIs and careless slips, high enough
# that the model reaches for more imaginative mechanics, art and game feel instead of
# the same safe template every time. Design/intake calls run hotter still (see below).
BUILD_TEMPERATURE = 0.22

# temperature for the DESIGN brain: the clickable intake and the question→options
# helper. Imagination is the whole point here, so this runs much hotter than code gen —
# bolder, more surprising, more genre-specific options.
DESIGN_TEMPERATURE = 0.7

# ---- output budget (v1.2.0) ----
# The single nastiest silent failure in 1.1.x: no max_tokens was sent, so providers
# applied their own (often tiny) default completion cap and TRUNCATED whole games
# mid-file. Now every call asks for a real completion budget, clamped per model.
MAX_OUTPUT_TOKENS = 16384
# If a reply still gets cut (finish_reason == "length" or an unterminated code
# fence), Timmy automatically asks the model to continue from the exact cut point
# and stitches the parts together, up to this many times.
CONTINUE_MAX_ROUNDS = 2

# ---- runtime playtest (v1.2.0) ----
# After the import/static checks pass, actually RUN the game headlessly (SDL dummy
# video/audio drivers — no window ever opens), pilot it with synthetic key / mouse /
# touch input for a few seconds, then post QUIT and require a clean exit. Crashes,
# instant exits, and loops that ignore QUIT are fed back to the model as concrete
# tracebacks inside the normal auto-test fix rounds.
RUNTIME_TEST = True
RUNTIME_PLAY_SECONDS = 7
RUNTIME_TIMEOUT = 35

# ---- quality gate (v1.2.0) ----
# On a FRESH build only (never on your iteration requests), once the game passes
# every check, one extra playtest-critique call looks for genuine playability gaps
# (unreachable win, missing lose/restart, no touch controls when the phone is a
# target, spec items plainly missing). If it finds must-fix items, ONE improvement
# round runs; the improved code is kept only if it passes all checks again.
QUALITY_PASS = True

HOST = "127.0.0.1"
PORT = 8765

# This is the heart of it: the model is taught to build COMPLETE, PLAYABLE 2D games the
# way a careful senior game developer does -- agree first, testing build by default,
# release only on request. Targets Kali/KDE/X11 (X395) AND the OnePlus 6 on Phosh/Wayland.
# The Timmy kit (v1.2.0): hand-written, runtime-tested engine substrate that every
# generated game is built ON instead of re-inventing. The model pastes it verbatim at
# the top of each game and writes only game logic below it — so the delta-time loop,
# scene machine, particles, screen shake, easing, touch overlay and procedural sound
# are solved ONCE, correctly, rather than re-fumbled on every build.
GAMEKIT = r'''# ======= TIMMY KIT v1 — engine substrate. Paste VERBATIM; do not modify. =======
import sys, math, random

try:
    import pygame
except ImportError:
    sys.stderr.write("This game needs pygame. Install it with:\n    pip install pygame\n")
    sys.exit(1)

try:
    import numpy as _np
except Exception:
    _np = None


def lerp(a, b, t): return a + (b - a) * t
def ease_out_cubic(t): return 1 - (1 - t) ** 3
def ease_in_out(t): return 3 * t * t - 2 * t * t * t
def clamp(v, lo, hi): return lo if v < lo else hi if v > hi else v


class Sound:
    """Synthesised sfx (numpy + sndarray). Silently does nothing if numpy or the
    mixer is unavailable — sound must never crash or be required."""
    def __init__(self):
        self.ok, self.muted, self._cache = False, False, {}
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            self.ok = _np is not None
        except Exception:
            self.ok = False

    def _tone(self, freq, ms, kind="sine", vol=0.4, sweep=0.0):
        if not self.ok:
            return None
        key = (freq, ms, kind, round(vol, 2), round(sweep, 2))
        if key in self._cache:
            return self._cache[key]
        rate = 44100
        n = int(rate * ms / 1000.0)
        t = _np.linspace(0, ms / 1000.0, n, endpoint=False)
        f = freq + sweep * t * (1000.0 / max(ms, 1))
        if kind == "square":
            wave = _np.sign(_np.sin(2 * _np.pi * f * t))
        elif kind == "saw":
            wave = 2 * (f * t - _np.floor(0.5 + f * t))
        elif kind == "noise":
            wave = _np.random.uniform(-1, 1, n)
        else:
            wave = _np.sin(2 * _np.pi * f * t)
        env = _np.minimum(1.0, _np.linspace(1, 0, n) * 3)
        env *= _np.minimum(1.0, _np.linspace(0, 1, n) * 20)
        samples = (wave * env * vol * 32767).astype(_np.int16)
        stereo = _np.repeat(samples.reshape(-1, 1), 2, axis=1)
        snd = pygame.sndarray.make_sound(_np.ascontiguousarray(stereo))
        self._cache[key] = snd
        return snd

    def play(self, name):
        if not self.ok or self.muted:
            return
        table = {"jump":   (520, 130, "square", 0.35, 240),
                 "land":   (180, 90,  "sine",   0.30, -60),
                 "hit":    (140, 160, "noise",  0.40, 0),
                 "pickup": (760, 120, "square", 0.30, 380),
                 "shoot":  (420, 90,  "saw",    0.25, -180),
                 "blip":   (660, 60,  "square", 0.25, 0),
                 "win":    (640, 320, "square", 0.35, 300),
                 "lose":   (200, 420, "saw",    0.35, -120)}
        spec = table.get(name)
        if not spec:
            return
        snd = self._tone(*spec)
        if snd:
            try: snd.play()
            except pygame.error: pass  # mixer vanished mid-play: stay silent, never crash


class Particles:
    """Pooled particle system — emit with burst(), never allocate per frame."""
    def __init__(self, cap=400):
        self.cap, self.i = cap, 0
        self.p = [dict(life=0.0) for _ in range(cap)]

    def burst(self, x, y, n=12, color=(255, 255, 255), speed=160, spread=math.pi * 2,
              angle=0.0, life=0.5, size=3, gravity=320):
        for _ in range(n):
            d = self.p[self.i]; self.i = (self.i + 1) % self.cap
            a = angle + random.uniform(-spread / 2, spread / 2)
            v = speed * random.uniform(0.4, 1.0)
            d.update(x=x, y=y, vx=math.cos(a) * v, vy=math.sin(a) * v,
                     life=life, max=life, color=color, size=size, g=gravity)

    def update(self, dt):
        for d in self.p:
            if d["life"] <= 0:
                continue
            d["life"] -= dt
            d["vy"] += d["g"] * dt
            d["x"] += d["vx"] * dt
            d["y"] += d["vy"] * dt

    def draw(self, surf, ox=0, oy=0):
        for d in self.p:
            if d["life"] <= 0:
                continue
            a = max(0.0, d["life"] / d["max"])
            s = max(1, int(d["size"] * a))
            try:
                surf.fill(d["color"], (int(d["x"] - ox), int(d["y"] - oy), s, s))
            except (TypeError, ValueError): pass  # bad colour from game code: skip the speck


class TouchControls:
    """On-screen d-pad + A/B buttons. Appears on first touch, translates finger
    input into a held-direction + button API so game code reads keyboard and touch
    identically. Hit zones are deliberately thumb-sized."""
    def __init__(self, w, h):
        self.enabled = False
        self.dir = [0, 0]
        self.a_held = self.b_held = False
        self._touches = {}
        self._layout(w, h)

    def _layout(self, w, h):
        self.w, self.h = w, h
        r = int(min(w, h) * 0.085)
        self.r = r
        cx, cy = int(w * 0.16), int(h * 0.74)
        self.pad = {"L": pygame.Rect(cx - 2 * r, cy - r, r * 2, r * 2),
                    "R": pygame.Rect(cx, cy - r, r * 2, r * 2),
                    "U": pygame.Rect(cx - r, cy - 3 * r, r * 2, r * 2),
                    "D": pygame.Rect(cx - r, cy + r, r * 2, r * 2)}
        self.btnA = pygame.Rect(int(w * 0.82) - r, int(h * 0.70) - r, r * 2, r * 2)
        self.btnB = pygame.Rect(int(w * 0.92) - r, int(h * 0.80) - r, r * 2, r * 2)

    def _refresh(self):
        self.dir = [0, 0]; a = b = False
        for p in self._touches.values():
            if self.pad["L"].collidepoint(p): self.dir[0] = -1
            if self.pad["R"].collidepoint(p): self.dir[0] = 1
            if self.pad["U"].collidepoint(p): self.dir[1] = -1
            if self.pad["D"].collidepoint(p): self.dir[1] = 1
            if self.btnA.collidepoint(p): a = True
            if self.btnB.collidepoint(p): b = True
        self.a_held, self.b_held = a, b

    def handle(self, ev):
        if ev.type == pygame.FINGERDOWN:
            self.enabled = True
            self._touches[ev.finger_id] = (int(ev.x * self.w), int(ev.y * self.h))
        elif ev.type == pygame.FINGERMOTION:
            if ev.finger_id in self._touches:
                self._touches[ev.finger_id] = (int(ev.x * self.w), int(ev.y * self.h))
        elif ev.type == pygame.FINGERUP:
            self._touches.pop(ev.finger_id, None)
        self._refresh()

    def draw(self, surf):
        if not self.enabled:
            return
        ov = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        for k, rect in self.pad.items():
            on = ((k == "L" and self.dir[0] < 0) or (k == "R" and self.dir[0] > 0) or
                  (k == "U" and self.dir[1] < 0) or (k == "D" and self.dir[1] > 0))
            pygame.draw.rect(ov, (255, 255, 255, 90 if on else 40), rect, border_radius=8)
        for rect, on in ((self.btnA, self.a_held), (self.btnB, self.b_held)):
            pygame.draw.circle(ov, (255, 255, 255, 110 if on else 50), rect.center, self.r)
        surf.blit(ov, (0, 0))


class Scene:
    """One game state (Title, Play, Pause, GameOver, ...). Subclass per state;
    the kit owns the loop, you own these hooks."""
    def __init__(self, kit):
        self.kit = kit
    def handle(self, ev): pass
    def update(self, dt): pass
    def draw(self, surf): pass


class Kit:
    """Owns the window, the delta-time loop, scene switching, screen shake,
    hit-stop, particles and sound. Create one, then kit.run(FirstSceneClass)."""
    def __init__(self, title="Game", logical=(480, 270), fps=60, palette=None):
        self.title = title
        self.lw, self.lh = logical
        self.fps = fps
        self.palette = palette or {}
        self.shake = 0.0
        self._hitstop = 0.0
        self.scene = None
        self.particles = Particles()
        self.sound = Sound()
        self.running = True
        self.screen = self.canvas = self.touch = self.clock = None
        self.frame = 0

    def add_shake(self, amt): self.shake = min(self.shake + amt, 24)
    def hitstop(self, ms): self._hitstop = max(self._hitstop, ms / 1000.0)
    def go(self, scene): self.scene = scene
    def quit(self): self.running = False

    def _open(self):
        pygame.init()
        try:
            pygame.display.set_mode((self.lw * 2, self.lh * 2),
                                    pygame.RESIZABLE | pygame.SCALED)
        except Exception:
            pygame.display.set_mode((self.lw * 2, self.lh * 2))
        pygame.display.set_caption(self.title)
        self.screen = pygame.display.get_surface()
        self.canvas = pygame.Surface((self.lw, self.lh)).convert()
        self.touch = TouchControls(self.lw, self.lh)
        self.clock = pygame.time.Clock()

    def run(self, first_scene_cls):
        self._open()
        self.scene = first_scene_cls(self)
        while self.running:
            dt = min(self.clock.tick(self.fps) / 1000.0, 0.05)
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self.running = False
                if ev.type in (pygame.FINGERDOWN, pygame.FINGERMOTION, pygame.FINGERUP):
                    self.touch.handle(ev)
                self.scene.handle(ev)
            if self._hitstop > 0:
                self._hitstop -= dt
            else:
                self.scene.update(dt)
                self.particles.update(dt)
            if self.shake > 0:
                self.shake = max(0.0, self.shake - 60 * dt)
            self.canvas.fill(self.palette.get("bg", (12, 12, 20)))
            self.scene.draw(self.canvas)
            self.particles.draw(self.canvas)
            self.touch.draw(self.canvas)
            ox = oy = 0
            if self.shake > 0.4:
                ox = random.uniform(-self.shake, self.shake)
                oy = random.uniform(-self.shake, self.shake)
            sw, sh = self.screen.get_size()
            self.screen.blit(pygame.transform.scale(self.canvas, (sw, sh)), (ox, oy))
            pygame.display.flip()
            self.frame += 1
        pygame.quit()
# ======= END TIMMY KIT — all game code goes BELOW this line ======='''

_KIT_SECTION = """

THE TIMMY KIT (non-negotiable — every game is built ON this, never from scratch):
Below is a hand-written, runtime-tested engine substrate. EVERY game you produce starts with this
kit pasted VERBATIM at the very top of the file (including both sentinel comment lines), and ALL of
your game code goes below the END sentinel. Rules:
- NEVER modify, trim, reorder, or 'improve' anything between the sentinels — not even whitespace.
  When iterating on a game, reproduce the kit block byte-identical every time. Extend by subclassing
  and composing BELOW it, never by editing inside it.
- BUILD ON IT: subclass `Scene` for every state (Title, Play, Pause, GameOver, Win, ...); switch with
  `self.kit.go(NextScene(self.kit))`; start the game with `Kit(title=..., logical=(480, 270),
  palette=PAL).run(TitleScene)` inside main() under `if __name__ == \"__main__\":`.
- USE the kit instead of re-implementing: `kit.particles.burst(...)` for every impact/pickup/death,
  `kit.add_shake(n)` + `kit.hitstop(ms)` on big hits, `kit.sound.play(name)` with names from the kit's
  table (jump, land, hit, pickup, shoot, blip, win, lose), `lerp/ease_out_cubic/ease_in_out/clamp`
  for all tweening, and `kit.touch.dir` / `kit.touch.a_held` / `kit.touch.b_held` read alongside the
  keyboard every frame so touch works automatically. The kit already handles QUIT, the touch overlay,
  delta-time, letterbox scaling and the canvas — never write your own main loop or set_mode call.
- The logical canvas is `kit.canvas` at `kit.lw x kit.lh` (pick a modest logical size like 480x270 or
  426x240 and draw everything in those coordinates; the kit scales it to the window).
- Esc handling, pausing, options, HUD, levels, dialogue, saving — all YOUR code, in Scenes, below the
  kit. The save-file rule from above (JSON under ~/.local/share/<gameslug>/) still applies.

```python
""" + GAMEKIT + """
```
"""

SYSTEM_PROMPT = """You are Timmy, a senior game developer who builds complete, genuinely PLAYABLE
2D GAMES from a single self-contained Python file using pygame. Any genre is fair game — platformer,
top-down shooter, twin-stick, puzzle, roguelike, RPG, arcade, tower defence, racing, beat-'em-up,
metroidvania, visual novel, rhythm, deckbuilder, bullet-hell, life-sim, anything. Every game you
produce opens a real window and is FUN to play with NOTHING but pygame installed. The two target
machines are:
  1. DESKTOP — Kali Linux on KDE Plasma, X11, on a ThinkPad X395 (keyboard + mouse).
  2. PHONE — a OnePlus 6 running NetHunter Pro / Phosh on Wayland, bare metal (a TOUCHSCREEN, no
     keyboard). This is real Linux, so pygame runs natively; it is NOT Android.
The same single file must run on both. Be GENEROUS, ambitious and CREATIVE: pick a strong, specific
idea and a clear hook, then build it well. When depth or story is asked for, ship a deep, content-rich
game — multiple levels/areas, real progression, actual writing — not a single-screen demo. Hold
yourself to that bar regardless of how the request is phrased. You are not a code-stub generator; you
are a craftsperson who ships small games that feel good in the hand.

YOU LEARN FROM THE BEST. Bring the proven ideas of real engines and tools into every game:
  - GODOT / UNITY — a clean scene/state machine and a node-ish split of responsibilities; decoupled
    feedback via simple events/signals rather than spaghetti.
  - CONSTRUCT / GDEVELOP — composable "behaviours" attached to entities (Platformer, 8-Direction,
    Bullet, Sine/bob, Pin/follow, simple pathfinding). Build a tiny version of these and reuse them.
  - PICO-8 / TIC-80 / GAME BOY — a tight, COHESIVE palette and a deliberate low-res look; sfx made in
    code, not loaded; constraints that make art read clearly. Cohesion beats detail.
  - LÖVE / PHASER — a disciplined delta-time loop, tweens/easing, particle systems, sprite batching.
  - REN'PY / BITSY / RPG MAKER — for narrative games: a script of labels + branching choices, tile
    maps and tile-triggered events as DATA, parties/inventory/quest flags, typewriter dialogue.
  - VLAMBEER / "JUICE IT OR LOSE IT" — game feel is not optional polish, it is the product. See below.

ENGINE (pick ONE — honour the user's intake choice exactly):
- pygame — THE DEFAULT, right for almost everything. SDL2 under the hood, so it runs under BOTH X11
  and Wayland. `pip install pygame` (pygame-ce is a drop-in upgrade; same `import pygame`). Choose
  this unless the user explicitly asks otherwise.
- arcade — modern, OpenGL-accelerated, nice for tile games; only if asked. `pip install arcade`.
- pyglet — lower-level OpenGL; only if asked. `pip install pyglet`.
Stay on ONE engine for the whole game. Never mix engines.

SELF-CONTAINED, RUNS INSTANTLY (non-negotiable — this is what makes "play it" real):
- NO external asset files. Draw ALL visuals procedurally — `pygame.draw` shapes, `Surface`
  gradients, sprites composed in code, simple particles — or render text with the default font
  (`pygame.font.Font(None, size)` / `SysFont`). Never load a .png/.wav/.ttf that isn't shipped.
- Embed ALL content inline as Python data: levels/maps as tile grids or entity lists, dialogue as
  structured dicts/lists, enemy/item tables, palettes. The game opens and PLAYS the instant it
  launches, with only the engine installed — never "missing file" / "couldn't load image".
- You MAY load the user's own art from a folder ONLY if they explicitly ask — and even then fall
  back to procedural drawing when a file is missing, so it still runs.

GIVE THE GAME A REAL NAME (required, every build):
- Choose a short, evocative, FITTING title (e.g. "Neon Drift", "Hollow Bastion", "Spudnik", "Last
  Lantern") — never "Game", "My Game", "Untitled", or the genre word alone. Make it memorable.
- Set it with `pygame.display.set_caption("<Title>")` right after creating the display. Timmy reads
  that caption to name the file and the build, so the title MUST live there.
- On a RELEASE build, also put the title (and a one-line tagline) at the top of the docstring.
- Keep the SAME title across iterations unless the user asks to rename it.

GAME FEEL / JUICE (apply to EVERY game — this is the difference between a tech demo and a game players
*feel*; scale the amount to the game, but never ship it dry):
- HIT FEEDBACK: brief hit-flash (tint the sprite white for a few frames), a short knockback, a tiny
  HIT-STOP / freeze-frame on big impacts (pause logic ~40–90ms), and a damage number or spark.
- SCREEN SHAKE on impacts/explosions/landings — a small decaying random camera offset. Keep it
  tasteful and short; make it scale with the event. Add a settings toggle to reduce/disable it.
- PARTICLES: a tiny reusable particle system (pooled) for dust on landing, sparks on hit, trails,
  explosions, pickups, confetti on win. Particles sell almost everything.
- TWEENING / EASING: never snap UI or important motion. Write a couple of easing helpers
  (ease_out_cubic, ease_in_out, a simple lerp) and use them for menus sliding in, the camera
  following with lag, pop-in scaling, screen fades.
- SQUASH & STRETCH / ANTICIPATION: scale sprites on jump/land/hit; a brief wind-up before a big
  action. Bouncy, alive — not rigid.
- CAMERA: smooth follow (lerp toward the target) with a little look-ahead in the direction of travel;
  clamp to level bounds; shake composes on top.
- JUICY TRANSITIONS: fade or wipe between scenes (title → play → game over); a short "ready?" beat
  before play starts; a satisfying win/lose stinger.
- READABLE FEEDBACK ALWAYS: floating score/damage text, clear telegraphs before enemy attacks, a
  visible combo/score pop, controller/key prompts on screen.
- GENRE FEEL: platformers get COYOTE TIME (~6 frames of grace after leaving a ledge), JUMP BUFFERING
  (queue a jump pressed just before landing), and variable jump height (cut velocity on early
  release). Shooters get recoil, muzzle flashes, and screen-edge feedback. Make controls feel tight.

ARCHITECTURE (use what fits the scope — light for a quick arcade game, full for an RPG):
- DELTA-TIME LOOP: one main loop. `clock = pygame.time.Clock()`; each frame `dt = clock.tick(60) /
  1000.0`; drive ALL motion by dt. For physics-y games use a FIXED TIMESTEP accumulator (step logic
  at a fixed dt, render with interpolation) so collisions are stable on any machine. Always pump the
  event queue every frame and ALWAYS handle `pygame.QUIT`.
- SCENE / STATE MACHINE: a clean Scene/State system (TITLE, PLAYING, PAUSED, DIALOGUE, GAME_OVER,
  WIN, and any of your own). A real title/menu, the game, pause, and a win/lose end with restart —
  not a bare play loop that quits to nothing. Transitions handled centrally.
- ENTITIES + BEHAVIOURS: prefer `pygame.sprite.Sprite`/`Group`, and give entities small composable
  behaviours (move, patrol, chase, shoot, bob) instead of one giant update() per type. OBJECT-POOL
  anything spawned in bulk (bullets, particles, enemies) — reuse, don't allocate per frame.
- EVENTS/SIGNALS: a tiny pub/sub (on("enemy_killed", fn)) to fire score, sfx, particles from one
  place — keeps feedback out of the logic.
- BALANCE BLOCK: gather tunable constants (speeds, gravity, spawn rates, colours, sizes) into a clear
  config section near the top so the game is easy to tune and re-skin. Name things well.

INPUT FOR BOTH MACHINES (same code):
- Desktop: arrows/WASD to move, Space/Enter to act/confirm, Esc to pause/back; mouse where it fits.
- Phone (touch): pygame delivers `FINGERDOWN/FINGERUP/FINGERMOTION` with normalised 0..1 coords —
  multiply by surface size for pixels. Draw an ON-SCREEN control overlay (d-pad or thumbstick +
  large action buttons) with finger-sized hit areas; treat taps/drags as input. pygame also
  synthesises mouse events from touch, so mouse-driven UI works under a finger. Never rely on hover
  or right-click on the phone; make buttons thumb-sized.

DISPLAY & RESOLUTION (must fit a phone, not overflow it): do NOT hardcode a giant fixed window. Pick a
logical render resolution and SCALE it to the actual display (render to an off-screen Surface, then
scale-blit, letterboxing rather than clipping), or use a resizable/SCALED window and adapt layout.
Query `pygame.display.get_desktop_sizes()` when useful; prefer filling the screen on the phone
(SCALED/FULLSCREEN). The SAME code runs under X11 and Wayland — rely on SDL defaults; NEVER hardcode
`SDL_VIDEODRIVER`, `DISPLAY`, or a driver. If you set a video hint, only as a guarded fallback.

PERFORMANCE (smooth on the OnePlus 6): pre-render static surfaces ONCE (backgrounds, text labels,
tiles); call `.convert()`/`.convert_alpha()` after the display exists; use sprite groups; avoid
per-frame allocations and per-pixel Python loops in the hot path; keep the logical resolution modest
and scale up. Cache fonts and rendered text. Aim to hold 60 FPS on the phone.

ART DIRECTION (make it look intentional, not programmer-art):
- Pick ONE cohesive PALETTE (a handful of colours) and stick to it; define it in the balance block.
  Honour any palette the intake chose (e.g. PICO-8-like, Game Boy 4-shade green, neon/vaporwave,
  mono-noir, pastel). Backgrounds with a subtle gradient/parallax read far better than flat fills.
- Strong, readable SILHOUETTES; consistent line/▢pixel scale; a little animation everywhere (idle
  bob, walk cycle via squash, blinking, twinkling stars). Optional, tasteful CRT/scanline or vignette
  overlay if it suits the vibe — keep it cheap and behind a toggle.

AUDIO IN CODE (optional but encouraged — PICO-8 style, all synthesised, never a file):
- Initialise the mixer DEFENSIVELY (`pygame.mixer.init()` in try/except) and degrade to silent on
  failure. If numpy is importable, synthesise short sfx with `pygame.sndarray` (jump, hit, pickup,
  shoot, UI blip, win/lose) — simple sine/square/noise with a quick envelope; clamp and convert to
  int16. If numpy is NOT available, skip sound entirely — NEVER require it and NEVER crash.
- A simple procedural ambience/music loop is a nice touch for bigger games; keep it optional and
  silenceable. Always provide a volume/mute control in options.

DEPTH & STORY (when asked for a deep/story game, DELIVER IN FULL — don't be stingy):
- Several levels/areas/rooms, a real difficulty curve and progression, a complete beginning → middle
  → end. Not a single screen.
- For story/RPG-scale games include: a DIALOGUE SYSTEM (text boxes with speaker names, typewriter
  reveal, advancing lines, portraits drawn in code), BRANCHING CHOICES that actually change what
  happens (Ren'Py-style labels/flags), named NPCs with personality, a coherent WORLD with real lore,
  QUESTS/objectives that track state, an INVENTORY (and party where it fits), tile maps + tile-
  triggered events as DATA, and SAVE/LOAD. Write actual dialogue and lore — never "TODO" or
  "[insert story]" placeholders.
- Keep it ONE FILE but content-RICH: maps as data, dialogue trees as data, everything embedded.

ALWAYS-ON BASELINE (every game, regardless of size, ships with all of these):
- A real animated TITLE screen with the game's name and a "press to start" (and a tappable start).
- An OPTIONS/SETTINGS menu the player can open from the title and pause: at minimum volume/mute and
  a difficulty or screen-shake toggle; persist these to the save file so they stick.
- PAUSE (Esc / a pause button), a clear WIN and LOSE state, and a RESTART path that fully resets.
- A HUD (score / health / lives / objective) and on-screen touch controls when the phone is a target.
- SAVE DATA: high scores / progress / options as JSON under `~/.local/share/<gameslug>/` via
  `pathlib.Path` (honour `$XDG_DATA_HOME`); create the dir if missing; write defensively. NEVER
  persist to the current directory or `/tmp`.

CODE CORRECTNESS — the bugs that pass a parse check and only bite when the window opens. Timmy
pre-checks your code by IMPORTING it, then AUTO-PLAYTESTS it headlessly: the game is actually run
(SDL dummy drivers), piloted with synthetic key/mouse/touch input for several seconds, then sent
pygame.QUIT — it must open its window, survive real play, and exit cleanly. Crashes during play come
back to you as tracebacks to fix. So trace each of these:
- IMPORT-SAFE STRUCTURE: ALL game setup AND the main loop live inside a class and/or a `main()` and
  run ONLY under `if __name__ == "__main__":`. Top level is imports and definitions ONLY — never call
  `pygame.init()`, `pygame.display.set_mode()`, or start the loop at module top level, or importing
  the file will try to open a window and hang the pre-check.
- DEGRADE IF THE ENGINE IS MISSING: import the engine inside a try/except at the very top; on
  ImportError print the exact install line to stderr and exit non-zero:
      pip install pygame
  (for arcade/pyglet, the matching `pip install arcade` / `pip install pyglet`).
- CLEAN LIFECYCLE: create the display once; run the loop once; on quit call `pygame.quit()` then
  `sys.exit()` so you never leave a zombie fullscreen window. Wrap the loop so one exception tears the
  window down cleanly instead of stranding it on top of everything.
- No bare `except: pass`; bounds-check before indexing tile grids; guard divisions; keep
  surface/sound references alive on `self`; match every function/method's argument count; use NO
  invented pygame APIs — if unsure a name exists, use an approach you are sure of.
- numpy is OPTIONAL: only use it for sound, always inside `try: import numpy` and skip sound if absent.
- Self-review pass before you finish: re-read your code once and confirm every name is defined, the
  loop pumps events and handles QUIT, motion uses dt, nothing blocks the loop, on-screen/touch input
  is wired, the caption/title is set, and it actually opens and plays.

METHOD (the build dialogue):
1. CLARIFY ONLY WHAT CHANGES THE GAME. If meaningful details are unresolved, surface the decisions —
   don't dump code on a guess. (Timmy may run a structured intake for you; honour every answer
   precisely — genre, perspective, controls/target, art style, scope & story depth, vibe.) Prefer
   concrete either/or choices. If the user gave a clear idea or says "just build it" / "surprise me",
   BUILD — and if they left it open, make bold, fitting creative choices and TELL them what you chose.
2. TESTING BUILD BY DEFAULT: ONE complete, runnable, single-file game. Lean but COMPLETE — title
   screen, the real game with working mechanics and JUICE, win/lose, options, and (if asked) the
   story content — no packaging ceremony yet. Genuinely playable, not a skeleton.
3. ITERATE on real play feedback: when given a run result / error / "it feels too fast" / "add a
   level", return the FULL updated script (never a diff) and say briefly what you changed and why.
4. RELEASE BUILD ONLY WHEN ASKED: top docstring with title + tagline + how to launch + the controls
   (keyboard and touch), clean structure, an optional minimal argparse for flags (e.g. --version,
   --windowed) that does NOT replace the game, robust error handling, helpful comments, zero dead code.
5. SAFETY: it runs on the user's own machine; no destructive operations.

OUTPUT FORMAT: a tight message first (a few sentences — what you built, the name you gave it, what
you're asking). THEN, only when actually providing code, exactly ONE ```python fenced block with the
entire single-file game — never two blocks. When only planning or discussing, include no code block.""" + _KIT_SECTION

# v1.2.0 — the DESIGN PASS. On a fresh build, one cheap call turns the request +
# intake answers into a compact authoritative spec the code pass then implements.
# Splitting design from code keeps a fast model focused: it stops juggling "what
# should this game be" and "write 900 correct lines" in the same breath.
SPEC_PROMPT = """You are the lead designer for Timmy, a forge for complete, playable 2D pygame games
on Linux (a Kali/KDE desktop with keyboard+mouse AND a OnePlus 6 touchscreen phone). From the user's
request and any intake answers, write ONE compact, authoritative GAME SPEC the programmer will
implement exactly. Be bold and specific — concrete numbers, a real hook, zero hedging. Honour every
choice the user already made precisely; invent confidently where they left it open.

Output PLAIN TEXT (no markdown fences, no JSON), at most ~220 words, exactly these labelled lines:
TITLE: <short evocative name — never generic>
HOOK: <one sentence: what makes THIS game cool>
GENRE/VIEW: <genre + perspective (side-on / top-down / fixed screen)>
TARGET: <desktop / phone / both — from the user's choice; default both>
PALETTE: <4-6 hex colours with roles, e.g. bg #1a1626, player #7adcff, danger #e65a6e ...>
PLAYER: <what the player is + EXACT controls: keys AND the touch mapping (d-pad/A/B)>
CORE LOOP: <what the player does moment-to-moment, with tuned numbers (speeds, jump height, fire
rate, timer) where they matter>
ENTITIES: <each enemy/object: name — behaviour — interaction, one line each, max 5>
PROGRESSION: <levels/waves/areas and how difficulty rises; for story scope: areas, named NPCs, the
arc in one line>
WIN/LOSE: <exact win condition AND exact lose condition + what restart does>
JUICE: <the 4-6 specific feedback moments this game sells: which events get particles, shake,
hitstop, sfx names from (jump land hit pickup shoot blip win lose)>
SCOPE: <quick arcade / substantial / deep-story — match the user's choice>"""

# v1.2.0 — the QUALITY GATE critique. After a fresh build passes every automatic
# check, one playtester call hunts for genuine playability gaps. must_fix is
# reserved for real failures, not taste.
PLAYTEST_PROMPT = """You are a brutally honest senior playtester reviewing a single-file pygame game
against its design spec. You receive the SPEC and the full CODE. Judge it as a player: would this
actually play, and does it deliver what the spec promised?

Return ONLY a JSON object, no prose, no fences:
{"must_fix": [{"title": "<short>", "detail": "<what's wrong + exactly what to change>"}],
 "polish": [{"title": "<short>", "detail": "<nice-to-have>"}]}

must_fix is ONLY for genuine failures (max 4, most important first):
- the win condition is unreachable, or there is no lose state, or restart doesn't fully reset
- a spec line is plainly not implemented (an entity missing, the core mechanic absent, wrong controls)
- the phone is a target but touch input (kit.touch.dir / a_held / b_held) is never read in gameplay
- unfair instant death at spawn, impossible first obstacle, or softlock states
- the title screen is missing, or the game starts straight into play with no way to begin/retry
- gameplay-breaking logic you can see in the code (collision that can't trigger, score never updates)
Everything that is taste, balance polish, or extra content goes in "polish". If the game honestly
delivers the spec, return {"must_fix": [], "polish": [...]} — do NOT invent problems."""

# v1.2.0 — genre craft notes: compact, genre-specific requirements appended to the
# spec so the code pass gets the trade secrets of THIS genre, not generic advice.
GENRE_NOTES = {
    "platformer": ("PLATFORMER CRAFT: coyote time (~0.1s) + a jump buffer (~0.12s) + variable jump "
                   "height (cut vy on early release); squash on land, stretch on jump; one-way "
                   "platforms drop-through with down+jump; camera leads the facing direction."),
    "shooter":    ("SHOOTER CRAFT: pool bullets and enemies (the kit's Particles pattern); fire rate "
                   "as a cooldown timer, never per-frame; muzzle flash + 1-frame recoil per shot; "
                   "brief i-frames + hit-flash on player damage; waves escalate count AND behaviour."),
    "puzzle":     ("PUZZLE CRAFT: the board is pure data, drawing reads it; one input = one move with "
                   "debounce; check win/clears after every move; animate clears with ease_out_cubic "
                   "before removing; an undo stack of board snapshots."),
    "roguelike":  ("ROGUELIKE CRAFT: seed the RNG and show the seed; rooms+corridors generation with "
                   "a guaranteed path to the exit; explored/fog map; instant restart on death with a "
                   "new seed; meta-progress (best depth) in the save file."),
    "story":      ("STORY/RPG CRAFT: dialogue as data (speaker, lines, choices -> labels/flags); "
                   "typewriter text at ~40 chars/s, tap/key to complete then advance; tile maps as "
                   "string grids with tile-triggered events; quest flags drive NPC lines; save/load "
                   "the full flag+position state."),
    "racing":     ("RACING/RUNNER CRAFT: speed ramps with a cap and per-level curve; spawn obstacles "
                   "by DISTANCE not frames; near-miss bonus within a few px; parallax background "
                   "layers at 0.3x/0.6x scroll; subtle speed-shake at top speed."),
    "arcade":     ("ARCADE CRAFT: name the exact classic rules you are honouring, then add ONE twist; "
                   "score with floating +N text; speed/difficulty steps every N points; persistent "
                   "high score in the save file shown on the title screen."),
    "defence":    ("TOWER-DEFENCE CRAFT: the path is a waypoint list enemies lerp along; build spots "
                   "are a grid with thumb-sized tap targets; money/lives HUD always visible; a wave "
                   "preview line ('next: 8 fast'); sell/upgrade on tap of an owned tower."),
}
_GENRE_KEYS = {
    "platformer": ("platform", "jump", "metroidvania"),
    "shooter":    ("shoot", "shmup", "bullet", "twin-stick", "twin stick", "space invader", "galaga"),
    "puzzle":     ("puzzle", "match", "tetris", "sokoban", "block", "sliding"),
    "roguelike":  ("roguelike", "rogue", "dungeon", "procedural"),
    "story":      ("story", "rpg", "visual novel", "dialogue", "quest", "narrative", "adventure"),
    "racing":     ("racing", "runner", "drive", "driving", "race", "endless"),
    "arcade":     ("arcade", "snake", "pong", "breakout", "asteroids", "pac", "frogger", "high score"),
    "defence":    ("tower defence", "tower defense", "defend", "waves of enemies"),
}

def genre_hints(text):
    """Return up to two genre craft notes matched by keyword against the spec/request."""
    low = (text or "").lower()
    hits = [GENRE_NOTES[g] for g, keys in _GENRE_KEYS.items() if any(k in low for k in keys)]
    return "\n".join(hits[:2])

# Used to generate a tailored, clickable intake for a new GAME request.
INTAKE_PROMPT = """You are the design analyst for Timmy, a forge for complete, playable 2D GAMES
written in pygame and run on Linux — a Kali / KDE desktop (keyboard + mouse) and a OnePlus 6 phone on
Phosh / Wayland (touchscreen). The user wants to make a game. Produce a SHORT, HIGH-VALUE, genuinely
CREATIVE set of tappable questions that pin down EXACTLY the right game — no lazy or generic filler.
Think like a game designer pitching options, not a form. Tailor everything to the hint they gave; the
options should feel specific and a little exciting, never boilerplate.

Return ONLY a JSON object, no prose, no markdown fences:
{"summary": "<one line restating the game they want to make, with a bit of flair>",
 "questions": [
   {"q": "<clear question>", "options": ["<opt1>", "<opt2>", "<opt3>"], "multi": false},
   ...
 ]}

Rules:
- 3 to 6 questions MAX. Only ask what genuinely changes the game. Quality of options over quantity.
- ALWAYS include these three:
  * GENRE — tailored to their hint, concrete and evocative, e.g. ["Tight platformer", "Twin-stick
    bullet-hell", "Cozy story RPG", "Falling-block puzzle", "Roguelike dungeon crawl"]. Offer ones
    that actually fit.
  * SCOPE & STORY DEPTH — ["Quick arcade — one screen, chase a high score", "Substantial — several
    levels & real progression", "Deep — story, characters & multiple areas (RPG-scale)"]. This
    decides small vs full story-driven.
  * CONTROLS / TARGET — ["Desktop — keyboard & mouse (KDE / X11)", "OnePlus 6 — touchscreen
    (Phosh / Wayland)", "Both — keyboard + on-screen touch controls"].
- ALWAYS include an ART STYLE / PALETTE question with vivid, named looks, e.g. ["PICO-8 chunky pixels",
  "Game Boy 4-shade green", "Neon / vaporwave glow", "Minimal geometric", "Hand-drawn paper cutout",
  "Mono noir + one accent"]. Pick the 3–4 that suit the idea.
- Tailor the rest to THIS game from: perspective (top-down / side-on / fixed screen), the CORE MECHANIC
  (what the player mainly DOES — make these options interesting and distinct), a VIBE / SETTING /
  THEME, how much JUICE & GAME FEEL ("Snappy & arcadey", "Floaty & dreamy", "Heavy & impactful"), an
  AUDIO choice ("Synthesised retro sfx & music", "Subtle sfx only", "Silent"), enemies/hazards, win &
  lose conditions, difficulty, number of levels, and whether it wants a save file / high-score table.
- WHEN A QUESTION HAS A SENSIBLE DESIGNER DEFAULT, offer an option like "Surprise me — you pick" or
  "You decide", so a user who wants to be bold can hand you the creative call.
- Do NOT ask which OS or which language — it's always pygame on Linux. Do NOT ask about the engine
  unless they signalled they want something other than pygame (pygame is the default).
- 2 to 4 options per question, concrete and mutually distinct. Set "multi": true ONLY when picking
  several genuinely makes sense (e.g. "which mechanics?", "which enemies?").
- Prefer options the user can just tap. Keep them short and flavourful."""

# Turns the model's OWN clarifying questions (asked mid-build, when it returned no
# code) into the same tappable multiple-choice block used for the opening intake — so
# EVERY time Timmy asks you something, you can tap an answer instead of typing it.
FOLLOWUP_PROMPT = """You convert a game-builder assistant's questions into tappable multiple-choice
options. You are given the assistant's latest message to the user (the assistant builds 2D pygame
games for Linux — a Kali / KDE desktop and a OnePlus 6 touchscreen). If that message asks the user
anything — a design choice, a yes/no, which approach they prefer — turn EACH such question into a
clickable question with concrete options the user can just tap.

Return ONLY a JSON object, no prose, no markdown fences:
{"questions": [
   {"q": "<the question, short>", "options": ["<concrete answer>", "..."], "multi": false},
   ...
 ]}

Rules:
- If the assistant is NOT actually asking the user to decide anything (it is only explaining,
  confirming, or reporting what it just built), return {"questions": []}. Never invent questions.
- One entry per real question the assistant asked; keep the user's wording and intent.
- 2 to 4 options each, concrete and mutually distinct, short enough to sit on a button. Offer the
  obvious real answers (include a sensible default; for a yes/no include both). Add an option like
  "you decide" when that is a reasonable answer.
- Set "multi": true ONLY when choosing several genuinely makes sense (e.g. "which enemies?").
  Otherwise false.
- Max 6 questions. The user can always type a free-form reply instead, so do NOT pad — structure
  only what the assistant actually asked."""

# Used by the GitHub-ready flow to assemble repo files from the user's answers.
GITHUB_PROMPT = """You are preparing a polished GitHub release of a 2D pygame GAME that runs on Linux
— a Kali / KDE desktop (keyboard + mouse) and a OnePlus 6 on Phosh / Wayland (touchscreen). You will
be given the final code and the user's repo details. Produce a complete, professional repo.

Return ONLY a JSON object, no prose, no markdown fences:
{"readme": "<full README.md markdown>",
 "gitignore": "<.gitignore contents>",
 "requirements": "<requirements.txt for pip deps, or empty string if pure stdlib>",
 "description": "<one-line repo description>"}

README requirements:
- Title, one-line description, then a short paragraph: what the game is, the genre, and that it is a
  native Linux pygame game that runs on the desktop (X11 / Wayland) and on a Linux phone (touchscreen).
- A "Controls" section listing the keyboard controls AND the touch controls.
- A "Requirements" section: Python >= 3.8 and the pip packages from requirements.txt (e.g. pygame).
- An "Install" section with ONE one-line installer:
    curl -fsSL https://raw.githubusercontent.com/<user>/<repo>/<branch>/install.sh | bash
  The same line should work for updates (re-running it). Use the exact user/repo/branch given.
- A "Play" section: launch from the app menu / launcher, or by running `<name>` from a terminal, and
  a sentence on the goal of the game. Keep it real and copy-pasteable.
- The license name. Clean, scannable, professional. No fluff.

For "requirements": detect imports beyond the stdlib in the code. The usual entry is pygame; others
might be numpy (for sound) or arcade / pyglet. Do NOT list stdlib modules. If pure stdlib, return an
empty string."""

# Used by the "review my code" button: a focused critique that DIAGNOSES, never rewrites.
REVIEW_PROMPT = """You are a senior game developer doing a careful code review of a single-file 2D
pygame game that runs on a Kali / KDE desktop (X11) and a OnePlus 6 on Phosh / Wayland (touchscreen).
You are given the FULL code and, separately, the findings of an automated static analyzer. Your job
is to REVIEW, not rewrite — do NOT output a corrected script.

Look hard for things that will actually bite the player:
- logic errors and clashes: functions called with wrong / missing args, methods that don't exist,
  variables used before assignment, state-machine transitions that dead-end
- GAME-LOOP problems: motion tied to frames instead of delta-time (runs at a different speed on the
  phone); the event queue not pumped (window won't close / input ignored); QUIT not handled; work
  that stalls the loop and drops the framerate; surfaces rebuilt every frame instead of cached
- LINUX / DISPLAY: hardcoded SDL_VIDEODRIVER or DISPLAY (breaks under X11 or Wayland); a fixed window
  size that overflows a phone screen instead of scaling; loading asset files that aren't shipped
- TOUCH / PHONE: no touch (FINGER*) handling or on-screen controls when the phone is a target; hit
  targets too small for a thumb; reliance on hover or keyboard-only input
- correctness: out-of-bounds tile / grid indexing, unhandled error paths, resource leaks, off-by-one,
  collision that misses, a missing or broken win / lose / restart path
- dead or contradictory code, and anything that simply won't do what it claims

Return ONLY a JSON object, no prose, no fences:
{"verdict": "<one short sentence: is it solid, or does it need work?>",
 "issues": [
   {"severity": "high|medium|low", "title": "<short>", "detail": "<what's wrong and why it matters>",
    "line": <line number or null>}
 ],
 "strengths": ["<one or two things done well>"]}

Be specific and honest. If it's genuinely clean, say so with an empty issues list — do not invent
problems. Order issues high severity first. Cap at the ~8 most important."""

DANGER = [
    # POSIX
    r"rm\s+-rf\s+/", r"rm\s+-rf\s+~", r"rm\s+-rf\s+\$HOME", r"rm\s+-rf\s+\*",
    r":\(\)\s*\{", r"shutil\.rmtree\(\s*['\"]/", r"\bmkfs\b",
    r"dd\s+if=", r"\bof=/dev/sd", r"os\.system\(\s*['\"]\s*rm\b", r">\s*/dev/sd",
    r"os\.fork\s*\(\)", r"shutil\.rmtree\(\s*os\.path\.expanduser",
    # Windows
    r"format\s+[a-zA-Z]:\s*/[a-zA-Z]",                  # format c: /q
    r"del\s+/[sSqQfF]\s+/[sSqQfF]",                     # del /s /q /f ...
    r"rd\s+/[sSqQ]\s+/[sSqQ]\s+[a-zA-Z]:\\\\",          # rd /s /q C:\
    r"rmdir\s+/[sSqQ]\s+/[sSqQ]\s+[a-zA-Z]:\\\\",       # rmdir /s /q C:\
    r"cipher\s+/w:[a-zA-Z]:",                           # cipher /w:C:  (overwrite free space)
    r"diskpart",                                        # diskpart (interactive disk wiper)
    r"Remove-Item\s+.*-Recurse\s+.*-Force.*[Cc]:\\\\",  # PowerShell mass delete on C:\
    r"Format-Volume",                                    # PowerShell format
]

# key persistence: per-provider keys in an owner-only config file
CONFIG_PATH = str(config_dir() / "config.json")

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except Exception:
        return {}

def save_config(cfg):
    """Write config (keys + chosen provider) with owner-only perms on POSIX.
    Windows ACLs work differently — the file lives under %APPDATA% which is already
    per-user, so we just write it normally there."""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
        if not IS_WIN:
            try:
                os.chmod(CONFIG_PATH, 0o600)
            except Exception:
                pass
        return True
    except Exception:
        return False

def _initial_keys():
    """env var wins per provider, else the saved config."""
    saved = load_config().get("keys", {})
    keys = {}
    for pid, p in PROVIDERS.items():
        keys[pid] = os.environ.get(p["env"], "").strip() or (saved.get(pid) or "").strip()
    return keys

# session state: per-provider keys + the currently selected provider + chosen model per provider
STATE = {
    "keys": _initial_keys(),
    "provider": load_config().get("provider") or DEFAULT_PROVIDER,
    "models": load_config().get("models", {}),   # {provider_id: chosen_model}
}

def persist_state():
    return save_config({"keys": STATE["keys"], "provider": STATE["provider"],
                        "models": STATE["models"]})

# --------------------------------------------------------------------------
# LIVE MODEL CATALOG  -- ask each provider what YOUR key can actually call
# --------------------------------------------------------------------------
# Cache of {provider_id: [model_id, ...]} fetched from each provider's /models
# endpoint. Avoids the whole class of "model unavailable on your plan" errors that
# come from hardcoded names drifting out of date.
_MODEL_CACHE = {}

# Some providers run multiple regional API hosts whose keys are NOT interchangeable
# (a key from one returns 401 on the other). SiliconFlow is the prime example:
# .com (international) vs .cn (China). We try the configured host first, then the
# alternates, and REMEMBER whichever host accepted the key so every later call uses
# it. This makes "which site was my key from?" a non-issue for the user.
HOST_ALIASES = {
    "siliconflow": ["api.siliconflow.com", "api.siliconflow.cn"],
}
# {provider_id: working_host} once discovered for the current key
_HOST_OK = {}

def _provider_urls(provider_id):
    """Yield (chat_url, models_url) candidates for a provider, best-known host first."""
    prov = PROVIDERS[provider_id]
    base_chat = prov["url"]
    base_models = prov.get("models_url", "")
    aliases = HOST_ALIASES.get(provider_id)
    if not aliases:
        yield base_chat, base_models
        return
    # if we already know which host works for this key, use only that
    known = _HOST_OK.get(provider_id)
    hosts = [known] + [h for h in aliases if h != known] if known else list(aliases)
    # derive the host currently in base_chat so we can swap it
    cur_host = re.sub(r"^https?://([^/]+)/.*$", r"\1", base_chat)
    for h in hosts:
        yield (base_chat.replace(cur_host, h, 1),
               base_models.replace(cur_host, h, 1) if base_models else "")

# crude size ranking so "biggest first" still roughly holds for an unknown catalog
def _model_rank(mid):
    s = mid.lower()
    score = 0
    # explicit param-count hints
    m = re.search(r"(\d+)\s*b\b", s) or re.search(r"-(\d+)b", s)
    if m:
        try: score += int(m.group(1))
        except Exception: pass
    # qualitative hints when there's no number
    for kw, pts in (("pro", 300), ("max", 320), ("ultra", 340), ("405", 405), ("671", 671),
                    ("flagship", 350), ("large", 200), ("70", 70), ("32", 32),
                    ("coder", 40), ("instruct", 10),
                    ("flash", -20), ("mini", -40), ("lite", -45), ("small", -50),
                    ("8b", 8), ("7b", 7), ("3b", 3), ("1.5", -10)):
        if kw in s: score += pts
    # generation/version bonus: a newer major version of the same family should sort
    # first (e.g. deepseek-v4-* above deepseek-v3, gemini-2.5 above gemini-1.5). Weighted
    # heavily enough that a newer generation beats an older one even when the newer is a
    # "flash"/"mini" variant (which otherwise carries a size penalty above).
    vm = re.search(r"v(\d+)\b", s) or re.search(r"-(\d+)\.(\d+)", s)
    if vm:
        try: score += int(vm.group(1)) * 25
        except Exception: pass
    return score

def fetch_models(provider_id, force=False):
    """Fetch the live list of chat models a provider exposes to this key.
    Returns {"models": [...], "source": "live"|"fallback"|"error", "error": ...}."""
    prov = PROVIDERS.get(provider_id)
    if not prov:
        return {"models": [], "source": "error", "error": "unknown provider"}
    if not force and _MODEL_CACHE.get(provider_id):
        return {"models": _MODEL_CACHE[provider_id], "source": "live"}
    key = STATE.get("keys", {}).get(provider_id, "")
    if not key:
        return {"models": list(prov["models"]), "source": "fallback", "error": "no key yet"}

    last_err = None
    # try each candidate host (e.g. SiliconFlow .com then .cn) until one accepts the key
    for chat_url, models_url in _provider_urls(provider_id):
        if not models_url:
            continue
        host = re.sub(r"^https?://([^/]+)/.*$", r"\1", models_url)
        try:
            req = urllib.request.Request(models_url, headers={
                "Authorization": "Bearer " + key,
                "User-Agent": f"timmy/{__version__}",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            items = data.get("data", data if isinstance(data, list) else [])
            ids = []
            for it in items:
                mid = it.get("id") if isinstance(it, dict) else str(it)
                if not mid:
                    continue
                low = mid.lower()
                # keep chat/text LLMs only; drop embeddings/rerank/image/audio/video/tts/etc.
                if any(b in low for b in ("embed", "rerank", "bge-", "whisper", "tts", "stt",
                                          "stable-diffusion", "flux", "sdxl", "kolors", "cogvideo",
                                          "wan-", "speech", "audio", "image", "video", "vl-",
                                          "-vl", "vision", "ocr")):
                    continue
                ids.append(mid)
            if not ids:
                last_err = "no chat models returned"
                continue
            ids = sorted(set(ids), key=_model_rank, reverse=True)
            _MODEL_CACHE[provider_id] = ids
            if provider_id in HOST_ALIASES:
                _HOST_OK[provider_id] = host   # remember the host that worked for this key
            return {"models": ids, "source": "live", "host": host}
        except urllib.error.HTTPError as e:
            detail = ""
            try: detail = e.read().decode(errors="replace")[:150]
            except Exception: pass
            if e.code == 401:
                last_err = "key rejected (401)"
                continue   # try the next host — a .cn key 401s on .com and vice versa
            if e.code == 403:
                last_err = "forbidden (403): " + detail
                continue
            last_err = f"HTTP {e.code}" + (": "+detail if detail else "")
        except Exception as e:
            last_err = str(e)

    # nothing worked → fall back to the static list, with a clear reason
    hint = ""
    if provider_id in HOST_ALIASES and last_err and "401" in last_err:
        hint = (" — the key was rejected on every SiliconFlow host (.com and .cn). "
                "Re-copy the key (watch for spaces), or check the account needs verification.")
    return {"models": list(prov["models"]), "source": "fallback",
            "error": (last_err or "could not reach provider") + hint}

def provider_model_chain(provider_id):
    """The model order to try: live catalog if we have it, else the static fallback."""
    return _MODEL_CACHE.get(provider_id) or list(PROVIDERS[provider_id]["models"])

# --------------------------------------------------------------------------
# TOOL LIBRARY  -- persistent, reloadable tools (code + conversation)
# --------------------------------------------------------------------------
LIBRARY_DIR = str(app_data_dir() / "library")

def _safe_id(name):
    return re.sub(r"[^A-Za-z0-9_\-]", "_", (name or "tool")).strip("_") or "tool"

def library_save(name, code, messages, version="testing", args="", sid=None, ver="1.0"):
    """Snapshot a tool to the library at its CURRENT state: its code, the full build
    conversation, the version badge, and the test args. Reopening it restores all of
    that so you continue exactly where you left off — like saving a chat."""
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    tid = _safe_id(name)
    rec = {"id": tid, "name": name or tid, "code": code,
           "messages": messages or [], "version": version or "testing",
           "ver": ver or "1.0",
           "args": args or "", "toolkit": (detect_toolkit(code or "") or {}).get("label"),
           "from_session": sid, "saved": time.strftime("%Y-%m-%d %H:%M")}
    with open(os.path.join(LIBRARY_DIR, tid + ".json"), "w") as f:
        json.dump(rec, f)
    return {"id": tid, "saved": rec["saved"]}

def library_list():
    if not os.path.isdir(LIBRARY_DIR):
        return {"tools": []}
    tools = []
    for fn in os.listdir(LIBRARY_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(LIBRARY_DIR, fn)) as f:
                r = json.load(f)
            tools.append({"id": r.get("id"), "name": r.get("name"),
                          "saved": r.get("saved"), "toolkit": r.get("toolkit"),
                          "version": r.get("version", "testing"),
                          "ver": r.get("ver", "1.0"),
                          "lines": len((r.get("code") or "").splitlines())})
        except Exception:
            continue
    tools.sort(key=lambda t: t.get("saved", ""), reverse=True)
    return {"tools": tools}

def library_load(tid):
    path = os.path.join(LIBRARY_DIR, _safe_id(tid) + ".json")
    if not os.path.exists(path):
        return {"error": "not found"}
    with open(path) as f:
        return {"tool": json.load(f)}

def library_delete(tid):
    path = os.path.join(LIBRARY_DIR, _safe_id(tid) + ".json")
    try:
        os.remove(path); return {"ok": True}
    except Exception as e:
        return {"error": str(e)}

# --------------------------------------------------------------------------
# SESSIONS  -- live works-in-progress (auto-saved as you build), like chats
# --------------------------------------------------------------------------
SESSION_DIR = str(app_data_dir() / "sessions")

def session_save(sid, name, code, messages, version="testing", args="", ver="1.0"):
    """Auto-save the live conversation+code for a tool in progress (its full state)."""
    os.makedirs(SESSION_DIR, exist_ok=True)
    sid = sid or time.strftime("s%Y%m%d-%H%M%S")
    rec = {"id": sid, "name": name or "untitled", "code": code or "",
           "messages": messages or [], "version": version or "testing",
           "ver": ver or "1.0", "args": args or "",
           "toolkit": (detect_toolkit(code or "") or {}).get("label"),
           "updated": time.strftime("%Y-%m-%d %H:%M")}
    with open(os.path.join(SESSION_DIR, _safe_id(sid) + ".json"), "w") as f:
        json.dump(rec, f)
    return {"id": sid, "updated": rec["updated"]}

def session_list():
    if not os.path.isdir(SESSION_DIR):
        return {"sessions": []}
    out = []
    for fn in os.listdir(SESSION_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSION_DIR, fn)) as f:
                r = json.load(f)
            msgs = r.get("messages", [])
            out.append({"id": r.get("id"), "name": r.get("name"),
                        "updated": r.get("updated"), "toolkit": r.get("toolkit"),
                        "ver": r.get("ver", "1.0"),
                        "turns": sum(1 for m in msgs if m.get("role") == "user"),
                        "hasCode": bool(r.get("code"))})
        except Exception:
            continue
    out.sort(key=lambda s: s.get("updated", ""), reverse=True)
    return {"sessions": out}

def session_load(sid):
    path = os.path.join(SESSION_DIR, _safe_id(sid) + ".json")
    if not os.path.exists(path):
        return {"error": "not found"}
    with open(path) as f:
        return {"session": json.load(f)}

def session_delete(sid):
    try:
        os.remove(os.path.join(SESSION_DIR, _safe_id(sid) + ".json")); return {"ok": True}
    except Exception as e:
        return {"error": str(e)}

# --------------------------------------------------------------------------
# GUI TOOLKITS  -- detect which windowing toolkit a tool uses, and how to get it
# --------------------------------------------------------------------------
# Maps a top-level import to (human label, pip package name, linux-specific apt hint).
# Everything is installable via pip on all three OSes. The apt hint is ONLY shown to
# Linux users in error messages — Tkinter on Linux often needs `python3-tk` as well
# because it's a C extension that Debian splits out of the base python3 package.
GUI_TOOLKITS = {
    # --- game engines (the primary target of Timmy) -------------------------
    # pygame is the default; SDL2 under the hood, so it runs under BOTH X11 (KDE
    # desktop) and Wayland (Phosh on the OnePlus 6). pygame-ce imports as `pygame`
    # too, so this one entry covers both. arcade/pyglet are offered on request.
    "pygame":       ("pygame",         "pygame",         None),
    "arcade":       ("arcade",         "arcade",         None),
    "pyglet":       ("pyglet",         "pyglet",         None),
    # --- GUI toolkits (still detected, e.g. a tiny menu/launcher game on Tk) ----
    "tkinter":      ("Tkinter",        None,             "python3-tk"),
    "customtkinter": ("CustomTkinter", "customtkinter",  "python3-tk"),  # needs Tk under the hood
    "PyQt5":        ("PyQt5",          "PyQt5",          None),
    "PyQt6":        ("PyQt6",          "PyQt6",          None),
    "PySide6":      ("PySide6",        "PySide6",        None),
    "PySide2":      ("PySide2",        "PySide2",        None),
    "wx":           ("wxPython",       "wxPython",       None),
}

def detect_toolkit(code):
    """Return the GUI toolkit a tool uses, or None.
    Result shape: {module, label, pip (pip package or None), apt_hint (Linux only)}."""
    tops = set()
    for m in re.finditer(r"^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)", code, re.M):
        tops.add(m.group(1).split(".")[0])
    for mod, (label, pip, apt_hint) in GUI_TOOLKITS.items():
        if mod in tops:
            return {"module": mod, "label": label, "pip": pip, "apt_hint": apt_hint}
    return None

# --------------------------------------------------------------------------
# DEPENDENCIES  -- detect third-party imports, optionally install into a venv
# --------------------------------------------------------------------------
def detect_deps(code):
    """Return third-party pip deps + the GUI toolkit pip package (if any).
    On Timmy EVERYTHING — including the GUI toolkit — installs via pip, so the
    UI just needs one unified install button. Tkinter is stdlib so it has no pip
    package, but on Linux it may need an apt hint."""
    std = getattr(sys, "stdlib_module_names", set())
    obvious = {"os","sys","re","io","json","time","math","socket","subprocess","argparse",
               "itertools","collections","random","hashlib","base64","struct","threading",
               "datetime","pathlib","shutil","csv","urllib","textwrap","glob","tempfile",
               "functools","typing","enum","dataclasses","queue","signal","select","ssl",
               "ipaddress","binascii","zlib","gzip","sqlite3","html","xml","http","email",
               "platform","tkinter"}
    toolkit_mods = set(GUI_TOOLKITS.keys())
    pip = set()
    for m in re.finditer(r"^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)", code, re.M):
        top = m.group(1).split(".")[0]
        if (top and top not in std and top not in obvious
                and top not in toolkit_mods and not top.startswith("_")):
            pip.add(top)
    tk = detect_toolkit(code)
    # roll the toolkit's pip name into the pip list, so one click installs everything
    if tk and tk.get("pip"):
        pip.add(tk["pip"])
    return {"pip": sorted(pip), "toolkit": tk}


VENV_DIR = str(app_data_dir() / "venv")

def _venv_python():
    """Return the python interpreter inside our managed venv, or None if not built yet.
    Windows lives in Scripts/python.exe; POSIX in bin/python."""
    cands = [Path(VENV_DIR) / "Scripts" / "python.exe",
             Path(VENV_DIR) / "bin" / "python",
             Path(VENV_DIR) / "bin" / "python3"]
    for c in cands:
        if c.exists():
            return str(c)
    return None

def install_deps(pkgs):
    """Install pip packages into Timmy's managed venv. Returns log + python path.
    The venv is created WITH access to system site-packages so a tool can use BOTH
    pip packages installed here AND anything Python already has on this machine."""
    if not pkgs:
        return {"ok": True, "log": "no pip packages to install — already covered", "python": sys.executable}
    try:
        if not os.path.isdir(VENV_DIR):
            import venv
            # system_site_packages=True so the venv can still import packages already
            # available in the system Python (avoids re-installing things twice).
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(VENV_DIR)
        vpy = _venv_python() or sys.executable
        proc = subprocess.run([vpy, "-m", "pip", "install", "--upgrade", *pkgs],
                              capture_output=True, text=True, timeout=600,
                              encoding="utf-8", errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "log": out[-1800:], "python": vpy}
    except Exception as e:
        return {"ok": False, "log": f"venv/install failed: {e}", "python": sys.executable}

# the interpreter used to run a generated tool. We prefer the managed venv (which sees
# the system site-packages too, so it has everything available). If the venv hasn't
# been built yet, we fall back to the interpreter Timmy itself is running on.
def run_python(code=None):
    return _venv_python() or sys.executable

# ==========================================================================
# helpers
# ==========================================================================
def looks_dangerous(code):
    return [p for p in DANGER if re.search(p, code)]

def _http_post(url, headers, body, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

# --------------------------------------------------------------------------
# CONTEXT BUDGET  -- keep requests under the ACTIVE model's real window
# --------------------------------------------------------------------------
# The previous version used one fixed budget (120k chars). That was the bug behind
# "works on a fresh tool, dies after long use": a long session would fall through
# the model chain to a SMALL-context model (e.g. an 8k-token model) for which 120k
# chars is wildly over the limit — so the request 400'd even though trimming "ran".
# Now we budget against the specific model being called.
#
# Context windows in TOKENS (input side). ~3.5 chars/token for code-heavy text, and
# we reserve room for the reply, so usable input chars ≈ tokens * 3. Unknown models
# get a conservative default so we never overshoot a small one.
MODEL_CONTEXT_TOKENS = {
    # Groq
    "llama-3.3-70b-versatile": 128000, "openai/gpt-oss-120b": 128000,
    "openai/gpt-oss-20b": 128000, "gemma2-9b-it": 8192, "llama-3.1-8b-instant": 128000,
    # SiliconFlow
    "deepseek-ai/deepseek-v3": 64000, "qwen/qwen2.5-72b-instruct": 32000,
    "qwen/qwen2.5-coder-32b-instruct": 32000, "deepseek-ai/deepseek-v2.5": 32000,
    "qwen/qwen2.5-7b-instruct": 32000,
    # Google
    "gemini-2.5-pro": 1000000, "gemini-2.5-flash": 1000000, "gemini-2.0-flash": 1000000,
    "gemini-1.5-pro": 2000000, "gemini-1.5-flash": 1000000,
    # Novita
    "deepseek/deepseek-v3": 64000, "qwen/qwen-2.5-72b-instruct": 32000,
    "meta-llama/llama-3.1-70b-instruct": 128000, "meta-llama/llama-3.1-8b-instruct": 128000,
    # DeepSeek (first-party) — V4 Pro and Flash both carry a 1M-token window
    # DeepSeek V4 context windows (1M-token), as exposed via SiliconFlow / Novita
    "deepseek-ai/deepseek-v4-flash": 1000000, "deepseek-ai/deepseek-v4-pro": 1000000,
    "deepseek/deepseek-v4-flash": 1000000, "deepseek/deepseek-v4-pro": 1000000,
}
DEFAULT_CONTEXT_TOKENS = 16000      # safe assumption for an unknown model
REPLY_RESERVE_TOKENS   = 4000       # minimum room always left for the model's answer

def _max_output_tokens(model):
    """Completion budget for a model: the full MAX_OUTPUT_TOKENS on big-window
    models, clamped to half the window on small ones so input + output always fit."""
    ctx = MODEL_CONTEXT_TOKENS.get((model or "").lower(), DEFAULT_CONTEXT_TOKENS)
    return max(1024, min(MAX_OUTPUT_TOKENS, ctx // 2))

def context_budget_chars(model):
    """Usable input-char budget for a specific model, conservatively converted from
    its token window with headroom reserved for the reply — sized to the completion
    budget we actually request (v1.2.0), not a fixed guess."""
    toks = MODEL_CONTEXT_TOKENS.get((model or "").lower(), DEFAULT_CONTEXT_TOKENS)
    reserve = max(REPLY_RESERVE_TOKENS, _max_output_tokens(model))
    usable = max(2000, toks - reserve)
    # ~3 input chars per token (conservative for code), capped so we never send an
    # absurdly huge request even to a million-token model (keeps latency/cost sane).
    # The cap is generous enough that a large tool plus a long build conversation
    # survives on a big-window model (e.g. DeepSeek V4 Flash / Gemini) instead of
    # being trimmed prematurely, but still bounds latency and token spend.
    return min(usable * 3, 600_000)

def _msg_len(m):
    return len(m.get("content", "") or "")

# matches a fenced code block so we can collapse superseded copies
_CODE_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n.*?```", re.S)

def trim_history(messages, model=None):
    """Keep a long build conversation under the ACTIVE model's window without losing
    what matters. Two-stage:
      1. COLLAPSE every OLD assistant code block into a one-line placeholder — only the
         most recent full script is kept verbatim. (This is the real fix: long sessions
         accumulate many full copies of the same growing program, and that redundancy,
         not the chat, is what blows the context window.)
      2. If still over budget, drop the stale middle of the conversation, keeping the
         system prompt, the current code, and the most recent turns; leave a marker.
    """
    if not messages:
        return messages
    budget_total = context_budget_chars(model)

    system = [m for m in messages if m.get("role") == "system"]
    body   = [m for m in messages if m.get("role") != "system"]

    # ---- stage 1: collapse superseded code blocks ----
    last_code_idx = None
    for i in range(len(body) - 1, -1, -1):
        if body[i].get("role") == "assistant" and "```" in (body[i].get("content") or ""):
            last_code_idx = i
            break
    if last_code_idx is not None:
        for i in range(len(body)):
            if i == last_code_idx:
                continue
            m = body[i]
            if m.get("role") == "assistant" and "```" in (m.get("content") or ""):
                collapsed = _CODE_FENCE.sub("`[earlier version of the code — superseded by the latest below]`",
                                            m["content"])
                body[i] = {"role": m["role"], "content": collapsed}

    sys_len = sum(_msg_len(m) for m in system)
    budget  = budget_total - sys_len
    total   = sum(_msg_len(m) for m in body)
    if total <= budget:
        return system + body   # stage-1 collapse alone got us under the limit

    # ---- stage 2: drop the stale middle, force-keeping the current code ----
    # recompute the code index after collapse (it didn't move)
    kept_tail, used = [], 0
    for i in range(len(body) - 1, -1, -1):
        m = body[i]
        L = _msg_len(m)
        if used + L <= budget or not kept_tail:
            kept_tail.append(m); used += L
        elif i == last_code_idx:
            content = m.get("content") or ""
            if L > budget:
                content = content[: max(2000, budget - 200)] + "\n# …(truncated by Timmy to fit this model)…"
            kept_tail.append({"role": m["role"], "content": content}); used += min(L, budget)
        else:
            continue
    kept_tail.reverse()

    dropped = len(body) - len(kept_tail)
    marker = []
    if dropped > 0:
        marker = [{"role": "user", "content":
                   f"(Timmy note: {dropped} earlier message(s) were trimmed to fit this model's "
                   f"context window. The current code and recent discussion are below; treat the "
                   f"latest code block as the source of truth.)"}]
    result = system + marker + kept_tail

    # ---- stage 3: HARD GUARANTEE — never exceed budget, even by one char ----
    # Stages 1-2 can land slightly over (the newest message is kept whole, the system
    # prompt is large, etc.). That residual overflow was the real cause of the 400 that
    # struck only after long use. Here we make overflow impossible: while the payload is
    # over the model's total budget, truncate the single largest NON-system message (the
    # current code, almost always) until everything fits with headroom.
    def _total(ms): return sum(_msg_len(m) for m in ms)
    guard = 0
    while _total(result) > budget_total and guard < 200:
        guard += 1
        # find the largest message that isn't a system message
        idx, biggest = -1, -1
        for i, m in enumerate(result):
            if m.get("role") == "system":
                continue
            L = _msg_len(m)
            if L > biggest:
                biggest, idx = L, i
        if idx < 0 or biggest <= 0:
            break
        over = _total(result) - budget_total
        # cut the overflow plus a small margin, but keep at least a stub
        keep_len = max(500, _msg_len(result[idx]) - over - 400)
        c = result[idx]["content"]
        if keep_len >= len(c):
            break
        result[idx] = {"role": result[idx]["role"],
                       "content": c[:keep_len] + "\n…(truncated by Timmy to fit this model's context)…"}
    return result

def call_model(messages, provider_id=None, temperature=0.3, _fallback_chain=None, max_tokens=None):
    """Call the selected provider, falling through its model chain on error.
    Returns {"reply", "model", "provider", "finish"} or {"error"}.
    `temperature` defaults to 0.3; the code-build path lowers it for determinism.
    v1.2.0: every request now carries an explicit max_tokens (per-model completion
    budget) — without it, providers applied their own often-tiny default cap and
    silently TRUNCATED whole games mid-file. "finish" carries finish_reason so the
    caller can detect a cut reply and auto-continue it.
    If the whole provider chain fails AND a key exists for a configured fallback
    provider (e.g. Groq behind SiliconFlow), the call is retried there once so a
    SiliconFlow outage or quota stop doesn't dead-end the build."""
    pid = provider_id or STATE.get("provider") or DEFAULT_PROVIDER
    prov = PROVIDERS.get(pid)
    if not prov:
        return {"error": f"Unknown provider '{pid}'."}
    key = STATE.get("keys", {}).get(pid, "")
    # compute fallback providers up front so even a missing key can fall through.
    if _fallback_chain is None:
        _fallback_chain = [p for p in FALLBACK_PROVIDERS
                           if p != pid and STATE.get("keys", {}).get(p)]
    if not key:
        if _fallback_chain:
            nxt_pid, rest = _fallback_chain[0], _fallback_chain[1:]
            alt = call_model(messages, nxt_pid, temperature, _fallback_chain=rest,
                             max_tokens=max_tokens)
            if not alt.get("error"):
                alt["fellback_from"] = pid
                return alt
        return {"error": f"No API key for {prov['label']}. Add it in Settings, "
                         f"or set {prov['env']} and restart."}

    # raw history; trimmed PER MODEL inside the loop (each model has its own window)
    raw_messages = messages

    # model order: a user-chosen model wins; otherwise fall back to this provider's
    # configured default (e.g. DeepSeek V4 Flash on SiliconFlow) so the primary model
    # is honoured even though the live catalog is rank-sorted (which would otherwise
    # float the pricier V4 Pro to the top). Whatever we pick is pinned to the front.
    chosen = STATE.get("models", {}).get(pid) or DEFAULT_MODEL_BY_PROVIDER.get(pid)
    chain = provider_model_chain(pid)
    if chosen:
        # match case-insensitively against the live chain so a slightly different
        # capitalisation from the catalog doesn't create a duplicate entry.
        cl = chosen.lower()
        chain = [chosen] + [m for m in chain if m.lower() != cl]

    # which host to call: the one fetch_models proved works for this key, else the
    # configured one. (Handles SiliconFlow .com vs .cn automatically.)
    chat_url = prov["url"]
    for cu, _mu in _provider_urls(pid):
        chat_url = cu
        break

    last = None
    context_hit = False
    _retried_host = [False]   # one-shot host re-discovery guard (mutable for closure-free use)
    _capped = set()           # models we already retried once with a smaller max_tokens
    for model in chain:
        # trim to THIS model's context window — the fix for "dies after long use":
        # a small-context model deeper in the chain now gets a request sized for it.
        messages = trim_history(raw_messages, model)
        # pre-flight: if even the trimmed payload won't fit this model (e.g. system
        # prompt + current code alone exceeds a tiny 8k window), skip it instead of
        # sending a request we know will 400. A bigger model later in the chain may fit.
        if sum(_msg_len(m) for m in messages) > context_budget_chars(model):
            last = f"{model}: skipped (payload exceeds its context window)"
            context_hit = True
            continue
        # per-model completion budget — the v1.2.0 fix for silently truncated games.
        mo = _max_output_tokens(model)
        if max_tokens is not None:
            mo = max(256, min(int(max_tokens), mo))
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                "User-Agent": f"timmy/{__version__}",
                "Accept": "application/json",
            }
            body = {"model": model, "temperature": temperature,
                    "messages": messages, "max_tokens": mo}
            data = _http_post(chat_url, headers, body, timeout=300)
            choice = data["choices"][0]
            reply = choice["message"]["content"]
            return {"reply": reply, "model": model, "provider": pid,
                    "finish": (choice.get("finish_reason") or "").lower()}
        except urllib.error.HTTPError as e:
            detail = ""
            try: detail = e.read().decode(errors="replace")[:400]
            except Exception: pass
            low = detail.lower()
            # --- the provider rejected OUR completion budget (max_tokens cap) ---
            # Must run before the context check below, which also matches "max_tokens".
            if (e.code in (400, 413, 422) and model not in _capped
                    and re.search(r"max_?(?:new_|completion_)?tokens", low)):
                _capped.add(model)
                num = re.search(r"max_?(?:new_|completion_)?tokens\D{0,60}?(\d{3,6})", low)
                new_mo = max(1024, min(int(num.group(1)), mo - 1) if num else mo // 2)
                if new_mo < mo:
                    try:
                        body = {"model": model, "temperature": temperature,
                                "messages": messages, "max_tokens": new_mo}
                        data = _http_post(chat_url, headers, body, timeout=300)
                        choice = data["choices"][0]
                        reply = choice["message"]["content"]
                        return {"reply": reply, "model": model, "provider": pid,
                                "finish": (choice.get("finish_reason") or "").lower()}
                    except Exception as e2:
                        last = f"{model}: retry with max_tokens={new_mo} failed: {e2}"
                        continue
            # --- the conversation got too big for this model's context window ---
            if (e.code in (400, 413) and any(s in low for s in (
                    "context", "token", "maximum context", "too long", "context_length",
                    "context length", "max_tokens", "reduce the length", "input is too long"))):
                context_hit = True
                last = f"{model}: context-window limit"
                continue   # a smaller-context sibling won't help, but try in case limits differ
            if e.code == 403 and "1010" in detail:
                return {"error": f"Blocked by Cloudflare (403/1010) before reaching "
                                 f"{prov['label']}. Usually a VPN/proxy or outdated client, not your key."}
            if e.code == 401:
                # For a multi-host provider (SiliconFlow .com/.cn), a 401 may just mean
                # we're hitting the wrong regional host for this key. Discover the right
                # one and retry this same request once.
                if pid in HOST_ALIASES and not _retried_host[0]:
                    _retried_host[0] = True
                    probe = fetch_models(pid, force=True)
                    if probe.get("source") == "live" and _HOST_OK.get(pid):
                        new_url = None
                        for cu, _mu in _provider_urls(pid):
                            new_url = cu; break
                        if new_url and new_url != chat_url:
                            chat_url = new_url
                            # retry the very same model against the correct host
                            try:
                                body = {"model": model, "temperature": temperature,
                                        "messages": messages, "max_tokens": mo}
                                data = _http_post(chat_url, headers, body, timeout=300)
                                choice = data["choices"][0]
                                reply = choice["message"]["content"]
                                return {"reply": reply, "model": model, "provider": pid,
                                        "finish": (choice.get("finish_reason") or "").lower()}
                            except Exception as e2:
                                last = f"{model}: retry on {_HOST_OK[pid]} failed: {e2}"
                                continue
                return {"error": f"{prov['label']} rejected the key (401). Check it in Settings — "
                                 f"and confirm you're using a {prov['label']} key, not another provider's."
                                 + (" For SiliconFlow, the key must be from the same site as the "
                                    "endpoint (cloud.siliconflow.com \u2194 api.siliconflow.com)."
                                    if pid == "siliconflow" else "")}
            if e.code == 429:
                return {"error": f"{prov['label']} rate-limited this request (429): "
                                 f"{detail or 'slow down or check your quota'}."}
            if e.code in (404, 400):
                # this specific model name isn't callable with your key — try the next
                last = f"{model}: HTTP {e.code} (this model isn't available to your {prov['label']} key)"
                continue
            last = f"{model}: HTTP {e.code} {detail}"
        except Exception as e:
            last = f"{model}: {e}"

    def _try_fallback(reason):
        if _fallback_chain:
            nxt_pid, rest = _fallback_chain[0], _fallback_chain[1:]
            alt = call_model(messages, nxt_pid, temperature, _fallback_chain=rest,
                             max_tokens=max_tokens)
            if not alt.get("error"):
                alt["fellback_from"] = pid
                return alt
        return None

    if context_hit:
        return {"error": "context_overflow",
                "detail": "Your current tool plus the build conversation is too large for the "
                          "available model(s). Timmy already collapses old code revisions and "
                          "trims old turns automatically, so this means the tool itself is now very "
                          "big. Two fixes: pick a larger-context model in Settings (Gemini and the "
                          "70B/120B models have huge windows), or hit ＋ new tool to start fresh — "
                          "your saved work in the library is untouched. You can also save the current "
                          "tool to the library first, then reopen it in a clean session to keep going."}
    alt = _try_fallback(last)
    if alt:
        return alt
    return {"error": f"{prov['label']} chain failed. Last: {last}. "
                     f"Try Settings → refresh models, or pick a different model/provider."}

def extract_code(reply):
    """Pull the python code block out of a model reply (tagged, else any fence)."""
    m = re.search(r"```(?:python|py)\s*\n(.*?)```", reply, re.S | re.I) \
        or re.search(r"```\s*\n(.*?)```", reply, re.S)
    return m.group(1).rstrip() if m else None

def replace_first_code_block(reply, new_code):
    """Swap the body of the first python/py fenced block (or any fenced block) in a
    reply with new_code, preserving the surrounding prose. Mirrors extract_code's
    block selection so the swapped block is the same one the rest of the app reads.
    Returns the rewritten reply, or the original if no fenced block is present."""
    nc = new_code.rstrip()
    def _do(m):
        return m.group(1) + nc + "\n" + m.group(3)
    for pat in (re.compile(r"(```(?:python|py)\s*\n)(.*?)(```)", re.S | re.I),
                re.compile(r"(```\s*\n)(.*?)(```)", re.S)):
        if pat.search(reply):
            return pat.sub(_do, reply, count=1)
    return reply

def _unterminated(reply):
    """True when a reply was visibly cut mid-code: an odd number of ``` fences."""
    return bool(reply) and reply.count("```") % 2 == 1

_CONTINUE_MSG = ("Your previous reply was cut off by the output token limit, mid-stream. Continue "
                 "EXACTLY where it stopped: output ONLY the remaining content — no preamble, no "
                 "apology, do NOT repeat anything already sent, and do NOT open a new code fence. "
                 "If you were inside the ```python block, just keep writing the code from the exact "
                 "cut point and close the block with ``` when the script is complete.")

def _call_with_continue(messages, provider_id=None, temperature=0.3, max_tokens=None):
    """v1.2.0 — call_model plus automatic continuation: when a reply is cut by the
    completion limit (finish_reason == 'length', or an unterminated code fence),
    ask the model to continue from the exact cut point and stitch the parts into
    one seamless reply, up to CONTINUE_MAX_ROUNDS times. The death of half-games."""
    res = call_model(messages, provider_id, temperature, max_tokens=max_tokens)
    if res.get("error"):
        return res
    rounds = 0
    while rounds < CONTINUE_MAX_ROUNDS and (
            res.get("finish") == "length" or _unterminated(res.get("reply", ""))):
        rounds += 1
        partial = res.get("reply", "")
        convo = list(messages) + [{"role": "assistant", "content": partial},
                                  {"role": "user", "content": _CONTINUE_MSG}]
        nxt = call_model(convo, provider_id, temperature, max_tokens=max_tokens)
        if nxt.get("error"):
            res["continue_note"] = "continuation call failed: " + nxt["error"]
            break
        cont = nxt.get("reply", "") or ""
        # models love to re-open the fence — strip a leading fence line if we're
        # already inside one, and drop a duplicated overlap line.
        if _unterminated(partial):
            cont = re.sub(r"^\s*```[a-zA-Z0-9_+-]*[ \t]*\n", "", cont, count=1)
        p_lines = [l for l in partial.splitlines() if l.strip()]
        c_lines = cont.splitlines()
        if p_lines and c_lines and c_lines and c_lines[0].strip() and \
                c_lines[0].strip() == p_lines[-1].strip():
            cont = "\n".join(c_lines[1:])
        joiner = "" if partial.endswith("\n") else "\n"
        res["reply"] = partial + joiner + cont
        res["finish"] = nxt.get("finish", "")
        res["continued"] = rounds
    return res

# --------------------------------------------------------------------------
# WHOLE-CODE ANALYSIS  -- catch clashes the model can't see in its own output
# --------------------------------------------------------------------------
# A model checking its OWN code shares its own blind spots ("correlated error
# modes"), so it can convince itself broken code is fine. An INDEPENDENT analyzer
# breaks that: it reads the file as a whole and flags real problems — undefined
# names, calls with the wrong number of arguments, unused variables, redefinitions,
# unreachable code — BEFORE the tool is ever run. Uses Ruff if it's installed
# (faster, deeper); otherwise falls back to a built-in ast pass so Timmy stays
# zero-dependency and "just works".

def _ruff_path():
    import shutil as _sh
    return _sh.which("ruff")

def analyze_with_ruff(code):
    """Run Ruff's correctness lints (the F/E9 families: undefined names, bad calls,
    unused vars, syntax) and return a list of issue strings. None if Ruff absent."""
    ruff = _ruff_path()
    if not ruff:
        return None
    fd, path = tempfile.mkstemp(prefix="timmy_ruff_", suffix=".py")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)
        # F = pyflakes (undefined names, unused imports/vars, redefinitions, f-string bugs)
        # E9 = syntax/runtime-ish errors. We deliberately skip pure-style rules.
        proc = subprocess.run(
            [ruff, "check", "--select", "F,E9", "--output-format", "json", "--no-cache", path],
            capture_output=True, text=True, timeout=20)
        try:
            items = json.loads(proc.stdout or "[]")
        except Exception:
            return None
        out = []
        for it in items:
            loc = it.get("location") or {}
            ln = loc.get("row")
            code_id = it.get("code") or ""
            msg = it.get("message") or ""
            out.append(f"L{ln} {code_id}: {msg}" if ln else f"{code_id}: {msg}")
        return out
    except Exception:
        return None
    finally:
        try: os.unlink(path)
        except Exception: pass

def autofix_with_ruff(code):
    """The 'lint-and-fix' loop every serious AI coding tool runs (aider, etc.):
    if Ruff is present, silently apply its SAFE auto-fixes to generated code before
    the user ever sees it. Only fixes that cannot change behaviour are applied —
    things like a stray unused variable or a redundant f-string prefix — so the
    model never burns a whole fix-round on trivial mechanical cleanup. Import
    removal (F401) and redefinition rewrites (F811) are deliberately EXCLUDED, as
    those can touch import side-effects or intent. Returns (code, [rule_ids fixed]);
    a no-op returning the code unchanged when Ruff is absent or nothing is fixable."""
    ruff = _ruff_path()
    if not ruff:
        return code, []
    fd, path = tempfile.mkstemp(prefix="timmy_fix_", suffix=".py")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)
        sel = ["--select", "F,E9", "--ignore", "F401,F811", "--no-cache"]
        before = subprocess.run([ruff, "check", *sel, "--output-format", "json", path],
                                capture_output=True, text=True, timeout=20)
        try:
            items = json.loads(before.stdout or "[]")
        except Exception:
            items = []
        fixable = sorted({it.get("code") for it in items
                          if (it.get("fix") or {}).get("applicability") == "safe" and it.get("code")})
        if not fixable:
            return code, []
        subprocess.run([ruff, "check", *sel, "--fix", path], capture_output=True, text=True, timeout=20)
        with open(path) as f:
            fixed = f.read().rstrip()
        # only accept the fix if it still parses (paranoia — ruff safe fixes always do)
        try:
            import ast as _ast; _ast.parse(fixed)
        except SyntaxError:
            return code, []
        return (fixed or code), fixable
    except Exception:
        return code, []
    finally:
        try: os.unlink(path)
        except Exception: pass

def analyze_with_ast(code):
    """Built-in, zero-dependency fallback analyzer. Walks the AST to catch the
    highest-value clashes a model can't see in its own output:
      - use of a name that is bound NOWHERE in the file (typo / hallucinated name)
      - calls to a top-level function with the wrong number of positional args
      - calls to a class's OWN method (self.method(...)) with the wrong arity
      - local variables assigned a side-effect-free value but never used
    Precision over recall: it deliberately over-collects 'bound names' (scope-
    insensitively) so it will essentially never flag a name that is legitimately
    defined somewhere — at the cost of missing a few real bugs. Staying silent on
    correct code matters more here than catching everything, because a false alarm
    makes the model 'fix' code that was already right."""
    import ast, builtins
    issues = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"L{e.lineno} syntax: {e.msg}"]

    # ---- collect EVERY name bound anywhere in the module (scope-insensitive) ----
    # If the file does `from x import *` we can't know what it pulls in, so the
    # undefined-name check is skipped entirely rather than risk false positives.
    star_import = False
    bound = set()       # every name assigned / defined / imported / used as a param

    def _bind_target(t):
        # record names bound by an assignment/loop/with target (incl. tuple unpacking)
        if isinstance(t, ast.Name):
            bound.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                _bind_target(e)
        elif isinstance(t, ast.Starred):
            _bind_target(t.value)
        # attribute/subscript targets (self.x = …, d[k] = …) bind no bare name

    def _bind_args(a):
        for grp in (getattr(a, "posonlyargs", []), a.args, a.kwonlyargs):
            for arg in grp:
                bound.add(arg.arg)
        if a.vararg: bound.add(a.vararg.arg)
        if a.kwarg:  bound.add(a.kwarg.arg)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    star_import = True
                else:
                    bound.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name); _bind_args(node.args)
        elif isinstance(node, ast.Lambda):
            _bind_args(node.args)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _bind_target(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            _bind_target(node.target)
        elif isinstance(node, ast.NamedExpr):                 # walrus  (x := …)
            _bind_target(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _bind_target(node.target)
        elif isinstance(node, ast.comprehension):
            _bind_target(node.target)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                _bind_target(node.optional_vars)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for n in node.names:
                bound.add(n)
        elif node.__class__.__name__ in ("MatchAs", "MatchStar") and getattr(node, "name", None):
            bound.add(node.name)                              # match … as name (3.10+)

    builtin_names = set(dir(builtins)) | {
        "__name__", "__file__", "__doc__", "__builtins__", "__spec__", "__class__",
        "__loader__", "__package__", "__path__", "self", "cls",
    }
    allowed = bound | builtin_names

    # ---- undefined names: a Load-context bare name bound NOWHERE and not built-in ----
    if not star_import:
        seen_undef = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                nm = node.id
                if nm not in allowed and nm not in seen_undef:
                    seen_undef.add(nm)
                    issues.append(f"L{node.lineno} undefined: name '{nm}' is used but never "
                                  f"defined, imported, or built-in (typo or missing definition?)")

    # ---- arity helpers ----
    def _sig(fnnode, drop_first=False):
        a = fnnode.args
        posonly = getattr(a, "posonlyargs", [])
        pos = len(posonly) + len(a.args) - (1 if drop_first else 0)
        ndef = len(a.defaults)
        has_var = a.vararg is not None or a.kwarg is not None or bool(a.kwonlyargs)
        return (max(0, pos - ndef), None if has_var else max(0, pos))

    def _check_call(label, mn, mx, callnode):
        # skip calls using *args/**kwargs — too dynamic to judge
        if any(isinstance(a, ast.Starred) for a in callnode.args) or \
           any(k.arg is None for k in callnode.keywords):
            return
        nargs = len(callnode.args) + len(callnode.keywords)
        ln = getattr(callnode, "lineno", "?")
        if mx is not None and nargs > mx:
            issues.append(f"L{ln} call: {label}() called with {nargs} args but takes at most {mx}")
        elif nargs < mn:
            issues.append(f"L{ln} call: {label}() called with {nargs} args but needs at least {mn}")

    # --- arity: direct calls to an UNDECORATED top-level function by bare name ---
    # (a decorator can change a function's effective signature, so we skip those.)
    func_sigs = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.decorator_list:
            func_sigs[node.name] = _sig(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in func_sigs:
            mn, mx = func_sigs[node.func.id]
            _check_call(node.func.id, mn, mx, node)

    # --- arity: self.method(...) calls vs methods defined in the SAME class ---
    # We know the real signature regardless of base classes, so this is safe even
    # for tools that subclass Gtk.Window / QWidget / tk.Frame. Decorated methods
    # (static/class/property/custom) are skipped — their call shape can differ.
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        methods = {}
        for b in cls.body:
            if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and not b.decorator_list:
                methods[b.name] = _sig(b, drop_first=True)
        for node in ast.walk(cls):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                    and node.func.attr in methods):
                mn, mx = methods[node.func.attr]
                _check_call("self." + node.func.attr, mn, mx, node)

    # --- unused local variables (per-function, conservative) ---
    # GUI code constantly assigns the result of a call for its side effects
    # (building a widget, wiring a signal), so flagging those produces noise. We
    # ONLY flag a variable that is unused AND was assigned a plain literal/name
    # (a value with no side effect) — that's far more likely to be a real mistake.
    class UnusedVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, fn):
            assigned, used, simple = {}, set(), set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    # is the RHS side-effect-free? (literal, name, tuple/list of those)
                    rhs = n.value
                    is_simple = isinstance(rhs, (ast.Constant, ast.Name, ast.Tuple,
                                                 ast.List, ast.Dict, ast.Set))
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            assigned.setdefault(t.id, t.lineno)
                            if is_simple:
                                simple.add(t.id)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    used.add(n.id)
                elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                    used.add(n.target.id)
            for name, ln in assigned.items():
                if name == "_" or name.startswith("_"):
                    continue
                if name not in used and name in simple:
                    issues.append(f"L{ln} unused: local variable '{name}' assigned but never used")
            self.generic_visit(fn)
    UnusedVisitor().visit(tree)

    # --- self.<attr> read but never assigned anywhere in the SAME class ---
    # Runs as a shared helper so it also supplements Ruff (which doesn't catch this).
    if not star_import:
        issues.extend(_unassigned_self_attrs(tree))
    # --- high-confidence quality findings (silent except: pass, shell injection) ---
    issues.extend(_extra_safety_findings(tree))

    # de-dup and cap so we never flood the model
    seen, uniq = set(), []
    for i in issues:
        if i not in seen:
            seen.add(i); uniq.append(i)
    return uniq[:25]

def _unassigned_self_attrs(tree):
    """The #1 runtime crash the import-safe smoke test can NEVER catch: a callback or
    thread reads self.something that no method ever set, so the window opens fine and
    then throws AttributeError the moment the user clicks. Flag only the high-confidence
    case; bail out of a class entirely if it does anything dynamic (setattr/getattr,
    __getattr__/__setattr__) that could create attributes we can't see statically.
    Returns a list of issue strings (possibly empty). Caller handles de-dup."""
    import ast
    out = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        # CRITICAL false-positive guard: a class that subclasses anything (QMainWindow,
        # tk.Frame, QWidget, a project base class, etc.) inherits attributes and methods
        # we cannot see — self.setWindowTitle, self.pack, self.master are all legitimate
        # there. Flagging them would make the model "fix" correct code, the worst outcome.
        # So we ONLY analyze classes with no bases, or whose only base is `object`. That
        # covers plain controller/state classes while staying silent on every widget
        # subclass. (Decorators or keyword bases like metaclass= also mean: skip.)
        bases_ok = all(isinstance(b, ast.Name) and b.id == "object" for b in cls.bases)
        if cls.bases and not bases_ok:
            continue
        if getattr(cls, "keywords", None) or cls.decorator_list:
            continue
        assigned_attrs, read_attrs = set(), {}
        dynamic = False
        # an augmented assignment (self.x += 1) READS self.x before writing it, so a
        # name that ONLY ever appears as an augassign target was never truly initialized.
        # Collect those targets so a typo'd `self.valeu += 1` is caught.
        # v1.2.0 FIX: the old code discarded the attr from assigned_attrs even when a
        # plain `self.x = 0` ALSO existed — so the most common pattern in any game
        # (`self.score = 0` in __init__, `self.score += 1` in play) was falsely flagged
        # as a serious finding, failing correct code and burning auto-fix rounds on
        # non-bugs. Now an aug-assign target only counts as unassigned when NO plain
        # assignment of that attr exists anywhere in the class.
        augained = {}
        _aug_targets = set()
        for n in ast.walk(cls):
            if (isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Attribute)
                    and isinstance(n.target.value, ast.Name) and n.target.value.id == "self"):
                augained.setdefault(n.target.attr, n.target.lineno)
                _aug_targets.add(id(n.target))
        for n in ast.walk(cls):
            if (isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name) and n.value.id == "self"):
                if isinstance(n.ctx, (ast.Store, ast.Del)):
                    if id(n) not in _aug_targets:   # plain assignment only
                        assigned_attrs.add(n.attr)
                elif isinstance(n.ctx, ast.Load):
                    read_attrs.setdefault(n.attr, n.lineno)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in ("setattr", "getattr", "vars"):
                dynamic = True
        # an attr whose ONLY assignment is an augmented one (self.x += …) was never
        # initialized: surface it as a read of an unassigned attr.
        for attr, ln in augained.items():
            if attr not in assigned_attrs:
                read_attrs.setdefault(attr, ln)
        if any(m for m in cls.body
               if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
               and m.name in ("__getattr__", "__setattr__", "__getattribute__")):
            dynamic = True
        if dynamic:
            continue
        for m in cls.body:
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assigned_attrs.add(m.name)
        assigned_attrs |= {"__class__", "__dict__", "__doc__", "__module__"}
        for attr, ln in read_attrs.items():
            if attr not in assigned_attrs:
                out.append(f"L{ln} attribute: self.{attr} is read but never assigned in "
                           f"class '{cls.name}' (AttributeError at runtime — set it in "
                           f"__init__, or fix the name)")
    return out

def _extra_safety_findings(tree):
    """A small set of HIGH-CONFIDENCE quality findings the system prompt explicitly
    forbids, so the model can clean them up. Engine-independent (used by both the Ruff
    and the ast paths). Kept deliberately narrow to avoid flagging correct code:

      1. SILENT FAILURE: a bare `except:` or a broad `except Exception/BaseException:`
         whose body does nothing but `pass` (or `...`). That swallows every error with
         no message — exactly the "it silently did nothing" bug the standards prohibit.
      2. SHELL INJECTION: `subprocess.run/Popen/call/check_output/check_call(..., shell=True)`
         where the command is NOT a constant string (a variable/f-string/concatenation),
         or any `os.system(...)` / `os.popen(...)` with a non-constant argument. Both run
         a string through the shell, so a built-from-input command is an injection risk —
         the standards require a list argv instead.
    Returns a list of issue strings (possibly empty). Caller de-dups."""
    import ast
    out = []
    SHELL_FUNCS = {"run", "Popen", "call", "check_output", "check_call"}
    for n in ast.walk(tree):
        # --- 1. silent except: pass ---
        if isinstance(n, ast.ExceptHandler):
            body = [s for s in n.body if not (isinstance(s, ast.Expr)
                    and isinstance(getattr(s, "value", None), ast.Constant)
                    and isinstance(s.value.value, str))]   # drop a docstring-only line
            only_pass = all(isinstance(s, ast.Pass) for s in body) and len(body) > 0
            if not body:  # body was just a string/ellipsis expression
                only_pass = True
            etype = n.type
            broad = (etype is None
                     or (isinstance(etype, ast.Name) and etype.id in ("Exception", "BaseException")))
            if only_pass and broad:
                ln = getattr(n, "lineno", "?")
                out.append(f"L{ln} silent-failure: a broad 'except: pass' swallows every error "
                           f"with no message (forbidden — surface the failure in the window, or "
                           f"narrow the except and handle it)")
        # --- 2. shell injection ---
        if isinstance(n, ast.Call):
            f = n.func
            # subprocess.<func>(..., shell=True, ...) with non-constant command
            is_subprocess = (isinstance(f, ast.Attribute) and f.attr in SHELL_FUNCS
                             and isinstance(f.value, ast.Name) and f.value.id == "subprocess")
            if is_subprocess:
                shell_true = any(k.arg == "shell" and isinstance(k.value, ast.Constant)
                                 and k.value.value is True for k in n.keywords)
                cmd = n.args[0] if n.args else None
                cmd_const = isinstance(cmd, ast.Constant)
                if shell_true and cmd is not None and not cmd_const:
                    ln = getattr(n, "lineno", "?")
                    out.append(f"L{ln} shell-injection: subprocess.{f.attr}(..., shell=True) with a "
                               f"built command runs it through the shell (injection risk — pass a "
                               f"list argv and drop shell=True)")
            # os.system(x) / os.popen(x) with a non-constant arg
            is_ossys = (isinstance(f, ast.Attribute) and f.attr in ("system", "popen")
                        and isinstance(f.value, ast.Name) and f.value.id == "os")
            if is_ossys:
                cmd = n.args[0] if n.args else None
                if cmd is not None and not isinstance(cmd, ast.Constant):
                    ln = getattr(n, "lineno", "?")
                    out.append(f"L{ln} shell-injection: os.{f.attr}() runs a built string through "
                               f"the shell (injection risk — use subprocess with a list argv)")
    return out

def code_map(code):
    """Build a compact structural map of the current tool: imports, top-level
    functions (with signatures), and classes (with their methods). Given to the
    model before it edits, so it sees the file's shape at a glance and stops
    re-introducing bugs it already fixed or calling things that don't exist.
    Returns a short string, or '' if the code doesn't parse."""
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""

    def sig(fn):
        a = fn.args
        parts = []
        posonly = getattr(a, "posonlyargs", [])
        allpos = posonly + a.args
        ndef = len(a.defaults)
        first_def = len(allpos) - ndef
        for i, arg in enumerate(allpos):
            parts.append(arg.arg + ("=…" if i >= first_def else ""))
        if a.vararg: parts.append("*" + a.vararg.arg)
        for kw in a.kwonlyargs: parts.append(kw.arg + "=…")
        if a.kwarg: parts.append("**" + a.kwarg.arg)
        return f"{fn.name}({', '.join(parts)})"

    imports, funcs, classes = [], [], []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports += [a.asname or a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imports += [f"{mod}.{a.name}" for a in node.names]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(sig(node))
        elif isinstance(node, ast.ClassDef):
            methods = [sig(b) for b in node.body
                       if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            head = node.name + (f"({', '.join(bases)})" if bases else "")
            classes.append((head, methods))

    lines = ["STRUCTURE OF THE CURRENT TOOL (for your reference — keep calls consistent with this):"]
    if imports:
        lines.append("imports: " + ", ".join(imports[:30]))
    for head, methods in classes:
        lines.append(f"class {head}:")
        for m in methods:
            lines.append(f"    {m}")
    if funcs:
        lines.append("functions: " + "; ".join(funcs))
    return "\n".join(lines)

def analyze_code(code):
    """Whole-code clash analysis. Prefers Ruff, falls back to the ast pass.
    Returns {"issues": [...], "engine": "ruff"|"ast", "clean": bool}."""
    ruff_issues = analyze_with_ruff(code)
    if ruff_issues is not None:
        # Ruff is fast and deep on style/logic but does NOT track instance attributes.
        # Supplement it with our high-confidence self.<attr>-never-assigned pass plus the
        # extra safety findings (silent except: pass, shell injection) so those are caught
        # regardless of which engine runs.
        supplemental = []
        try:
            import ast as _ast
            tree = _ast.parse(code)
            if not any(isinstance(n, _ast.ImportFrom) and any(a.name == "*" for a in n.names)
                       for n in _ast.walk(tree)):
                supplemental = _unassigned_self_attrs(tree)
            supplemental = supplemental + _extra_safety_findings(tree)
        except SyntaxError:
            pass
        merged = ruff_issues + [s for s in supplemental if s not in ruff_issues]
        return {"issues": merged, "engine": "ruff", "clean": not merged}
    ast_issues = analyze_with_ast(code)
    return {"issues": ast_issues, "engine": "ast", "clean": not ast_issues}

# v1.2.0 — THE RUNTIME PLAYTEST. The old smoke test only IMPORTED the game, so every
# bug that bites after the window opens (a NameError on frame 30, a crash when the
# first enemy spawns, a loop that ignores QUIT) sailed straight through to the user.
# This harness actually RUNS the game in a subprocess under SDL's dummy video/audio
# drivers (fully headless — no window ever appears), pilots it with synthetic key /
# mouse / finger input for a few seconds, then posts QUIT and requires a clean exit.
# Exit codes: 0 ok · 6 exited instantly (no real loop) · 7 crashed (traceback on
# stderr) · 8 never opened a window · 9 ignored pygame.QUIT. Timeout = hung loop.
RUNTIME_HARNESS = r'''
import os, sys, threading, time, runpy, traceback, warnings
warnings.filterwarnings("ignore")
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["TIMMY_RUNTIME_TEST"] = "1"
TARGET = sys.argv[1]
PLAY = float(sys.argv[2]) if len(sys.argv) > 2 else 7.0
T0 = time.time()
STATE = {"quit_posted": False}

def pilot():
    try:
        import pygame
    except Exception:
        return
    t0 = time.time()
    while time.time() - t0 < 12.0:
        try:
            if pygame.get_init() and pygame.display.get_surface():
                break
        except Exception:
            pass
        time.sleep(0.05)
    else:
        sys.stderr.write("TIMMY_RT: the game never opened a window / started its loop within 12s\n")
        os._exit(8)
    time.sleep(0.7)
    keys = [pygame.K_RETURN, pygame.K_SPACE, pygame.K_RIGHT, pygame.K_d,
            pygame.K_UP, pygame.K_w, pygame.K_SPACE, pygame.K_LEFT,
            pygame.K_a, pygame.K_DOWN, pygame.K_s, pygame.K_RETURN]
    end, i = time.time() + PLAY, 0
    while time.time() < end:
        k = keys[i % len(keys)]; i += 1
        try:
            pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=k, mod=0, unicode=""))
            time.sleep(0.05)
            pygame.event.post(pygame.event.Event(pygame.KEYUP, key=k, mod=0))
            surf = pygame.display.get_surface()
            if surf and i % 3 == 0:
                w, h = surf.get_size()
                pos = (w // 2, int(h * 0.7))
                pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
                pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1))
                pygame.event.post(pygame.event.Event(pygame.FINGERDOWN, touch_id=1, finger_id=1,
                                                     x=0.5, y=0.7, dx=0.0, dy=0.0, pressure=1.0))
                pygame.event.post(pygame.event.Event(pygame.FINGERUP, touch_id=1, finger_id=1,
                                                     x=0.5, y=0.7, dx=0.0, dy=0.0, pressure=0.0))
        except Exception:
            pass
        time.sleep(0.10)
    STATE["quit_posted"] = True
    try:
        pygame.event.post(pygame.event.Event(pygame.QUIT))
    except Exception:
        pass
    time.sleep(3.0)
    sys.stderr.write("TIMMY_RT: the game ignored pygame.QUIT - the main loop never exits\n")
    os._exit(9)

threading.Thread(target=pilot, daemon=True).start()
try:
    runpy.run_path(TARGET, run_name="__main__")
except SystemExit:
    pass
except BaseException:
    sys.stderr.write(traceback.format_exc())
    sys.exit(7)
if not STATE["quit_posted"] and time.time() - T0 < max(2.0, PLAY * 0.5):
    sys.stderr.write("TIMMY_RT: the game exited almost immediately - no real game loop ran\n")
    sys.exit(6)
'''

_RT_NOISE = ("no fast renderer", "pygame-ce", "pygame 2", "Hello from the pygame",
             "ALSA", "aplay", "dummy", "libGL", "XDG_RUNTIME_DIR", "Warning:")

def _rt_tail(blob, limit=1400):
    """Last `limit` chars of harness output with known dummy-driver noise dropped.
    When a traceback is present, snap to the LAST one so the model gets pure signal."""
    blob = blob or ""
    marker = "Traceback (most recent call last):"
    if marker in blob:
        blob = blob[blob.rfind(marker):]
    lines = [l for l in blob.splitlines()
             if l.strip() and not any(n in l for n in _RT_NOISE)]
    return "\n".join(lines)[-limit:]

def runtime_test(code):
    """Run a pygame game headlessly under the pilot harness. Returns (ok, note).
    ok=True with a note when the test was skipped (e.g. pygame not installed on
    this box yet) — only a real in-game failure fails the check."""
    vpy = run_python()
    try:
        probe = subprocess.run([vpy, "-c", "import pygame"],
                               capture_output=True, timeout=30)
        if probe.returncode != 0:
            return True, ("skipped — pygame isn't installed in Timmy's venv yet "
                          "(use the deps button); the import check still ran")
    except Exception:
        return True, "skipped — couldn't probe for pygame on this box"
    tmpdir = tempfile.mkdtemp(prefix="timmy_rt_")
    game = os.path.join(tmpdir, "game.py")
    harness = os.path.join(tmpdir, "_timmy_rt.py")
    try:
        with open(game, "w", encoding="utf-8") as f:
            f.write(code)
        with open(harness, "w", encoding="utf-8") as f:
            f.write(RUNTIME_HARNESS)
        env = dict(os.environ)
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        try:
            proc = subprocess.run([vpy, harness, game, str(RUNTIME_PLAY_SECONDS)],
                                  capture_output=True, stdin=subprocess.DEVNULL,
                                  timeout=RUNTIME_TIMEOUT, env=env, cwd=tmpdir)
        except subprocess.TimeoutExpired:
            return False, ("the game HUNG during the headless playtest — the main loop blocked "
                           "or never exits (a while-loop without event handling, a blocking "
                           "input()/sleep, or QUIT not honoured).")
        err = proc.stderr.decode("utf-8", errors="replace")
        tail = _rt_tail(err)
        rc = proc.returncode
        if rc == 0:
            return True, f"played {RUNTIME_PLAY_SECONDS}s headlessly with synthetic input + clean quit"
        if rc == 6:
            return False, ("the game exited almost immediately — there's no real game loop "
                           "running.\n" + tail)
        if rc == 7:
            return False, ("the game CRASHED during the headless playtest. Traceback:\n" + tail)
        if rc == 8:
            return False, ("the game never opened a window / started its main loop within 12s — "
                           "is the setup stuck before the loop?\n" + tail)
        if rc == 9:
            return False, ("the game ignores pygame.QUIT — the window can never be closed. Handle "
                           "the QUIT event in the main loop.\n" + tail)
        return False, f"the headless playtest failed (exit {rc}).\n{tail}"
    finally:
        try: shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception: pass

def smoke_test(code):
    """Silent quality checks on generated code. Returns (passed, report, checks).
    IMPORTANT: this only checks that the code PARSES and IMPORTS cleanly. It does
    NOT open the window — doing that needs a display and would block. For GUI tools
    it also verifies the code is import-safe (no window opens at import time) and is
    TOLERANT of a headless/toolkit-less test box: a missing display or missing GUI
    typelib is an environment fact here, not a bug in the generated tool. Real
    behaviour is verified by the user pressing Run on their own machine."""
    checks = []
    # 1. syntax
    try:
        import ast
        ast.parse(code)
        checks.append(("syntax", True, ""))
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}", [("syntax", False, str(e))]

    tk = detect_toolkit(code)

    # 1b. import-safety for GUI tools: building/running the GUI must be guarded by
    #     `if __name__ == "__main__":` (or a main() called only there), so importing
    #     the module doesn't try to open a window. Catch the obvious mistake of a
    #     top-level mainloop/run/show call.
    if tk:
        # An optional assignment target is allowed before the dangerous call, so we
        # catch the common form `screen = pygame.display.set_mode(...)` and not just a
        # bare top-level call. `[\w\.\, \t]+=` matches simple/dotted/tuple targets
        # (screen =, self.screen =, a, b =) without crossing a newline.
        bad = re.search(r"^\s*(?:[\w\.\, \t]+=\s*)?(?:Gtk\.main\(\)|app\.run\(|"
                        r"window\.show_all\(\)|\w+\.mainloop\(\)|sys\.exit\(\s*app\.exec|"
                        r"pygame\.display\.set_mode\(|pygame\.display\.flip\(\)|"
                        r"arcade\.run\(|arcade\.open_window\(|\w+\.run\(\)\s*#\s*arcade)",
                        code, re.M)
        if bad and "__main__" not in code:
            msg = ("game isn't import-safe: it opens the window / runs the loop at module top "
                   "level. Move all setup and the main loop inside "
                   "`if __name__ == \"__main__\":`.")
            checks.append(("import-safe", False, msg))
            return False, msg, checks
        checks.append(("import-safe", True, ""))

    # 2. import-ability: load the module WITHOUT running its __main__ block.
    fd, path = tempfile.mkstemp(prefix="timmy_test_", suffix=".py")
    # signatures meaning "this box just can't load the GUI" — never a code bug
    ENV_SIGNS = ("Namespace", "not available", "cannot open display", "could not open display",
                 "couldn't connect to display", "no display name", "Unable to init server",
                 "Gtk couldn't be initialized", "GtkInitError", "QXcbConnection",
                 "qt.qpa.plugin", "no Qt platform plugin", "xcb", "DISPLAY",
                 "_tkinter.TclError", "libGL", "Gdk",
                 # pygame / SDL on a headless test box: no video device is an env fact
                 "No available video device", "video system not initialized",
                 "video device", "pygame.error", "SDL", "ALSA", "No such audio device")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)
        harness = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('timmy_candidate', {path!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "try:\n"
            "    spec.loader.exec_module(mod)\n"
            "except (ModuleNotFoundError, ImportError) as e:\n"
            "    print('DEP_MISSING:' + str(e)); sys.exit(0)\n"
            "except SystemExit as e:\n"
            "    print('TOOLKIT_EXIT:' + str(e)); sys.exit(0)\n"
            "except BaseException as e:\n"
            "    import traceback; tb = traceback.format_exc()\n"
            "    sys.stderr.write(tb)\n"
            "    sys.exit(7)\n"
        )
        try:
            proc = subprocess.run([run_python(), "-c", harness],
                                  capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
            out = proc.stdout.decode("utf-8", errors="replace")
            err = proc.stderr.decode("utf-8", errors="replace")
            blob = out + "\n" + err
            if out.startswith("DEP_MISSING:"):
                note = "needs a package (use the deps button)"
                if tk:
                    hint = tk.get("apt_hint") or tk.get("pip") or "pip install"
                    note = f"needs the {tk['label']} toolkit — {hint}"
                checks.append(("imports", True, note))
            elif out.startswith("TOOLKIT_EXIT:") or (tk and any(s in blob for s in ENV_SIGNS)):
                # the tool bailed gracefully because the toolkit/display isn't on THIS box,
                # or hit an environment-only error. Structurally fine.
                checks.append(("imports", True, "toolkit/display not present on the test box "
                                                 "(expected — runs on the user's own machine)"))
            elif proc.returncode != 0:
                # a genuine error at import/definition time (NameError, bad default, etc.)
                msg = err.strip()[-500:] or "import failed"
                checks.append(("imports", False, msg))
                return False, msg, checks
            else:
                checks.append(("imports", True, ""))
        except subprocess.TimeoutExpired:
            checks.append(("imports", False, "import timed out (top-level code is blocking — "
                                             "is a window opening at import time?)"))
            return False, "Import timed out — there may be blocking/GUI code at module top level.", checks

        # 3. whole-code analysis: catch clashes the model can't see in its own output
        #    (undefined names, wrong-arity calls, unused vars). Independent of the model.
        analysis = analyze_code(code)
        if analysis["clean"]:
            checks.append(("analysis", True, f"{analysis['engine']}: no issues"))
        else:
            # Treat these as fixable findings: report them so the autotest loop can
            # feed them back, but they don't, by themselves, "fail" a tool that imports
            # fine — some ast findings (e.g. an unused var) are minor. We surface them
            # and let the loop decide. Genuine correctness issues (undefined name, bad
            # call) are worth a fix round.
            serious = [i for i in analysis["issues"]
                       if any(k in i for k in ("undefined", "call:", "attribute:", "F821", "F811",
                                               "F706", "F702", "E9", "syntax"))]
            report = (f"Whole-code analysis ({analysis['engine']}) found:\n  - "
                      + "\n  - ".join(analysis["issues"]))
            if serious:
                checks.append(("analysis", False, report))
                return False, report, checks
            else:
                # only minor findings (e.g. unused vars) — note them, still pass
                checks.append(("analysis", True, f"{analysis['engine']}: minor only — " +
                               "; ".join(analysis["issues"][:5])))
        # 4. RUNTIME PLAYTEST (v1.2.0, pygame games): actually run it headlessly,
        #    pilot it with synthetic input, and require a clean exit on QUIT. The
        #    check the 1.1.x pipeline was missing — import-clean games that crash
        #    on frame 30 stop here instead of in the user's lap.
        if RUNTIME_TEST and tk and tk.get("module") == "pygame":
            ok_rt, note_rt = runtime_test(code)
            checks.append(("runtime", ok_rt, note_rt))
            if not ok_rt:
                return False, "Runtime playtest failed: " + note_rt, checks
        return True, "", checks
    finally:
        try: os.unlink(path)
        except Exception: pass

def _latest_code_in(convo):
    """Find the most recent code block in a conversation (the current tool)."""
    for m in reversed(convo):
        if m.get("role") == "assistant":
            c = extract_code(m.get("content", ""))
            if c:
                return c
    return None

def chat_with_autotest(messages, provider_id=None):
    """Call the model, then silently smoke-test any code it returns, feeding
    failures back for up to AUTOTEST_MAX_ROUNDS before returning to the user."""
    convo = list(messages)

    # FILE MAP (feature #3): if there's already a tool in this conversation and the
    # user is asking for a change, give the model a compact structural map of the
    # current code right before it edits — so it keeps calls consistent with what
    # actually exists and stops re-introducing bugs. Injected as a transient system
    # note (not persisted into the saved conversation).
    existing = _latest_code_in(convo)
    if existing:
        cmap = code_map(existing)
        if cmap:
            # place the map just before the final user turn so it's freshest in context
            insert_at = len(convo)
            for i in range(len(convo) - 1, -1, -1):
                if convo[i].get("role") == "user":
                    insert_at = i
                    break
            convo = convo[:insert_at] + [{"role": "system", "content": cmap}] + convo[insert_at:]

    # DESIGN PASS (v1.2.0): on a FRESH build only, distil the request + intake
    # answers into an authoritative spec, add matching genre craft notes, and pin
    # it right before the final user turn. The code pass then implements a concrete
    # design instead of juggling design and 900 lines of code in one breath.
    spec = None
    if not existing:
        spec = make_spec(convo, provider_id)
        if spec:
            note = ("GAME DESIGN SPEC — authoritative. Implement EXACTLY this (every line), "
                    "then push the polish further:\n" + spec)
            hints = genre_hints(spec)
            if hints:
                note += "\n\n" + hints
            insert_at = len(convo)
            for i in range(len(convo) - 1, -1, -1):
                if convo[i].get("role") == "user":
                    insert_at = i
                    break
            convo = convo[:insert_at] + [{"role": "system", "content": note}] + convo[insert_at:]

    rounds = []
    # Lower temperature on code generation: more deterministic, fewer hallucinated
    # APIs and careless slips. Reasoning paths (intake/review) keep the default 0.3.
    res = _call_with_continue(convo, provider_id, temperature=BUILD_TEMPERATURE)
    if res.get("error"):
        return res

    for attempt in range(AUTOTEST_MAX_ROUNDS + 1):
        code = extract_code(res.get("reply", ""))
        if not code:
            res["autotest"] = {"ran": False, "rounds": rounds}
            # No code means the model spoke or asked rather than built. If it asked the
            # user something, structure those questions into tappable options so the user
            # can answer with a click — the opening-intake experience, on every turn.
            res["followup"] = structure_followup(res.get("reply", ""), convo, provider_id)
            return res
        # lint-and-fix loop: silently apply Ruff's SAFE mechanical fixes so trivial
        # cleanup (a stray unused var, a redundant f-string prefix) never costs a fix
        # round. Behaviour-affecting fixes are excluded; see autofix_with_ruff().
        fixed, applied = autofix_with_ruff(code)
        if applied and fixed != code:
            res["reply"] = replace_first_code_block(res.get("reply", ""), fixed)
            code = fixed
        passed, report, checks = smoke_test(code)
        # also surface any non-fatal analysis notes (minor findings) for visibility
        minor = [note for name, ok, note in checks if name == "analysis" and ok and note
                 and ("minor only" in note)]
        rounds.append({"attempt": attempt + 1, "passed": passed,
                       "checks": [c[0] for c in checks if c[1]],
                       "failed": [c[0] for c in checks if not c[1]],
                       "report": "" if passed else report,
                       "autofixed": applied,
                       "minor": minor})
        if passed or attempt == AUTOTEST_MAX_ROUNDS:
            res["autotest"] = {"ran": True, "passed": passed, "rounds": rounds}
            if spec:
                res["spec"] = spec
            # QUALITY GATE (v1.2.0): fresh builds that pass everything get one
            # playtest-critique round; the improved code is kept only if it ALSO
            # passes every check. Never runs on the user's own iteration requests.
            if passed and spec and QUALITY_PASS:
                res = _quality_gate(res, convo, provider_id, spec)
            return res
        # FEED THE FAILURE BACK with a structural map so the fix is informed, not blind.
        # Giving the model a map of its own code + the exact analyzer findings produces a
        # far better fix than just "it failed, try again" (the agentic-loop pattern).
        cmap = code_map(code)
        fix_msg = (f"Your code failed an automatic quality check before I saw it. "
                   f"Fix the SPECIFIC problems below and return the FULL corrected script "
                   f"(one ```python block, nothing omitted).\n\n"
                   f"=== problems found ===\n{report}\n")
        if cmap:
            fix_msg += f"\n=== structure of the code you just wrote (keep calls consistent) ===\n{cmap}\n"
        fix_msg += ("\nDo not introduce new problems. Re-check that every function is called with "
                    "the right arguments and every name is defined before use. Quick pass on the "
                    "usual game traps: the kit block is byte-identical and untouched; every Scene "
                    "the code switches to exists and takes (self, kit); motion uses dt; the loop "
                    "never blocks (no input()/time.sleep in the play path); tile/grid indexing is "
                    "bounds-checked; sounds use only the kit's names; no invented pygame APIs; no "
                    "bare except: pass swallowing real errors.")
        convo = convo + [
            {"role": "assistant", "content": res["reply"]},
            {"role": "user", "content": fix_msg},
        ]
        nxt = _call_with_continue(convo, provider_id, temperature=BUILD_TEMPERATURE)
        if nxt.get("error"):
            res["autotest"] = {"ran": True, "passed": False, "rounds": rounds,
                               "note": "auto-fix call failed: " + nxt["error"]}
            return res
        res = nxt

def _quality_gate(res, convo, provider_id, spec):
    """v1.2.0 — one playtest-critique pass on a fresh, fully passing build. If the
    critique finds genuine must-fix gaps, run ONE improvement round and keep the
    result only if it passes every check again; otherwise keep the passing build."""
    code = extract_code(res.get("reply", ""))
    if not code:
        return res
    critique = call_model([
        {"role": "system", "content": PLAYTEST_PROMPT},
        {"role": "user", "content": f"=== SPEC ===\n{spec}\n\n=== CODE ===\n```python\n{code}\n```"},
    ], provider_id, max_tokens=1200)
    if critique.get("error"):
        return res
    parsed = _parse_json_reply(critique.get("reply", "")) or {}
    must = [m for m in parsed.get("must_fix", []) if isinstance(m, dict) and m.get("title")][:4]
    if not must:
        res["playtest"] = {"clean": True}
        return res
    titles = [str(m["title"]) for m in must]
    items = "\n".join(f"- {m['title']}: {m.get('detail', '')}" for m in must)
    fix_msg = ("A playtest review of your game found genuine playability gaps. Fix ALL of them and "
               "return the FULL corrected script (one ```python block, nothing omitted, the kit "
               "block byte-identical):\n\n" + items)
    convo2 = list(convo) + [{"role": "assistant", "content": res["reply"]},
                            {"role": "user", "content": fix_msg}]
    nxt = _call_with_continue(convo2, provider_id, temperature=BUILD_TEMPERATURE)
    if nxt.get("error"):
        res["playtest"] = {"applied": False, "items": titles, "note": nxt["error"]}
        return res
    new_code = extract_code(nxt.get("reply", ""))
    if not new_code:
        res["playtest"] = {"applied": False, "items": titles,
                           "note": "improvement reply had no code — kept the passing build"}
        return res
    fixed, _ = autofix_with_ruff(new_code)
    if fixed != new_code:
        nxt["reply"] = replace_first_code_block(nxt.get("reply", ""), fixed)
        new_code = fixed
    ok2, report2, _checks2 = smoke_test(new_code)
    if ok2:
        nxt["autotest"] = res.get("autotest")
        nxt["spec"] = spec
        nxt["playtest"] = {"applied": True, "items": titles}
        return nxt
    res["playtest"] = {"applied": False, "items": titles,
                       "note": "the improved version failed checks — kept the version that passes"}
    return res

def review_code(code, provider_id=None):
    """Feature #2 — the 'review my code' button. Runs the independent static analyzer,
    then asks the model for a focused critique (diagnose, don't rewrite). Returns a
    structured report the UI renders. Never modifies the code."""
    if not code or not code.strip():
        return {"error": "There's no code to review yet."}
    # 1. independent static analysis first — concrete, model-blind findings
    analysis = analyze_code(code)
    analyzer_block = ("Automated static analysis: no issues found."
                      if analysis["clean"]
                      else "Automated static analysis (" + analysis["engine"] + ") found:\n- "
                           + "\n- ".join(analysis["issues"]))
    # 2. ask the model to review, given the code + the analyzer's findings
    res = call_model([
        {"role": "system", "content": REVIEW_PROMPT},
        {"role": "user", "content":
            f"Here is the tool to review:\n```python\n{code}\n```\n\n{analyzer_block}"},
    ], provider_id)
    if res.get("error"):
        return res
    parsed = _parse_json_reply(res.get("reply", ""))
    if not parsed:
        # graceful fallback: hand back the analyzer findings even if the model's
        # JSON didn't parse, so the button still does something useful.
        return {"verdict": "Automated checks only (model review unavailable).",
                "issues": [{"severity": "medium", "title": i.split(":")[0] if ":" in i else "issue",
                            "detail": i, "line": None} for i in analysis["issues"]],
                "strengths": [], "engine": analysis["engine"],
                "model": res.get("model")}
    parsed["engine"] = analysis["engine"]
    parsed["model"] = res.get("model")
    # make sure the concrete analyzer findings aren't lost if the model overlooked them
    if not analysis["clean"]:
        parsed.setdefault("analyzer_findings", analysis["issues"])
    return parsed

def _parse_json_reply(reply):
    """Extract a JSON object from a model reply, tolerating fences/prose."""
    reply = re.sub(r"```(?:json)?", "", reply).strip()
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def make_intake(request, provider_id=None):
    """Ask the model for a tailored, clickable question set for a tool request."""
    res = call_model([{"role": "system", "content": INTAKE_PROMPT},
                      {"role": "user", "content": request}], provider_id,
                     temperature=DESIGN_TEMPERATURE)
    if res.get("error"):
        return res
    parsed = _parse_json_reply(res.get("reply", ""))
    if not parsed or "questions" not in parsed:
        # graceful fallback: no intake, just proceed to build
        return {"intake": None}
    # sanitise
    qs = []
    for q in parsed.get("questions", [])[:6]:
        opts = [str(o) for o in q.get("options", [])][:4]
        if q.get("q") and len(opts) >= 2:
            qs.append({"q": str(q["q"]), "options": opts, "multi": bool(q.get("multi"))})
    return {"intake": {"summary": parsed.get("summary", ""), "questions": qs}}

def make_spec(convo, provider_id=None):
    """v1.2.0 — the DESIGN PASS. Distil the user's request + intake answers into a
    compact authoritative spec for the code pass. Returns the spec text or None;
    never blocks a build (any failure just skips the spec)."""
    user_blob = "\n".join((m.get("content") or "") for m in (convo or [])
                          if m.get("role") == "user").strip()[-4000:]
    if not user_blob:
        return None
    res = call_model([{"role": "system", "content": SPEC_PROMPT},
                      {"role": "user", "content": user_blob}], provider_id,
                     temperature=DESIGN_TEMPERATURE, max_tokens=1024)
    if res.get("error"):
        return None
    spec = re.sub(r"```[a-zA-Z]*", "", res.get("reply", "") or "").strip()
    # sanity: a real spec has the labelled lines; garbage gets dropped silently
    if "TITLE:" not in spec or "WIN/LOSE:" not in spec or len(spec) < 120:
        return None
    return spec[:2600]

def structure_followup(reply, convo, provider_id=None):
    """When the model's reply contained NO code, it usually means it asked the user
    something rather than building. Turn those questions into the same tappable
    options the opening intake uses, so the user can answer with a click every time —
    not just on the first message. Returns {"questions": [...]} (possibly empty).
    Cheap-gated: if the reply has no '?' it can't be asking, so we skip the model
    call entirely and return no questions."""
    text = (reply or "").strip()
    if not text or "?" not in text:
        return {"questions": []}
    # a little context keeps the generated options concrete: the user's most recent ask
    last_user = ""
    for m in reversed(convo or []):
        if m.get("role") == "user":
            last_user = (m.get("content") or "")[:600]
            break
    user_blob = (f"For context, the user's last message was:\n{last_user}\n\n" if last_user else "")
    res = call_model([
        {"role": "system", "content": FOLLOWUP_PROMPT},
        {"role": "user", "content":
            user_blob + "The assistant's message to turn into options:\n" + text[:2500]},
    ], provider_id, temperature=DESIGN_TEMPERATURE)
    if res.get("error"):
        return {"questions": []}   # never block the build on the optional helper failing
    parsed = _parse_json_reply(res.get("reply", "")) or {}
    qs = []
    for q in parsed.get("questions", [])[:6]:
        opts = [str(o) for o in q.get("options", [])][:4]
        if q.get("q") and len(opts) >= 2:
            qs.append({"q": str(q["q"]), "options": opts, "multi": bool(q.get("multi"))})
    return {"questions": qs}

def make_github(code, details, provider_id=None):
    """Generate README/.gitignore/requirements from the final code + repo details."""
    user = details.get("username", "USER")
    repo = details.get("repo", "tool")
    branch = details.get("branch", "main")
    license_name = details.get("license", "MIT")
    detail_blob = (f"username: {user}\nrepo: {repo}\nbranch: {branch}\n"
                   f"license: {license_name}\nclone over HTTPS only (never ssh).\n"
                   f"raw base: https://raw.githubusercontent.com/{user}/{repo}/{branch}/")
    res = call_model([{"role": "system", "content": GITHUB_PROMPT},
                      {"role": "user", "content":
                       f"Repo details:\n{detail_blob}\n\n=== FINAL CODE ===\n```python\n{code}\n```"}],
                     provider_id)
    if res.get("error"):
        return res
    parsed = _parse_json_reply(res.get("reply", "")) or {}
    return {"github": parsed, "details": details}

# Live GUI processes launched by Run, so we can report status and stop them.
# {pid: {"proc": Popen, "name": str, "path": tmpfile, "started": ts}}
RUNNING = {}
_RUNNING_LOCK = threading.Lock()

def _reap():
    """Drop finished processes and clean up their temp files."""
    with _RUNNING_LOCK:
        for pid in list(RUNNING):
            info = RUNNING[pid]
            if info["proc"].poll() is not None:
                try: os.unlink(info["path"])
                except Exception: pass
                RUNNING.pop(pid, None)

def list_running():
    _reap()
    with _RUNNING_LOCK:
        return {"running": [{"pid": pid, "name": i["name"],
                             "seconds": round(time.time() - i["started"], 1)}
                            for pid, i in RUNNING.items()]}

def stop_running(pid):
    """Terminate a launched GUI (and its children) — cross-platform.
    POSIX: signal the whole process group (we made one with start_new_session=True).
    Windows: taskkill /F /T does the equivalent — terminate the tree."""
    _reap()
    with _RUNNING_LOCK:
        info = RUNNING.get(pid)
    if not info:
        return {"ok": False, "error": "not running (already closed?)"}
    proc = info["proc"]
    try:
        if IS_WIN:
            # taskkill /T = terminate the entire tree, /F = forceful
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            except Exception:
                try: proc.terminate()
                except Exception: pass
                try: proc.kill()
                except Exception: pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try: proc.wait(timeout=3)
            except Exception:
                try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception: proc.kill()
        return {"ok": True, "pid": pid}
    finally:
        _reap()

def run_code(code, args, confirmed, name="tool"):
    danger = looks_dangerous(code)
    if danger and not confirmed:
        return {"needsConfirm": True, "patterns": danger}

    # parse args the way a shell would (handles quotes/spaces), not naive split
    try:
        argv = shlex.split(args) if args else []
    except ValueError as e:
        return {"stdout": "", "stderr": f"Couldn't parse arguments: {e}", "exit": -1, "seconds": 0}

    tk = detect_toolkit(code)
    interp = run_python(code)

    # unique temp file per run so concurrent/rapid runs can't clobber each other.
    # GUI launches keep their file alive until the window closes (cleaned up by _reap).
    fd, path = tempfile.mkstemp(prefix="timmy_", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(code)

    # ----- GUI tool: LAUNCH it (don't block on the window) -----------------
    if tk:
        _reap()
        # peek at the first ~1.8s of stderr to catch immediate failures
        # (missing toolkit, missing display, a crash on startup), then leave it running.
        try:
            errf = tempfile.NamedTemporaryFile(prefix="timmy_err_", suffix=".log", delete=False)
            t0 = time.time()
            # process-group setup so we can cleanly terminate the whole tree later:
            #   POSIX  -> start_new_session=True  (so killpg(getpgid(pid), SIG) works)
            #   Windows -> CREATE_NEW_PROCESS_GROUP (so taskkill /T can find children)
            popen_kw = dict(
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errf,
            )
            if IS_WIN:
                popen_kw["creationflags"] = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            else:
                popen_kw["start_new_session"] = True
            proc = subprocess.Popen([interp, path] + argv, **popen_kw)
        except Exception as e:
            try: os.unlink(path)
            except Exception: pass
            return {"stdout": "", "stderr": f"Could not launch: {e}", "exit": -1, "seconds": 0}

        time.sleep(1.8)
        rc = proc.poll()
        try:
            errf.flush(); errf.close()
            with open(errf.name, "rb") as ef:
                early_err = ef.read().decode("utf-8", errors="replace")
        except Exception:
            early_err = ""
        finally:
            try: os.unlink(errf.name)
            except Exception: pass

        if rc is not None and rc != 0:
            # died on startup — diagnose toolkit / display problems precisely
            hint = ""
            if tk and ("ModuleNotFoundError" in early_err or "ImportError" in early_err
                       or "No module named" in early_err):
                if tk.get("pip"):
                    hint = (f"\n[Timmy] The {tk['label']} toolkit isn't installed. Install it:\n"
                            f"  pip install {tk['pip']}\n"
                            f"(or click the ⬇ deps button, which does it for you).")
                elif tk["module"] == "tkinter" and IS_LINUX:
                    hint = ("\n[Timmy] Tkinter is split out from Python on Debian-based distros "
                            "(including Kali). Install it:\n"
                            "  sudo apt install python3-tk     (Kali / Debian / Ubuntu / Mint)\n"
                            "  sudo dnf install python3-tkinter (Fedora)")
                else:
                    hint = "\n[Timmy] A required module is missing — see the traceback above."
            elif any(s in early_err for s in ("cannot open display", "no display name",
                      "Unable to init server", "QXcbConnection", "no Qt platform plugin",
                      "could not open display", "couldn't connect to display", "DISPLAY")):
                hint = ("\n[Timmy] The game couldn't open a window — no display is available. "
                        "Launch Timmy from inside a real desktop session (KDE on the X395, or "
                        "Phosh on the OnePlus 6), not over a plain SSH shell. The game itself looks fine.")
            elif any(s in early_err for s in ("No available video device", "video system not initialized",
                      "pygame.error", "Failed to initialize", "wayland", "Wayland")):
                hint = ("\n[Timmy] pygame/SDL couldn't initialise a video device. Run from inside "
                        "a real graphical session. Under Phosh/Wayland on the OnePlus 6, SDL uses "
                        "Wayland by default; if a window still won't open you can try the X11 path "
                        "with  SDL_VIDEODRIVER=x11  in front of the launch — but the game code itself "
                        "should not hardcode a driver. The game itself looks fine.")
            try: os.unlink(path)
            except Exception: pass
            return {"stdout": "", "stderr": (early_err or "the GUI exited immediately") + hint,
                    "exit": rc, "seconds": round(time.time() - t0, 2), "gui": True}

        if rc is not None and rc == 0:
            # opened and closed cleanly within the peek window (or it's a one-shot)
            try: os.unlink(path)
            except Exception: pass
            return {"stdout": "", "stderr": early_err, "exit": 0,
                    "seconds": round(time.time() - t0, 2), "gui": True, "launched": False,
                    "note": "ran and exited cleanly"}

        # still running -> success: the window is open on the user's screen
        with _RUNNING_LOCK:
            RUNNING[proc.pid] = {"proc": proc, "name": name or "tool", "path": path, "started": t0}
        return {"stdout": "", "stderr": early_err, "exit": 0,
                "seconds": round(time.time() - t0, 2), "gui": True, "launched": True,
                "pid": proc.pid,
                "note": f"{tk['label']} window launched (pid {proc.pid}). It's open on your "
                        f"desktop — interact with it there. Use ■ stop to close it."}

    # ----- non-GUI fallback (rare now): capture output as before ----------
    try:
        t0 = time.time()
        try:
            proc = subprocess.run(
                [interp, path] + argv,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=120)
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "Killed: exceeded 120s (possible infinite loop, "
                    "or the tool was waiting for input — Timmy provides none).",
                    "exit": -1, "seconds": round(time.time() - t0, 2)}
        except Exception as e:
            return {"stdout": "", "stderr": f"Could not launch: {e}", "exit": -1, "seconds": 0}

        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        errtxt = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        if proc.returncode != 0 and "EOFError" in errtxt and "input(" in code:
            errtxt += ("\n[Timmy] This tool reads from stdin via input(). The test runner "
                       "doesn't supply interactive input — pass values as command-line args instead.")
        return {"stdout": out, "stderr": errtxt, "exit": proc.returncode,
                "seconds": round(time.time() - t0, 2)}
    finally:
        try: os.unlink(path)
        except Exception: pass

def save_tool(code, name, kind, ver=""):
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", (name or "tool")).strip("_") or "tool"
    vtag = f" v{ver}" if ver else ""
    # save under a fixed, predictable home location (never the volatile cwd)
    base = tools_dir()
    tk = detect_toolkit(code)
    if kind == "release":
        d = base / "release" / name
        d.mkdir(parents=True, exist_ok=True)
        pyp = d / (name + ".py")
        pyp.write_text(code + "\n", encoding="utf-8")
        if not IS_WIN:
            try: os.chmod(pyp, 0o755)
            except Exception: pass
        readme = d / "README.md"
        if not readme.exists():
            launch_lin = f"python3 {name}.py"
            pip_note = ""
            if tk and tk.get("pip"):
                pip_note = f"\n\nNeeds: `pip install {tk['pip']}`"
            elif tk and tk.get("apt_hint"):
                pip_note = f"\n\nNeeds: `{tk['apt_hint']}`"
            readme.write_text(
                f"# {name}{vtag}\n\nA 2D game built with Timmy "
                f"(tested on Kali / KDE Plasma and a OnePlus 6 on Phosh).{pip_note}\n\n"
                f"## Play\n\n```bash\n{launch_lin}\n```\n",
                encoding="utf-8")
        # .desktop entry so a GUI tool appears in the app menu / grid. StartupWMClass
        # helps KDE/GNOME bind the running window to this entry.
        if tk:
            dt = d / (name + ".desktop")
            dt.write_text(
                "[Desktop Entry]\nType=Application\n"
                f"Name={name}\nComment=Built with Timmy{vtag}\n"
                f"Exec=python3 {pyp}\nTerminal=false\n"
                f"StartupWMClass={name}\nStartupNotify=true\n"
                "Categories=Game;\n",
                encoding="utf-8")
        return {"path": str(d), "toolkit": tk["label"] if tk else None}
    else:
        d = base / "forge"
        d.mkdir(parents=True, exist_ok=True)
        pyp = d / (name + ".py")
        pyp.write_text(code + "\n", encoding="utf-8")
        if not IS_WIN:
            try: os.chmod(pyp, 0o755)
            except Exception: pass
        return {"path": str(pyp), "toolkit": tk["label"] if tk else None}

LICENSES = {
    "MIT": ("MIT License\n\nCopyright (c) {year} {holder}\n\nPermission is hereby granted, "
            "free of charge, to any person obtaining a copy of this software and associated "
            "documentation files (the \"Software\"), to deal in the Software without restriction, "
            "including without limitation the rights to use, copy, modify, merge, publish, "
            "distribute, sublicense, and/or sell copies of the Software, and to permit persons "
            "to whom the Software is furnished to do so, subject to the following conditions:\n\n"
            "The above copyright notice and this permission notice shall be included in all "
            "copies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED \"AS IS\", "
            "WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE "
            "WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. "
            "IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES "
            "OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING "
            "FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE "
            "SOFTWARE.\n"),
}

def _install_sh(user, repo, branch, name, pip_deps=""):
    """POSIX installer (Linux + macOS) — one-line install/update over HTTPS:
       curl -fsSL https://raw.githubusercontent.com/<user>/<repo>/<branch>/install.sh | bash
    Installs the script under ~/.local/share/<repo>, a CLI launcher on PATH, and on
    Linux a .desktop entry. Installs pip deps with --user fallbacks."""
    pip_line = ""
    if pip_deps.strip():
        pip_line = f'''
# install the python deps this tool needs
PIP_PKGS="{pip_deps.strip()}"
echo "installing python deps: $PIP_PKGS"
python3 -m pip install --user $PIP_PKGS --break-system-packages 2>/dev/null \\
  || python3 -m pip install --user $PIP_PKGS \\
  || echo "WARN: pip install failed for: $PIP_PKGS — install manually"
'''
    return f"""#!/usr/bin/env bash
# {repo} installer (Linux / macOS) — one-line install/update:
#   curl -fsSL https://raw.githubusercontent.com/{user}/{repo}/{branch}/install.sh | bash
set -euo pipefail
REPO="{user}/{repo}"; BRANCH="{branch}"
SRC="$HOME/.local/share/{repo}"; BIN="$HOME/.local/bin"; LAUNCH="$BIN/{name}"
APPS="$HOME/.local/share/applications"

command -v python3 >/dev/null 2>&1 || {{ echo "python3 required (>= 3.8)"; exit 1; }}
{pip_line}
mkdir -p "$SRC" "$BIN" "$APPS"
SELF_DIR="$( cd "$( dirname "${{BASH_SOURCE[0]:-$0}}" )" 2>/dev/null && pwd || true )"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/{name}.py" ]; then
  cp -f "$SELF_DIR/{name}.py" "$SRC/"
  [ -f "$SELF_DIR/requirements.txt" ] && cp -f "$SELF_DIR/requirements.txt" "$SRC/" || true
else
  if command -v git >/dev/null 2>&1; then
    if [ -d "$SRC/.git" ]; then git -C "$SRC" pull --ff-only --quiet || true
    else rm -rf "$SRC"; git clone --depth 1 -b "$BRANCH" "https://github.com/$REPO.git" "$SRC" --quiet; fi
  else
    TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$TARBALL" | tar xz -C "$SRC" --strip-components=1
    elif command -v wget >/dev/null 2>&1; then wget -qO- "$TARBALL" | tar xz -C "$SRC" --strip-components=1
    else echo "need git, curl, or wget"; exit 1; fi
  fi
fi

# CLI launcher
cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
exec python3 "$SRC/{name}.py" "\\$@"
EOF
chmod +x "$LAUNCH"

# desktop entry (Linux only — harmless on macOS)
if [ "$(uname -s)" = "Linux" ]; then
  cat > "$APPS/{name}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name={name}
Comment={repo} — built with Timmy
Exec=python3 $SRC/{name}.py
Terminal=false
Categories=Utility;Development;
EOF
  update-desktop-database "$APPS" >/dev/null 2>&1 || true
fi

case ":$PATH:" in *":$BIN:"*) ;; *)
  RC="$HOME/.bashrc"; [ -n "${{ZSH_VERSION:-}}" ] && RC="$HOME/.zshrc"
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
  echo "added $BIN to PATH in $RC — run: source $RC" ;;
esac
echo "installed {name}. launch from your app grid (Linux), or run: {name}"
"""

def write_github_repo(code, name, gh, details):
    """Write a complete polished repo into ~/timmy-games/github/<repo>/.
    Includes install.sh (Linux, curl|bash) so a release installs cleanly on Kali
    (KDE Plasma) and other Linux desktops, under Wayland or X11."""
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", (name or "tool")).strip("_") or "tool"
    user = details.get("username", "USER")
    repo = re.sub(r"[^A-Za-z0-9_.\-]", "-", details.get("repo", name)) or name
    branch = details.get("branch", "main")
    license_name = details.get("license", "MIT")
    holder = details.get("holder", user)

    d = tools_dir() / "github" / repo
    d.mkdir(parents=True, exist_ok=True)
    d = str(d)

    # main script
    pyp = os.path.join(d, name + ".py")
    with open(pyp, "w", encoding="utf-8") as f:
        f.write(code + "\n")
    if not IS_WIN:
        try: os.chmod(pyp, 0o755)
        except Exception: pass

    # README (AI-generated, with fallback)
    fallback_readme = (
        f"# {repo}\n\n{gh.get('description', 'A Linux graphical Python tool built with Timmy.')}\n\n"
        f"A native **Linux desktop** GUI tool — tested on Kali (KDE Plasma).\n\n"
        f"## Install\n\n"
        f"```bash\ncurl -fsSL https://raw.githubusercontent.com/{user}/{repo}/{branch}/install.sh | bash\n```\n\n"
        f"## Usage\n\nLaunch from your app grid / launcher, or run `{name}` in a terminal.\n"
    )
    readme = gh.get("readme") or fallback_readme
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)

    # .gitignore
    with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as f:
        f.write(gh.get("gitignore") or
                "__pycache__/\n*.py[cod]\n.venv/\nvenv/\n.env\n*.key\n.DS_Store\n"
                "build/\ndist/\n*.spec\n")

    # requirements (only if non-empty)
    reqs = (gh.get("requirements") or "").strip()
    if reqs:
        with open(os.path.join(d, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write(reqs + "\n")

    # derive pip deps line for the installers (joins requirements.txt-style lines into "pkg1 pkg2")
    pip_deps = " ".join(line.split("#", 1)[0].strip()
                        for line in reqs.splitlines() if line.strip() and not line.startswith("#"))

    # install.sh (Linux)
    ish = os.path.join(d, "install.sh")
    with open(ish, "w", encoding="utf-8", newline="\n") as f:
        f.write(_install_sh(user, repo, branch, name, pip_deps))
    if not IS_WIN:
        try: os.chmod(ish, 0o755)
        except Exception: pass

    # .desktop entry — for Linux users to drop into ~/.local/share/applications.
    # StartupWMClass helps KDE/GNOME bind the running window to this entry's icon.
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={gh.get('description', repo + ' — built with Timmy')}\n"
        f"Exec=python3 %h/.local/share/{repo}/{name}.py\n"
        "Terminal=false\n"
        f"StartupWMClass={name}\n"
        "StartupNotify=true\n"
        "Categories=Utility;Development;\n"
    )
    with open(os.path.join(d, name + ".desktop"), "w", encoding="utf-8") as f:
        f.write(desktop)

    # LICENSE
    lic = LICENSES.get(license_name)
    if lic:
        with open(os.path.join(d, "LICENSE"), "w", encoding="utf-8") as f:
            f.write(lic.format(year=time.strftime("%Y"), holder=holder))

    # the exact push commands, HTTPS only
    push = [
        "cd " + repo,
        "git init",
        "git add .",
        f'git commit -m "{repo} — initial release"',
        f"git branch -M {branch}",
        f"git remote add origin https://github.com/{user}/{repo}.git",
        f"git push -u origin {branch}",
    ]
    return {
        "path": d,
        "files": sorted(os.listdir(d)),
        "push": push,
        "install_line_posix": f"curl -fsSL https://raw.githubusercontent.com/{user}/{repo}/{branch}/install.sh | bash",
    }

# --------------------------------------------------------------------------
# PYINSTALLER  -- pack a tool into a standalone Linux binary
# --------------------------------------------------------------------------
# Timmy builds a single-file binary for Linux via PyInstaller in its managed
# venv. (No cross-compilation: PyInstaller bakes the host Python + libs into the
# output, so a binary built here runs on Linux only — which is the target.)
def build_executable(code, name, console=False):
    """Run PyInstaller in Timmy's managed venv to produce a single-file Linux
    binary. Returns the path to the artefact + a tail of the build log."""
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", (name or "tool")).strip("_") or "tool"
    if not code or not code.strip():
        return {"ok": False, "log": "no code to build"}

    # 1) ensure the venv exists and PyInstaller is installed in it
    venv_py = _venv_python()
    if not venv_py:
        # build the venv lazily so the user doesn't pay the cost until they actually build
        try:
            import venv
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(VENV_DIR)
            venv_py = _venv_python() or sys.executable
        except Exception as e:
            return {"ok": False, "log": f"venv creation failed: {e}"}

    # also install whatever the TOOL imports (toolkit + pip deps) so PyInstaller can
    # actually find them when it sniffs the script
    deps = detect_deps(code)
    pip_to_install = ["pyinstaller"] + [p for p in deps["pip"] if p]
    try:
        proc = subprocess.run([venv_py, "-m", "pip", "install", "--upgrade", *pip_to_install],
                              capture_output=True, text=True, timeout=900,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return {"ok": False, "log": "pip install failed:\n" + (proc.stderr or proc.stdout)[-2000:]}
    except Exception as e:
        return {"ok": False, "log": f"pip install error: {e}"}

    # 2) lay out a work dir under timmy-games/dist/<name>/
    workdir = tools_dir() / "dist" / name
    workdir.mkdir(parents=True, exist_ok=True)
    py_file = workdir / (name + ".py")
    py_file.write_text(code, encoding="utf-8")

    dist_dir  = workdir / "out"
    build_dir = workdir / "build"
    spec_dir  = workdir / "spec"
    for p in (dist_dir, build_dir, spec_dir):
        p.mkdir(exist_ok=True)

    # 3) build args: --onefile bakes everything into one binary, --windowed drops the
    #    controlling console for GUI tools, --clean wipes PyInstaller's cache so
    #    re-builds always reflect the latest code
    args = [venv_py, "-m", "PyInstaller", "--onefile", "--clean", "--noconfirm",
            "--name", name,
            "--distpath", str(dist_dir),
            "--workpath", str(build_dir),
            "--specpath", str(spec_dir)]
    if not console:
        args.append("--windowed")
    # bundle the Timmy icon if present (PyInstaller takes a PNG on Linux)
    icon_png = Path(HERE) / "assets" / "icon.png"
    if icon_png.exists():
        args += ["--icon", str(icon_png)]
    args.append(str(py_file))

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=1200,
                              encoding="utf-8", errors="replace")
        log = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return {"ok": False, "log": "PyInstaller timed out after 20 minutes."}
    except Exception as e:
        return {"ok": False, "log": f"PyInstaller crashed: {e}"}

    # 4) find the artefact
    out_name, target = name, "Linux binary"
    out_path = dist_dir / out_name
    if proc.returncode == 0 and out_path.exists():
        size_mb = round(out_path.stat().st_size / (1024 * 1024), 1)
        return {"ok": True, "path": str(out_path), "target": target,
                "size_mb": size_mb, "log": log[-2000:]}
    return {"ok": False, "log": "PyInstaller didn't produce a binary.\n\n" + log[-2500:]}


# --------------------------------------------------------------------------
# SESSION LOG  -- every run is appended; one button hands it all to the model
# --------------------------------------------------------------------------
SESSION_LOG = []   # list of dicts: {ts, kind, name, args, exit, seconds, stdout, stderr}

def log_run(name, args, result):
    SESSION_LOG.append({
        "ts": time.strftime("%H:%M:%S"),
        "name": name, "args": args,
        "exit": result.get("exit"), "seconds": result.get("seconds"),
        "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""),
    })
    # keep it bounded so we never blow the context window
    if len(SESSION_LOG) > 40:
        del SESSION_LOG[0:len(SESSION_LOG) - 40]

def render_log(full=True):
    """Render the session log as a single text blob (also what gets saved to file)."""
    lines = [f"Timmy session log — {len(SESSION_LOG)} run(s)", "=" * 50]
    for i, e in enumerate(SESSION_LOG, 1):
        lines.append(f"\n[run {i}] {e['ts']}  {e['name']}.py {e['args']}".rstrip())
        lines.append(f"exit {e['exit']} · {e['seconds']}s")
        if e["stdout"]:
            out = e["stdout"] if full else e["stdout"][-1500:]
            lines.append("--- stdout ---\n" + out.rstrip())
        if e["stderr"]:
            lines.append("--- stderr ---\n" + e["stderr"].rstrip())
    return "\n".join(lines)

def fix_from_log(code, messages, provider_id=None):
    """Send the current code + the whole session log to the model for a fix."""
    if not SESSION_LOG:
        return {"error": "No runs logged yet — run the tool at least once first."}
    log_blob = render_log(full=False)
    convo = [m for m in messages if m.get("role") != "system"]
    convo = [{"role": "system", "content": SYSTEM_PROMPT}] + convo + [{
        "role": "user",
        "content": (
            "Here is the current tool and the full log of how it behaved when I ran it. "
            "Diagnose every problem you can see in the runs and return the FULL corrected "
            "script. Briefly list what you fixed.\n\n"
            f"=== CURRENT CODE ===\n```python\n{code}\n```\n\n"
            f"=== RUN LOG ===\n{log_blob}"
        )
    }]
    return chat_with_autotest(convo, provider_id)

def polish_round(code, messages, provider_id=None):
    """One iteration of the auto-polish loop: run a quick smoke, then ask the model
    to make the tool more robust/polished, returning improved code."""
    # smoke the current code so we can tell the model what's wrong right now
    passed, report, _ = smoke_test(code)
    state_note = "It passes a basic smoke test." if passed else f"It currently FAILS a check:\n{report}"
    log_blob = render_log(full=False) if SESSION_LOG else "(no runs yet)"
    convo = [{"role": "system", "content": SYSTEM_PROMPT}, {
        "role": "user",
        "content": (
            "Improve this tool by one meaningful increment: fix any bug, harden error "
            "handling, improve output clarity, and add the single most valuable missing "
            "feature — but keep it ONE self-contained script and don't over-engineer. "
            "Return the FULL improved script and one line on what you changed.\n\n"
            f"{state_note}\n\n=== CODE ===\n```python\n{code}\n```\n\n=== RECENT RUNS ===\n{log_blob}"
        )
    }]
    return chat_with_autotest(convo, provider_id)

# ==========================================================================
# http
# ==========================================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._file(os.path.join(HERE, "ui", "index.html"), "text/html; charset=utf-8")
        elif self.path.startswith("/assets/"):
            name = os.path.basename(self.path)
            ext = name.rsplit(".", 1)[-1].lower()
            ctype = {"svg": "image/svg+xml", "png": "image/png"}.get(ext, "application/octet-stream")
            self._file(os.path.join(HERE, "assets", name), ctype)
        elif self.path.startswith("/sounds/"):
            # serve any audio file the user dropped in the sounds/ directory.
            # supports mp3, wav, ogg, m4a, flac — whatever the browser can play.
            name = os.path.basename(self.path)
            # only serve plain filenames — no path traversal
            if "/" in name or "\\" in name or name.startswith("."):
                self._send(404, {"error": "not found"}); return
            ext = name.rsplit(".", 1)[-1].lower()
            ctype = {
                "mp3":  "audio/mpeg",
                "wav":  "audio/wav",
                "ogg":  "audio/ogg",
                "oga":  "audio/ogg",
                "m4a":  "audio/mp4",
                "flac": "audio/flac",
                "aac":  "audio/aac",
            }.get(ext, "application/octet-stream")
            full = os.path.join(HERE, "sounds", name)
            if not os.path.isfile(full):
                self._send(404, {"error": "no such sound"}); return
            self._file(full, ctype)
        elif self.path == "/api/sounds":
            # tell the UI which trigger files actually exist, so it knows what to play.
            # The UI looks for these filenames in HERE/sounds/:
            #   startup.{mp3|wav|ogg|m4a}      — played when Timmy opens
            #   done.{mp3|wav|ogg|m4a}         — played when the model finishes a tool
            #   build.{mp3|wav|ogg|m4a}        — played when PyInstaller succeeds
            # User can drop any one of those extensions; we pick the first that exists.
            sdir = os.path.join(HERE, "sounds")
            os.makedirs(sdir, exist_ok=True)
            mapping = {}
            for trigger in ("startup", "done", "build"):
                for ext in ("mp3", "wav", "ogg", "m4a", "flac"):
                    cand = f"{trigger}.{ext}"
                    if os.path.isfile(os.path.join(sdir, cand)):
                        mapping[trigger] = "/sounds/" + cand
                        break
            self._send(200, {"sounds": mapping, "dir": sdir})
        elif self.path == "/api/status":
            provs = []
            for pid, p in PROVIDERS.items():
                chain = provider_model_chain(pid)   # live if cached, else fallback
                provs.append({"id": pid, "label": p["label"],
                              "hasKey": bool(STATE["keys"].get(pid)),
                              "models": chain,
                              "chosen": STATE["models"].get(pid) or (chain[0] if chain else "?"),
                              "topModel": chain[0] if chain else "?",
                              "live": pid in _MODEL_CACHE})
            cur_chain = provider_model_chain(STATE["provider"])
            chosen_cur = STATE["models"].get(STATE["provider"]) or (cur_chain[0] if cur_chain else "?")
            self._send(200, {
                "providers": provs,
                "provider": STATE["provider"],
                "model": chosen_cur,
                "hasKey": bool(STATE["keys"].get(STATE["provider"])),
                "autotest": AUTOTEST_MAX_ROUNDS,
                "version": __version__,
                "desktop": detect_desktop_env(),
            })
        elif self.path == "/api/log":
            self._send(200, {"log": render_log(full=True), "runs": len(SESSION_LOG)})
        elif self.path == "/api/library":
            self._send(200, library_list())
        elif self.path == "/api/running":
            self._send(200, list_running())
        elif self.path == "/api/sessions":
            self._send(200, session_list())
        elif self.path == "/api/log.txt":
            blob = render_log(full=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=timmy-session.log")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode() or "{}")
        except Exception:
            return self._send(400, {"error": "bad json"})

        if self.path == "/api/key":
            pid = data.get("provider") or STATE["provider"]
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            STATE["keys"][pid] = (data.get("key") or "").strip()
            saved = persist_state() if STATE["keys"][pid] else False
            # a new key means we can now ask the provider what it actually offers
            fetched = None
            if STATE["keys"][pid]:
                _MODEL_CACHE.pop(pid, None)
                _HOST_OK.pop(pid, None)
                fetched = fetch_models(pid, force=True)
            self._send(200, {"hasKey": bool(STATE["keys"][pid]), "saved": saved,
                             "models": (fetched or {}).get("models"),
                             "modelSource": (fetched or {}).get("source"),
                             "modelError": (fetched or {}).get("error")})
        elif self.path == "/api/provider":
            pid = data.get("provider")
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            STATE["provider"] = pid
            persist_state()
            chain = provider_model_chain(pid)
            self._send(200, {"provider": pid, "hasKey": bool(STATE["keys"].get(pid)),
                             "model": STATE["models"].get(pid) or (chain[0] if chain else "?")})
        elif self.path == "/api/models/refresh":
            pid = data.get("provider") or STATE["provider"]
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            self._send(200, {"provider": pid, **fetch_models(pid, force=True)})
        elif self.path == "/api/model":
            pid = data.get("provider") or STATE["provider"]
            model = data.get("model")
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            # accept any model from the live catalog OR the static fallback
            valid = set(provider_model_chain(pid)) | set(PROVIDERS[pid]["models"])
            if model and model in valid:
                STATE["models"][pid] = model
                persist_state()
                self._send(200, {"provider": pid, "model": model})
            else:
                self._send(200, {"error": "unknown model for this provider"})
        elif self.path == "/api/chat":
            # The methodology prompt is authoritative and lives here, server-side.
            convo = [m for m in data.get("messages", []) if m.get("role") != "system"]
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + convo
            provider = data.get("provider")  # optional per-request override
            self._send(200, chat_with_autotest(messages, provider))
        elif self.path == "/api/run":
            result = run_code(data.get("code", ""), data.get("args", ""),
                              bool(data.get("confirm")), data.get("name", "tool"))
            # log only actual runs (not the confirm-gate response)
            if "needsConfirm" not in result:
                log_run(data.get("name", "tool"), data.get("args", ""), result)
            self._send(200, result)
        elif self.path == "/api/stop":
            self._send(200, stop_running(int(data.get("pid", 0) or 0)))
        elif self.path == "/api/fixlog":
            convo = data.get("messages", [])
            self._send(200, fix_from_log(data.get("code", ""), convo, data.get("provider")))
        elif self.path == "/api/review":
            self._send(200, review_code(data.get("code", ""), data.get("provider")))
        elif self.path == "/api/intake":
            self._send(200, make_intake(data.get("request", ""), data.get("provider")))
        elif self.path == "/api/github":
            self._send(200, make_github(data.get("code", ""), data.get("details", {}),
                                        data.get("provider")))
        elif self.path == "/api/github/write":
            try:
                self._send(200, write_github_repo(data.get("code", ""), data.get("name", "tool"),
                                                  data.get("github", {}), data.get("details", {})))
            except Exception as e:
                self._send(200, {"error": str(e)})
        elif self.path == "/api/log.clear":
            SESSION_LOG.clear()
            self._send(200, {"runs": 0})
        elif self.path == "/api/library/save":
            self._send(200, library_save(data.get("name", "tool"), data.get("code", ""),
                                         data.get("messages", []),
                                         data.get("version", "testing"),
                                         data.get("args", ""), data.get("sessionId"),
                                         data.get("ver", "1.0")))
        elif self.path == "/api/library/load":
            self._send(200, library_load(data.get("id", "")))
        elif self.path == "/api/library/delete":
            self._send(200, library_delete(data.get("id", "")))
        elif self.path == "/api/session/save":
            self._send(200, session_save(data.get("id"), data.get("name", "untitled"),
                                         data.get("code", ""), data.get("messages", []),
                                         data.get("version", "testing"), data.get("args", ""),
                                         data.get("ver", "1.0")))
        elif self.path == "/api/session/load":
            self._send(200, session_load(data.get("id", "")))
        elif self.path == "/api/session/delete":
            self._send(200, session_delete(data.get("id", "")))
        elif self.path == "/api/deps":
            self._send(200, detect_deps(data.get("code", "")))
        elif self.path == "/api/deps/install":
            self._send(200, install_deps(data.get("pip", []) or data.get("deps", [])))
        elif self.path == "/api/build":
            try:
                self._send(200, build_executable(
                    data.get("code", ""),
                    data.get("name", "tool"),
                    bool(data.get("console", False))))
            except Exception as e:
                self._send(200, {"ok": False, "log": f"build crashed: {e}"})
        elif self.path == "/api/platform":
            self._send(200, {"os": platform.system(), "python": platform.python_version(),
                             "is_win": IS_WIN, "is_mac": IS_MAC, "is_linux": IS_LINUX,
                             "desktop": detect_desktop_env()})
        elif self.path == "/api/polish":
            convo = data.get("messages", [])
            self._send(200, polish_round(data.get("code", ""), convo, data.get("provider")))
        elif self.path == "/api/save":
            try:
                self._send(200, save_tool(data.get("code", ""), data.get("name", "tool"),
                                          data.get("kind", "testing"), data.get("ver", "")))
            except Exception as e:
                self._send(200, {"error": str(e)})
        elif self.path == "/api/quit":
            self._send(200, {"ok": True})
            # shut the server down shortly after responding
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
        else:
            self._send(404, {"error": "not found"})

def free_port(host, start):
    for p in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, p)) != 0:
                return p
    return start

def launch_app_window(url):
    """Open Timmy in a Chromium-family app window (no browser chrome).
    Falls back to a normal browser tab if no Chromium-family browser is found.
    Searches the right places on Windows, macOS, and Linux."""
    candidates = []
    if IS_WIN:
        # common install locations on Windows (Program Files + LocalAppData per-user installs)
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        ]
    elif IS_MAC:
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    else:
        # Linux: rely on PATH lookups for the binaries
        for binname in ("chromium", "chromium-browser", "google-chrome",
                        "google-chrome-stable", "brave-browser", "microsoft-edge", "vivaldi"):
            p = shutil.which(binname)
            if p:
                candidates.append(p)

    app_data = str(app_data_dir() / "window")
    for path in candidates:
        if not path:
            continue
        # PATH-relative names → resolve them; absolute paths must exist
        resolved = path if os.path.isabs(path) else shutil.which(path)
        if not resolved or not os.path.exists(resolved):
            continue
        try:
            argv = [resolved, f"--app={url}",
                    f"--user-data-dir={app_data}",
                    "--no-first-run", "--no-default-browser-check",
                    # let Timmy play its startup sound without needing a user gesture first.
                    # Chromium-family flag — safe on Chrome / Edge / Brave / Vivaldi / Chromium.
                    "--autoplay-policy=no-user-gesture-required",
                    "--window-size=1280,860"]
            # On Linux, set the window's WM class / Wayland app_id to "timmy" so it
            # matches StartupWMClass in the .desktop entry. Without this the running
            # window shows a generic Chromium icon in the KDE Plasma task switcher and
            # the GNOME/other desktop overview instead of the Timmy icon.
            if IS_LINUX:
                argv.insert(1, "--class=timmy")
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return os.path.basename(resolved)
        except Exception:
            continue
    # fallback: ordinary browser tab via webbrowser (handles every OS)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return None

def main():
    port = free_port(HOST, PORT)
    url = f"http://{HOST}:{port}"
    srv = ThreadingHTTPServer((HOST, port), Handler)
    print(f"\n  Timmy v{__version__}  —  {url}")
    print(f"  Linux Python toolsmith  ·  running on {platform.system()}")
    have = [PROVIDERS[pid]["label"] for pid in PROVIDERS if STATE["keys"].get(pid)]
    if have:
        print(f"  keys loaded for: {', '.join(have)}")
        # fetch each keyed provider's live model catalog in the background so the
        # dropdown is accurate without blocking startup
        def _warm():
            for pid in PROVIDERS:
                if STATE["keys"].get(pid):
                    fetch_models(pid, force=True)
        threading.Thread(target=_warm, daemon=True).start()
    else:
        print("  no API keys yet — add one in Settings")
    print(f"  active provider: {PROVIDERS[STATE['provider']]['label']}")
    print(f"  auto-test: up to {AUTOTEST_MAX_ROUNDS} silent fix rounds")
    print("  serving local-only. ctrl-c to stop.\n")
    used = launch_app_window(url)
    if used:
        print(f"  opened in app window via {used}")
    else:
        print("  no Chromium-family browser found — opened a normal tab\n"
              "  (install Chrome/Edge/Brave for the clean app window)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  forge banked. later, dawg.\n")
        srv.shutdown()

if __name__ == "__main__":
    main()
