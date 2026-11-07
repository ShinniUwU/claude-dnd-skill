#!/usr/bin/env bash
# Make the installed dm plugin load the modified skill in /opt/dnd/skill.
# Run after any `claude plugin update dm@neural-initiative`.
#
# Claude Code does not load a symlinked skill folder, nor a symlinked SKILL.md.
# So skills/dnd is a REAL directory, SKILL.md in it is a HARD link to
# /opt/dnd/skill/SKILL.md (same file, so edits show up in both; falls back to a
# copy across filesystems), and every other entry is a symlink into
# /opt/dnd/skill. An editor that saves by replacing the file breaks the hard
# link; re-run this script to repair it (or add it as a SessionStart hook:
# "bash /opt/dnd/relink-plugin.sh --quiet").
#
# The stock copy is kept OUTSIDE skills/ (as <version>/dnd.orig). Anything left
# inside skills/ gets registered as its own skill (it showed up as dm:dnd-orig
# and was what /dm:dnd actually loaded).
set -euo pipefail
SRC=/opt/dnd/skill
QUIET=0; [ "${1:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" = 1 ] || echo "$@"; }
for d in "$HOME"/.claude/plugins/cache/neural-initiative/dm/*/skills; do
  [ -d "$d" ] || continue
  ver_dir="$(dirname "$d")"
  dst="$d/dnd"

  # Move any stock copy left inside skills/ by older versions of this script.
  if [ -d "$d/dnd.orig" ] && [ ! -L "$d/dnd.orig" ]; then
    rm -rf "$ver_dir/dnd.orig"
    mv "$d/dnd.orig" "$ver_dir/dnd.orig"
    say "moved stray $d/dnd.orig -> $ver_dir/dnd.orig"
  fi

  if [ -L "$dst" ]; then
    rm "$dst"                                   # old style: symlinked folder (not loaded)
  elif [ -d "$dst" ] && [ ! -e "$dst/.linked-to-opt-dnd" ]; then
    rm -rf "$ver_dir/dnd.orig"                  # fresh plugin install: keep the stock copy
    mv "$dst" "$ver_dir/dnd.orig"
    say "kept stock copy at $ver_dir/dnd.orig"
  fi
  mkdir -p "$dst"
  find "$dst" -maxdepth 1 -type l -delete       # refresh entry links
  for f in "$SRC"/* "$SRC"/.[!.]*; do
    [ -e "$f" ] || continue
    name="$(basename "$f")"
    if [ "$name" = "SKILL.md" ]; then
      ln -f "$f" "$dst/SKILL.md" 2>/dev/null || cp -f "$f" "$dst/SKILL.md"
    else
      ln -s "$f" "$dst/$name"
    fi
  done
  touch "$dst/.linked-to-opt-dnd"
  say "linked: $dst -> $SRC (real folder, SKILL.md hard-linked, so the plugin loads it)"
done
