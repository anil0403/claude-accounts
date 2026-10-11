#!/bin/bash
# Install the Linux multi-account setup. Safe to re-run.
#
#   linux/install.sh               install claude-acc into ~/.local/bin and refresh existing accounts
#   linux/install.sh --statusline  also install the account-aware status line (backs up your old one)
#   linux/install.sh --tray        also install claude-tray (account picker in the panel), start it now and at login
#   linux/install.sh --share       also share skills/plugins added to one project with every project and account
#   linux/install.sh --uninstall   remove claude-acc (accounts are kept; remove them first with claude-acc rm)
#
# Needs: Claude Code (`claude`), jq, bash.
set -euo pipefail
cd "$(dirname "$0")"
BIN="$HOME/.local/bin"
settings="$HOME/.claude/settings.json"

if [ "${1:-}" = --uninstall ]; then
  [ -x "$BIN/claude-acc" ] && "$BIN/claude-acc" share --no-auto >/dev/null 2>&1 || true
  rm -f "$BIN/claude-acc" "$BIN/claude-tray" "$HOME/.local/share/applications/claude-tray.desktop" \
        "$HOME/.config/autostart/claude-tray.desktop"
  pkill -f "^[^ ]*python[0-9.]* [^ ]*/claude-tray$" 2>/dev/null || true
  echo "Removed claude-acc and claude-tray. Accounts in ~/.claude-accounts and their claude-<name> launchers were kept."
  exit 0
fi

command -v jq >/dev/null || { echo "jq is missing. Arch: sudo pacman -S jq   Debian/Ubuntu: sudo apt install jq"; exit 1; }
command -v claude >/dev/null || { echo "Claude Code isn't on PATH. Install it, log in once with: claude"; exit 1; }

echo "• claude-acc → $BIN/claude-acc"
mkdir -p "$BIN"
install -m 755 ../shared/claude-acc "$BIN/claude-acc"

for arg in "$@"; do case $arg in --statusline) want_sl=1 ;; --tray) want_tray=1 ;; --share) want_share=1 ;; esac; done

if [ -n "${want_sl:-}" ]; then
  echo "• status line → ~/.claude/statusline.sh"
  mkdir -p "$HOME/.claude"
  if [ -f "$HOME/.claude/statusline.sh" ] && ! cmp -s "$HOME/.claude/statusline.sh" ../shared/statusline.sh; then
    cp "$HOME/.claude/statusline.sh" "$HOME/.claude/statusline.sh.bak-$(date +%Y%m%d-%H%M%S)"
  fi
  install -m 755 ../shared/statusline.sh "$HOME/.claude/statusline.sh"
  [ -f "$settings" ] || echo '{}' > "$settings"
  old=$(jq -c '.statusLine // empty' "$settings")
  [ -n "$old" ] && echo "  (previous statusLine setting was: $old)"
  tmp=$(mktemp)
  # Write through the file (not mv) so accounts that symlink settings.json keep pointing at it.
  jq '.statusLine = {type: "command", command: "~/.claude/statusline.sh", padding: 0}' "$settings" > "$tmp"
  cat "$tmp" > "$settings"; rm -f "$tmp"
fi

if [ -n "${want_tray:-}" ]; then
  python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null ||
    { echo "claude-tray needs python-gobject and gtk3. Arch: sudo pacman -S python-gobject gtk3"; exit 1; }
  echo "• claude-tray → $BIN/claude-tray (app menu + autostart)"
  install -m 755 claude-tray "$BIN/claude-tray"
  entry="[Desktop Entry]
Type=Application
Name=Claude Accounts
Comment=Pick a Claude account and open it in a terminal
Exec=$BIN/claude-tray
Icon=utilities-terminal
Categories=Development;
X-GNOME-Autostart-enabled=true"
  mkdir -p "$HOME/.local/share/applications" "$HOME/.config/autostart"
  echo "$entry" > "$HOME/.local/share/applications/claude-tray.desktop"
  echo "$entry" > "$HOME/.config/autostart/claude-tray.desktop"
  # Restart so a reinstall runs the new version.
  pkill -f "^[^ ]*python[0-9.]* [^ ]*/claude-tray$" 2>/dev/null || true
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && setsid -f "$BIN/claude-tray" >/dev/null 2>&1 < /dev/null
fi

if [ -n "${want_share:-}" ]; then
  echo "• auto-share of project skills/plugins (SessionStart hook in ~/.claude/settings.json)"
  "$BIN/claude-acc" share --auto
fi

# Re-link shared files and rewrite launchers for accounts that already exist.
[ -d "$HOME/.claude-accounts" ] && "$BIN/claude-acc" sync >/dev/null && echo "• refreshed existing accounts"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "note: $BIN is not on PATH in this shell."
     echo "      bash: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
     echo "      fish: fish_add_path ~/.local/bin" ;;
esac
if [ -d "$HOME/.config/fish" ] && ! grep -rqs 'local/bin' "$HOME/.config/fish"; then
  echo "note: if you use fish, run once: fish_add_path ~/.local/bin"
fi
echo "Done. Add an account with:  claude-acc add <name>     See all with:  claude-acc list"
