#!/bin/bash
# Open the dashboard on made-up data (three accounts, a few projects and sessions, two running),
# in a throwaway HOME and on a private D-Bus, so it runs beside your real tray. For screenshots
# and for trying layout changes. Close the window (Esc) and press Enter here to clean up.
# Usage: tests/demo.sh [--shot file.png]
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
real_cache=${XDG_CACHE_HOME:-$HOME/.cache}
real_home=$HOME
HOME=$(mktemp -d); export HOME
# your GTK theme and icons, so it looks as it does for you
mkdir -p "$HOME/.config" "$HOME/.local/share"
for d in .themes .icons .config/gtk-3.0 .local/share/themes .local/share/icons; do
  [ -e "$real_home/$d" ] && ln -s "$real_home/$d" "$HOME/$d"
done
pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; rm -rf "$HOME"; }
trap cleanup EXIT
unset XDG_DATA_HOME XDG_CONFIG_HOME XDG_CACHE_HOME CLAUDE_CONFIG_DIR CLAUDE_ACCOUNT
now=$(date +%s)
C=$HOME/.claude A=$HOME/.claude-accounts
mkdir -p "$C/projects" "$C/plugins" "$C/skills/release-notes" "$A/work" "$A/side" "$A/.usage" "$HOME/stub"

# who each account is logged in as
cat >"$HOME/stub/claude" <<'EOF'
#!/bin/sh
case "${CLAUDE_ACCOUNT:-main}" in
  main) echo '{"loggedIn":true,"email":"alex@example.com","subscriptionType":"max"}' ;;
  work) echo '{"loggedIn":true,"email":"alex@company.example","subscriptionType":"team"}' ;;
  *)    echo '{"loggedIn":true,"email":"alex.side@example.org","subscriptionType":"pro"}' ;;
esac
EOF
chmod +x "$HOME/stub/claude"
export PATH="$HOME/stub:$PATH"

usage() {  # account, 5h %, minutes to reset, 7d %, hours to reset, minutes since last use
  printf '{"updated":%d,"session":{"pct":%d,"resets_at":%d},"weekly":{"pct":%d,"resets_at":%d}}' \
    $((now - $6 * 60)) "$2" $((now + $3 * 60)) "$4" $((now + $5 * 3600)) >"$A/.usage/$1.json"
}
usage main 64 95 41 70 1
usage work 22 210 73 30 25
usage side 5 280 9 140 600

# projects, sessions with titles, and today's replies for the Today card
i=0
for p in api-server web-app docs-site infra; do
  mkdir -p "$HOME/code/$p" "$C/projects/-code-$p"
done
for s in "api-server:Fix the flaky auth test" "web-app:Dark mode for settings" "api-server:Rate limiter design" \
         "docs-site:Migrate docs to MDX" "infra:Terraform state cleanup" "web-app:Speed up the build"; do
  p=${s%%:*} title=${s#*:} i=$((i + 1)) id="00000000-0000-4000-8000-00000000000$i"
  ts=$(( (now - i * 2700) * 1000 ))
  printf '{"display":"%s","project":"%s","sessionId":"%s","timestamp":%d}\n' "$title" "$HOME/code/$p" "$id" "$ts" >>"$C/history.jsonl.tmp"
  t=$(date -u -d "@$((now - i * 2700))" +%Y-%m-%dT%H:%M:%S.000Z)
  {
    printf '{"type":"user","timestamp":"%s","message":{"content":"%s"}}\n' "$t" "$title"
    for m in claude-opus-5-5 claude-opus-5-5 claude-sonnet-5-5; do
      printf '{"type":"assistant","timestamp":"%s","requestId":"r%s%s","message":{"id":"m%s%s","model":"%s","content":[{"type":"tool_use","id":"t%s%s","name":"Write","input":{"content":"%s"}}],"usage":{"input_tokens":3000,"output_tokens":4500,"cache_read_input_tokens":90000}}}\n' \
        "$t" "$i" "$m" "$i" "$m" "$m" "$i" "$m" "$(printf 'x\\n%.0s' {1..40})"
    done
    printf '{"type":"ai-title","aiTitle":"%s","sessionId":"%s"}\n' "$title" "$id"
  } >"$C/projects/-code-$p/$id.jsonl"
done
tac "$C/history.jsonl.tmp" >"$C/history.jsonl" && rm "$C/history.jsonl.tmp"

# two sessions running now
for s in "main:1:busy" "work:2:idle"; do
  acct=${s%%:*} n=$(echo "$s" | cut -d: -f2) st=${s##*:}
  sleep 3600 & pid=$!; pids+=("$pid")
  dir=$C/sessions; [ "$acct" = main ] || dir=$A/$acct/sessions
  mkdir -p "$dir"
  start=$(awk '{print $22}' "/proc/$pid/stat")
  printf '{"pid":%d,"procStart":"%s","sessionId":"00000000-0000-4000-8000-00000000000%s","cwd":"%s","status":"%s","name":"%s","statusUpdatedAt":%d}\n' \
    "$pid" "$start" "$n" "$HOME/code/$( [ "$n" = 1 ] && echo api-server || echo web-app)" "$st" \
    "$( [ "$n" = 1 ] && echo "Fix the flaky auth test" || echo "Dark mode for settings")" "$((now * 1000))" >"$dir/$pid.json"
done

echo '{"plugins":{"code-review@official":[{"version":"1.4.0","installPath":"/tmp"}],"commit-helper@official":[{"version":"0.9.2","installPath":"/tmp"}]}}' \
  >"$C/plugins/installed_plugins.json"
echo '{"enabledPlugins":{"code-review@official":true}}' >"$C/settings.json"
printf -- '---\ndescription: Draft release notes from the merged pull requests\n---\n' >"$C/skills/release-notes/SKILL.md"

# killing dbus-run-session would leave the tray running, so note the tray's own pid
dbus-run-session -- bash -c 'echo $$ >"$HOME/tray.pid"; exec env CLAUDE_TRAY_CHILD=1 python3 "$0"' \
  "$repo/tray/claude-tray" 2>"$real_cache/claude-tray-demo.log" &
pids+=("$!")
for _ in $(seq 20); do [ -s "$HOME/tray.pid" ] && break; sleep 0.1; done
pids+=("$(cat "$HOME/tray.pid")")
if [ "${1:-}" = --shot ]; then
  out=$(realpath -m "${2:?usage: tests/demo.sh --shot file.png}")
  for _ in $(seq 50); do w=$(xdotool search --onlyvisible --name '^Claude Accounts$' 2>/dev/null | tail -1) && [ -n "$w" ] && break; sleep 0.2; done
  sleep 1
  import -window "$w" "$out" && echo "saved $out"
  eval "$(xdotool getwindowgeometry --shell "$w")"
  xdotool mousemove $((X + 120)) $((Y + 120)) click 1  # the first account card
  sleep 0.8
  import -window "$w" "${out%.png}-account.png" && echo "saved ${out%.png}-account.png"
else
  read -rp "Dashboard open (click away or Esc closes it; run again to reopen). Enter to quit. " _
fi
