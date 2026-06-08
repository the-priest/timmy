# sounds

Timmy plays a short chime at three moments. **It ships with default sounds, so this
works out of the box** — but every one is overridable by dropping your own file here.

| File          | Plays when                                       | Ships by default |
|---------------|--------------------------------------------------|:----------------:|
| `startup.*`   | Timmy opens                                      | ✅ `startup.ogg` |
| `done.*`      | the model lands a finished game in the editor    | ✅ `done.ogg`    |
| `build.*`     | a ⬛ build finishes successfully                  | ✅ `build.ogg`   |

## Overriding a sound

- **Drop a file named exactly `startup`, `done`, or `build`** with one of these
  extensions: `.mp3`, `.wav`, `.ogg`, `.m4a`, `.flac`. Your file **wins over the
  default** for that trigger — Timmy checks `.mp3` → `.wav` → `.ogg` → `.m4a` →
  `.flac` and uses the first it finds, and the bundled defaults are `.ogg`, so a
  `done.mp3` you drop in beats the shipped `done.ogg`.
- **Want silence on a trigger?** Delete that default `.ogg` (e.g. `rm done.ogg`) and
  don't replace it — that trigger goes quiet, no errors, no nags.
- Your own audio files are **not** committed (`.gitignore` ignores everything here
  except the three shipped `.ogg` defaults and these docs).

## Notes

- **Autoplay**: the launcher passes `--autoplay-policy=no-user-gesture-required` to
  Chromium-family browsers, so the startup sound plays even before you click. In a
  plain Firefox tab it may wait for your first click — that's a browser policy, not a
  Timmy bug; Timmy fires the queued startup chime on that first interaction.
- **Volume / overlap**: 85% by default. Each play is cloned, so rapid iterations layer
  instead of cutting each other off.
- **Size**: keep replacements short — a second or two. The shipped defaults are ~0.5–1.2s.
- **Reload**: Timmy reads this directory at boot and on a page refresh (Ctrl/Cmd+R). Drop
  a new file while it's running, then refresh.
