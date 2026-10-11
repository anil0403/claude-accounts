#!/bin/bash
# Render a status line script against every fixture at several widths and with NO_COLOR.
# Usage: tests/preview.sh [script] [fixture.json ...]   (default: this repo's status line)
here=$(cd "$(dirname "$0")" && pwd)
script=${1:-$here/../statusline/statusline.sh}; shift
HOME=$(mktemp -d); trap 'rm -rf "$HOME"' EXIT; export HOME   # usage files go here, not in your real ~
fixtures=("$@"); [ ${#fixtures[@]} -eq 0 ] && fixtures=("$here"/fixtures/*.json)
for f in "${fixtures[@]}"; do
  echo "━━ ${f##*/}"
  for cols in 80 120 200; do
    out=$(COLUMNS=$cols "$script" < "$f" 2>&1)
    echo "── COLUMNS=$cols"
    printf '%s\n' "$out"
    COLS=$cols python3 -c '
import os, re, sys, unicodedata
cols = int(os.environ["COLS"])
for i, line in enumerate(sys.stdin.read().splitlines(), 1):
    plain = re.sub(r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*\x1b\\\\", "", line)
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in plain)
    if w > cols: print(f"   !! line {i} is {w} cols wide (> {cols})")
' <<<"$out"
  done
  echo "── NO_COLOR=1 (should contain no escape codes)"
  out=$(NO_COLOR=1 "$script" < "$f" 2>&1); printf '%s\n' "$out"
  [[ $out == *$'\e['* ]] && echo "   !! escape codes present despite NO_COLOR"
done
