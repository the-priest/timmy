#!/usr/bin/env bash
#
# Timmy installer  (Linux + macOS)
# ----------------------------------
# One-line install / update (paste in a terminal):
#
#   curl -fsSL https://raw.githubusercontent.com/the-priest/timmy/main/install.sh | bash
#
# Or, from a clone:   ./install.sh
#
# What it does (no root needed — everything under $HOME):
#   - checks for python3 (>= 3.8)
#   - fetches the LATEST repo into ~/.local/share/timmy     (git, or tarball fallback)
#   - drops a `timmy` launcher into ~/.local/bin            (so you can just type `timmy`)
#   - installs icons + a .desktop entry on Linux              (appears in your app menu)
#   - makes sure ~/.local/bin is on your PATH
#   - explains how to set the API key for your provider of choice
#
# Running it again = updating. It always pulls the latest from GitHub unless you
# explicitly run a local ./install.sh from a separate checkout.
#
set -euo pipefail

REPO="the-priest/timmy"
BRANCH="main"
SRC_DIR="$HOME/.local/share/timmy"
BIN_DIR="$HOME/.local/bin"
ICON_DIR="$HOME/.local/share/icons/hicolor"
APP_DIR="$HOME/.local/share/applications"
LAUNCHER="$BIN_DIR/timmy"
TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"

OS="$(uname -s)"

# ---- pretty ----
if [ -t 1 ]; then
  B="\033[1m"; R="\033[0m"; AMBER="\033[38;5;179m"; LIME="\033[38;5;149m"
  RED="\033[38;5;167m"; GREY="\033[38;5;245m"
else
  B=""; R=""; AMBER=""; LIME=""; RED=""; GREY=""
fi
say()  { printf "${AMBER}${B}::${R} %b\n" "$1"; }
ok()   { printf "  ${LIME}\xe2\x9c\x93${R} %b\n" "$1"; }
warn() { printf "  ${RED}\xe2\x9a\xa0${R} %b\n" "$1"; }
step() { printf "  ${GREY}\xe2\x80\xa6 %b${R}\n" "$1"; }

printf "\n${AMBER}${B}  Timmy installer${R}  ${GREY}\xe2\x80\x94 ${REPO}${R}\n"
printf "  ${GREY}AI 2D game forge for Linux & macOS${R}\n\n"

# ---- python ----
say "checking python"
if ! command -v python3 >/dev/null 2>&1; then
  warn "python3 not found"
  case "$OS" in
    Linux)  printf "    install it:  ${B}sudo apt install python3${R}  (Debian/Ubuntu/Mint/Kali)\n"
            printf "    or:          ${B}sudo dnf install python3${R}    (Fedora)\n" ;;
    Darwin) printf "    install it:  ${B}brew install python3${R}        (Homebrew)\n"
            printf "    or grab it from https://python.org\n" ;;
  esac
  exit 1
fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
ok "python3 $PYV"

# ---- decide the source ----
# A genuine local checkout means: this script exists as a real file on disk, sits
# next to timmy.py, AND that folder is NOT the install dir itself.
#
# When run via `curl | bash`, the script has no on-disk path, so [ -f "$SCRIPT" ]
# is false and we ALWAYS fall through to GitHub. This is the fix for the old bug
# where piping from inside ~/.local/share/timmy made it copy the folder onto
# itself ("are the same file") and silently skip the update.
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
LOCAL_SRC=""
if [ -f "$SCRIPT_PATH" ]; then
  CAND="$( cd "$( dirname "$SCRIPT_PATH" )" 2>/dev/null && pwd || true )"
  if [ -n "$CAND" ] && [ -f "$CAND/timmy.py" ] && [ "$CAND" != "$SRC_DIR" ]; then
    LOCAL_SRC="$CAND"
  fi
fi

mkdir -p "$SRC_DIR" "$BIN_DIR" "$APP_DIR" \
  "$ICON_DIR/512x512/apps" "$ICON_DIR/256x256/apps" \
  "$ICON_DIR/128x128/apps" "$ICON_DIR/scalable/apps"

fetch_tarball() {
  step "downloading latest tarball"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$TARBALL" | tar xz -C "$SRC_DIR" --strip-components=1
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$TARBALL" | tar xz -C "$SRC_DIR" --strip-components=1
  else
    warn "need git, curl, or wget to fetch the source"
    exit 1
  fi
}

say "fetching source"
if [ -n "$LOCAL_SRC" ]; then
  step "installing from local checkout: $LOCAL_SRC"
  cp -rf "$LOCAL_SRC/timmy.py" "$LOCAL_SRC/ui" "$LOCAL_SRC/assets" "$SRC_DIR/"
  mkdir -p "$SRC_DIR/sounds"
  [ -d "$LOCAL_SRC/sounds" ] && cp -rf "$LOCAL_SRC/sounds/." "$SRC_DIR/sounds/" || true
  [ -f "$LOCAL_SRC/README.md" ] && cp -f "$LOCAL_SRC/README.md" "$SRC_DIR/" || true
  [ -f "$LOCAL_SRC/LICENSE" ]   && cp -f "$LOCAL_SRC/LICENSE"   "$SRC_DIR/" || true
