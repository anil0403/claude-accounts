#!/bin/bash
# Install, use and uninstall in a throwaway HOME, with a stand-in `claude`. No packages, no tray start.
# shellcheck disable=SC2088  # "~/mine.sh" is the literal setting, not a path to expand
# Usage: tests/install-test.sh
set -uo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
HOME=$(mktemp -d); trap 'rm -rf "$HOME"' EXIT; export HOME
unset XDG_DATA_HOME XDG_CONFIG_HOME CLAUDE_CONFIG_DIR CLAUDE_ACCOUNT
mkdir -p "$HOME/stub" "$HOME/.claude"
printf '#!/bin/sh\ncase "$*" in --version) echo "9.9.9 (Claude Code)" ;; *) echo "{\\"loggedIn\\":false}" ;; esac\n' >"$HOME/stub/claude"
chmod +x "$HOME/stub/claude"
export PATH="$HOME/stub:$HOME/.local/bin:$PATH"
fails=0
check() {  # description, command...
  local what=$1; shift
  if "$@" >/dev/null 2>&1; then echo "ok   $what"; else echo "FAIL $what"; fails=$((fails + 1)); fi
}
share=$HOME/.local/share/claude-accounts
settings=$HOME/.claude/settings.json

# someone's own status line, which has to keep drawing
printf '#!/bin/sh\necho mine\n' >"$HOME/mine.sh"; chmod +x "$HOME/mine.sh"
echo '{"statusLine":{"type":"command","command":"~/mine.sh"},"theme":"dark"}' >"$settings"

check "install runs" bash "$repo/install.sh" --no-deps --no-start
check "install runs again" bash "$repo/install.sh" --no-deps --no-start
check "claude-acc is linked" test "$(readlink "$HOME/.local/bin/claude-acc")" = "$share/claude-acc"
check "status line points at the kit" test "$(jq -r .statusLine.command "$settings")" = "$share/statusline.sh"
check "other settings are kept" test "$(jq -r .theme "$settings")" = dark
check "the old status line is chained" grep -qx '~/mine.sh' "$share/statusline-next"
check "the chained one draws" test "$("$share/statusline.sh" <"$repo/tests/fixtures/mid.json")" = mine
check "usage is recorded" test -s "$HOME/.claude-accounts/.usage/main.json"
check "auto-share hook added" jq -e '[.hooks.SessionStart[].hooks[].command] | any(test("claude-acc"))' "$settings"

check "add an account" claude-acc add work --no-login
check "its sessions are shared" test "$(realpath "$HOME/.claude-accounts/work/projects")" = "$(realpath "$HOME/.claude/projects")"
check "its settings are the shared ones" test "$(realpath "$HOME/.claude-accounts/work/settings.json")" = "$(realpath "$settings")"
check "its command exists" test -x "$HOME/.local/bin/claude-work"
check "its usage is recorded apart" bash -c "CLAUDE_ACCOUNT=work '$share/statusline.sh' <'$repo/tests/fixtures/mid.json' && test -s '$HOME/.claude-accounts/.usage/work.json'"
check "doctor passes" claude-acc doctor
check "help works without claude" env PATH=/usr/bin:/bin "$share/claude-acc" help

check "uninstall runs" claude-acc uninstall
check "the old status line is back" test "$(jq -r .statusLine.command "$settings")" = "~/mine.sh"
check "the hook is gone" bash -c "! grep -q claude-acc '$settings'"
check "program files are gone" test ! -e "$share" -a ! -e "$HOME/.local/bin/claude-acc"
check "accounts are kept" test -d "$HOME/.claude-accounts/work" -a -x "$HOME/.local/bin/claude-work"

echo
[ "$fails" = 0 ] && echo "all passed" || echo "$fails failed"
exit $((fails > 0))
