#!/bin/sh
# Refresh the container's copy of the two resume skills from the copy Claude Code
# keeps on this Mac (~/.claude/skills/synced/...). Run it after you change either
# skill on claude.ai and have opened Claude Code on the Mac once. No rebuild needed:
# the container reads docker/skills directly.
set -e
cd "$(dirname "$0")"
for s in tailor-resume resume-impact-pass; do
  src=$(ls -td "$HOME"/.claude/skills/synced/*/"$s" 2>/dev/null | head -1)
  if [ -z "$src" ]; then
    echo "not found: $s (open Claude Code on the Mac once so it syncs your skills)"
    continue
  fi
  mkdir -p "skills/$s"
  cp -R "$src"/. "skills/$s/"
  echo "updated $s from $src"
done
# The claude.ai copies hold the mobile number; this repo's copies keep it in Backend_2026/Personal_Details.md.
if grep -rqE '\+[0-9]{3} ?[0-9]{6,}' skills; then
  echo "WARNING: docker/skills has the mobile number again. Remove it before you commit or push."
fi