elif [ -d "$SRC_DIR/.git" ] && command -v git >/dev/null 2>&1; then
  step "updating existing checkout (git)"
  git -C "$SRC_DIR" fetch --depth 1 origin "$BRANCH" --quiet || true
  git -C "$SRC_DIR" reset --hard "origin/$BRANCH" --quiet \
    || git -C "$SRC_DIR" pull --ff-only --quiet || true
elif [ -f "$SRC_DIR/timmy.py" ]; then
  # existing non-git install — overlay latest code, keep your sounds/config in place
  fetch_tarball
elif command -v git >/dev/null 2>&1; then
  step "git clone $REPO"
  tmp="$(mktemp -d)"
  if git clone --depth 1 -b "$BRANCH" "https://github.com/$REPO.git" "$tmp" --quiet; then
    cp -rf "$tmp/." "$SRC_DIR/"
    rm -rf "$tmp"
  else
    rm -rf "$tmp"
    fetch_tarball
  fi
else
  fetch_tarball
fi

if [ ! -f "$SRC_DIR/timmy.py" ]; then
  warn "source fetch failed — $SRC_DIR/timmy.py is missing"
  exit 1
fi
ok "source at $SRC_DIR"

# ---- CLI launcher ----
say "writing launcher: $LAUNCHER"
cat > "$LAUNCHER" <<EOSH
#!/usr/bin/env bash
exec python3 "$SRC_DIR/timmy.py" "\$@"
EOSH
chmod +x "$LAUNCHER"
ok "launcher: $LAUNCHER"

# ---- icons (Linux): install every size + the scalable SVG so any desktop
#      environment (KDE Plasma, GNOME, Phosh, XFCE…) picks a crisp icon ----
[ -f "$SRC_DIR/assets/icon-512.png" ] && cp -f "$SRC_DIR/assets/icon-512.png" "$ICON_DIR/512x512/apps/timmy.png"
[ -f "$SRC_DIR/assets/icon-256.png" ] && cp -f "$SRC_DIR/assets/icon-256.png" "$ICON_DIR/256x256/apps/timmy.png"
[ -f "$SRC_DIR/assets/icon-128.png" ] && cp -f "$SRC_DIR/assets/icon-128.png" "$ICON_DIR/128x128/apps/timmy.png"
[ -f "$SRC_DIR/assets/icon.svg" ]     && cp -f "$SRC_DIR/assets/icon.svg"     "$ICON_DIR/scalable/apps/timmy.svg"

if [ "$OS" = "Linux" ]; then
  say "registering app menu entry"
  # Terminal=false because Timmy backgrounds its own local server and opens a
  # browser app window — no terminal needed. StartupWMClass ties the window back
  # to this entry so the Timmy icon shows in the task switcher / overview.
  cat > "$APP_DIR/timmy.desktop" <<EODESKTOP
[Desktop Entry]
Type=Application
Name=Timmy
GenericName=AI 2D Game Forge
Comment=Build 2D games with AI — runs on KDE/X11 and NetHunter/Phosh
Exec=$LAUNCHER
Icon=timmy
Terminal=false
Categories=Development;Game;
StartupNotify=true
StartupWMClass=timmy
Keywords=AI;Python;Games;Pygame;2D;
EODESKTOP
  chmod 644 "$APP_DIR/timmy.desktop"
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
  gtk-update-icon-cache -f -t "$ICON_DIR" >/dev/null 2>&1 || true
  kbuildsycoca6 >/dev/null 2>&1 || kbuildsycoca5 >/dev/null 2>&1 || true
  ok "app menu: Timmy (search your launcher / app grid)"
fi

# ---- PATH ----
case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR already on PATH" ;;
  *)
    RC="$HOME/.bashrc"
    [ -n "${ZSH_VERSION:-}" ] && RC="$HOME/.zshrc"
    [ "$OS" = "Darwin" ] && [ ! -f "$RC" ] && RC="$HOME/.zshrc"
    printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
    warn "added $BIN_DIR to PATH in $RC"
    printf "    open a new terminal, or run:  ${B}source $RC${R}\n"
  ;;
esac

# ---- key setup hint ----
printf "\n${AMBER}${B}  set your API key${R}  (one of these, before launching — or use Settings in-app)\n"
printf "  ${B}export GROQ_API_KEY=gsk_...${R}            ${GREY}# Groq        (recommended — fast + free tier)${R}\n"
printf "  ${B}export SILICONFLOW_API_KEY=sk-...${R}       ${GREY}# SiliconFlow${R}\n"
printf "  ${B}export GOOGLE_API_KEY=AIza...${R}           ${GREY}# Google AI Studio${R}\n"
printf "  ${B}export NOVITA_API_KEY=sk_...${R}            ${GREY}# Novita AI${R}\n"
printf "  ${GREY}(add to ~/.bashrc / ~/.zshrc to persist, or set it inside Timmy's Settings panel)${R}\n"

# ---- done ----
printf "\n${LIME}${B}  ready.${R}  launch with:\n"
printf "  ${B}timmy${R}\n"
[ "$OS" = "Linux" ] && printf "  ${GREY}or pick Timmy from your app menu${R}\n"
printf "\n"
