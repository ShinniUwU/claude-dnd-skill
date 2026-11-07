# Hosted multiplayer fork (velvetcore)

This branch runs the display as an always-on, internet-facing table. Players log in with Discord, each login is bound to one character, and scenes can be private while the party is split.

## What this branch adds
- **Discord login and per-character privacy** (`display/auth_ext.py`, `login.html`, `settings.html`). `send.py --to` makes a block private, and `send.py --party together|split` sets the party mode.
- **Split-mode guard.** While split, `send.py` refuses any visible block without `--to` (use `--all` to send one to everyone on purpose).
- **Remote display.** `send.py`, `push_stats.py`, `check_input.py`, `autorun_wait.py` and `dice_player.py` honour `DND_DISPLAY_URL`, so the display can run on another host.
- **Queued actions.** Players can edit or withdraw an action until the DM takes it. The app and `check_input.py` both take the queue by rename, so an action is never lost or delivered twice. The "ready" checks survive a restart.
- **Voices.**
  - In `--npc` blocks, quotes use the NPC's profile and the text between them uses the Narrator's.
  - In narration, `{Name}"..."` tags a quote with a character's voice. The tag is stripped before display.
  - Audio is cached per segment.
  - A "Voices" status light in the audio controls (`/tts/status`), plus an offline note when synthesis fails.
  - The per-block voice picker is removed (each character has a fixed profile in `voices.json`).
- **`send.py` no longer hangs** on body-less calls (`--party`, stat-only) under non-TTY harnesses.
- **`campaign_graph.py`** resolves names by words ("Ilse Varrow" finds "Madam Ilse Varrow").

## Installing over the plugin
Claude Code does not load a symlinked skill folder or a symlinked `SKILL.md`. `relink-plugin.sh` makes the plugin's `skills/dnd` a real folder: `SKILL.md` is hard-linked to the working copy, and every other entry is a symlink. It also moves the stock copy out of `skills/`, so it can't load as a second skill. Adjust `SRC` in the script, and re-run it after every plugin update.
