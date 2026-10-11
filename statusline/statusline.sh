#!/bin/bash
# Claude Code status line (three lines, after the "claude-statusline" look):
#   Opus 5.5 │ ◔ 23% │ my-project (main) │ ● thinking
#   current  ●●●○○○○○○○ 28%   ⟳ 7:00pm
#   weekly   ●●●●●●●●○○ 79%   ⟳ mar 10, 10:00am
# ▲ after a % = using that limit faster than time is passing (pace).
# Fits $COLUMNS (drops thinking first; shortens the folder). NO_COLOR=1 disables colour.
# Rate limits come from Claude Code's own input and are saved to
# ~/.claude-accounts/.usage/<account>.json ("main" for plain `claude`). The tray app and
# `claude-acc list` read that file, so they rarely need the usage API (which 429s easily); the app
# also writes API answers there, which is this script's fallback when Claude Code sends no limits
# yet. Never calls the API. Line 1 names the account (◉ main / ◉ <name>) once extra accounts from
# claude-acc exist (launchers set CLAUDE_ACCOUNT).
# 256-colour only.
# One process spawn only (jq), since it runs on every refresh:
# the git branch is read from .git/HEAD with bash builtins, and the time comes from jq's `now`.
input=$(cat)
acct=${CLAUDE_ACCOUNT:-}
usage="$HOME/.claude-accounts/.usage"
if [ -z "$acct" ]; then
  # Plain `claude` = main. Only label it when other accounts exist.
  for d in "$HOME/.claude-accounts"/*/; do [ -d "$d" ] && { acct=main; break; }; done
fi
live="$usage/${acct:-main}.json"
cache=$live
[ -f "$cache" ] || cache=/dev/null

branch=""
if [[ $input =~ \"current_dir\":[[:space:]]*\"([^\"]*)\" ]]; then
  d=${BASH_REMATCH[1]}
  while [ -n "$d" ]; do
    if [ -f "$d/.git/HEAD" ]; then
      read -r head < "$d/.git/HEAD"
      [[ $head == "ref: refs/heads/"* ]] && branch=${head#ref: refs/heads/} || branch=${head:0:7}
      break
    fi
    [[ $d == */* ]] || break  # stop at / instead of looping forever
    d=${d%/*}
  done
fi

out=$(jq -r --arg branch "$branch" --arg acct "$acct" --arg nocolor "${NO_COLOR:-}" \
      --argjson cols "${COLUMNS:-120}" --slurpfile c "$cache" '
  (now | floor) as $now
  | def E(c): if $nocolor != "" then "" else "\u001b[" + c + "m" end;
  def R: E("0");
  def fg(n): E("38;5;\(n)");
  def DIM: fg(244); def SEP: fg(240) + " │ " + R;
  def heat(p): if p >= 90 then 203 elif p >= 70 then 221 elif p >= 50 then 214 else 77 end;
  def vis: gsub("\u001b\\[[0-9;]*m"; "") | length;
  def two: tostring | if length < 2 then "0" + . else . end;
  def pie(p): if p < 13 then "○" elif p < 38 then "◔" elif p < 63 then "◑" elif p < 88 then "◕" else "●" end;
  def dots(p; n): ([p, 100] | min | [., 0] | max | . * n / 100 | round | if . == 0 and p > 0 then 1 else . end) as $f
    | fg(heat(p)) + ("●" * $f) + ("○" * (n - $f)) + R;

  # Reset time as a clock: "7:00pm" if today, else "mar 10, 10:00am".
  def clock(t):
    (t | localtime) as $l | ($now | localtime) as $n
    | (($l[3] % 12) | if . == 0 then 12 else . end) as $h
    | "\($h):\($l[4] | two)\(if $l[3] < 12 then "am" else "pm" end)" as $hm
    | if $l[0:3] == $n[0:3] then $hm
      else (["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"][$l[1]]) + " \($l[2]), " + $hm end;

  def row(name; p; reset; win):
    if p == null then empty else
      (if (reset // 0) > 0 then ((win - (reset - $now)) * 100 / win) else null end) as $pace
      | fg(252) + name + R + "  " + dots(p; 10) + " " + fg(heat(p)) + ("\(p)%" | . + " " * (4 - length)) + R
        + (if $pace != null and p > $pace + 5 then fg(heat(p)) + "▲" + R else " " end)
        + (if (reset // 0) > $now then "  " + DIM + "⟳ " + R + fg(252) + clock(reset) + R else "" end)
    end;

  # Join segments; while too wide, drop the highest-priority-number droppable one (p > 0).
  def fit(sep):
    if (map(.t) | join(sep) | vis) <= $cols or (map(select(.p > 0)) | length) == 0
    then map(.t) | join(sep)
    else (max_by(.p)) as $m | (. - [$m]) | fit(sep) end;

  . as $in
  | ($c[0] // {}) as $cache
  | ($in.context_window.used_percentage // 0) as $cp
  | ($in.rate_limits.five_hour.used_percentage // $cache.session.pct) as $s5
  | ($in.rate_limits.five_hour.resets_at // $cache.session.resets_at) as $s5t
  | ($in.rate_limits.seven_day.used_percentage // $cache.weekly.pct) as $s7
  | ($in.rate_limits.seven_day.resets_at // $cache.weekly.resets_at) as $s7t
  | (($in.workspace.current_dir // $in.cwd // "") | split("/") | last // "") as $folder
  | ([$cols - 30, 12] | max) as $maxdir

  | ([ { p: 0, t: (fg(75) + ($in.model.display_name // "Claude") + R) },
       (if $acct != "" then { p: 0, t: (fg(213) + "◉ " + $acct + R) } else empty end),
       { p: 0, t: (fg(heat($cp)) + pie($cp) + " \($cp)%" + R) },
       { p: 0, t: (fg(80) + (if ($folder | length) > $maxdir then $folder[0:$maxdir - 1] + "…" else $folder end) + R
                   + (if $branch != "" then fg(79) + " (" + $branch + ")" + R else "" end)) },
       (if $in.thinking.enabled then { p: 2, t: (fg(240) + "●" + R + " " + DIM + "thinking" + R) } else empty end)
     ] | fit(SEP)) as $line1
  # First output line: feed for the tray app (empty when Claude Code sent no rate limits).
  | (if ($in.rate_limits.five_hour.used_percentage // null) != null then
       { updated: $now,
         session: { pct: ($in.rate_limits.five_hour.used_percentage | round), resets_at: ($in.rate_limits.five_hour.resets_at // 0) },
         weekly: { pct: (($in.rate_limits.seven_day.used_percentage // 0) | round), resets_at: ($in.rate_limits.seven_day.resets_at // 0) } }
       | tojson else "" end),
    ([$line1, row("current"; $s5; $s5t; 18000), row("weekly "; $s7; $s7t; 604800)] | join("\n"))
' <<<"$input")
feed=${out%%$'\n'*}
if [ -n "$feed" ]; then
  [ -d "$usage" ] || mkdir -p "$usage" 2>/dev/null
  # Write-then-rename so readers never see half a file.
  printf '%s\n' "$feed" 2>/dev/null > "$live.$$" && mv -f "$live.$$" "$live" 2>/dev/null
fi
printf '%s\n' "${out#*$'\n'}"
