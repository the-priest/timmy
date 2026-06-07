# sounds

Drop audio files here and Timmy plays them at three moments:

| Drop this file       | Plays when                                    |
|----------------------|-----------------------------------------------|
| `startup.mp3`        | Timmy opens                                 |
| `done.mp3`           | The model lands a finished tool in the editor |
| `build.mp3`          | ⬛ build finishes successfully                |

## Notes

- **Filename is what matters** — the file *must* be called exactly `startup`,
  `done`, or `build`, with one of these extensions: `.mp3`, `.wav`, `.ogg`,
  `.m4a`, `.flac`. The first match wins, so if you drop both `done.mp3` and
  `done.wav`, the mp3 plays.
- **It's safe to leave any of them empty.** If a file isn't here, that trigger
  is silent — no errors, no nags.
- **Autoplay**: the launcher passes `--autoplay-policy=no-user-gesture-required`
  to Chromium-family browsers, so the startup sound plays even before you've
  clicked anything. If you ever launch Timmy in a plain Firefox tab, the
  startup sound may stay quiet until you click somewhere — that's a browser
  policy, not a Timmy bug.
- **Volume / overlap**: 85% by default. Sounds are cloned per-play so rapid
  iterations don't cut off the previous one.
- **Size**: keep them short. A few seconds is great. Multi-minute files will
  hammer your nerves *and* your bandwidth.
- **Reload**: Timmy reads this directory at boot and after a page refresh
  (Ctrl/Cmd+R). If you drop in new files while it's running, just refresh.
- **.gitignore**: this folder's audio files aren't tracked — only this README is.

## Where to get sounds

You make them, you find them online, you record yourself yelling "DAEDALUS" into
your mic for `done.mp3`. Whatever fits. No defaults are shipped.
