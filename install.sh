#!/bin/bash
# claude-accounts installer: several Claude Code accounts on one Linux machine, sharing history,
# sessions, settings, plugins and skills, with a usage dashboard in the panel.
#
#   curl -fsSL https://raw.githubusercontent.com/anil0403/claude-accounts/main/install.sh | bash
#   ./install.sh                  (from a clone)
#
# Safe to run again: it updates in place. Options:
#   --no-tray         skip the panel icon and dashboard (and their GTK packages)
#   --no-statusline   leave Claude Code's status line setting alone
#   --no-share        don't turn on auto-sharing of project skills/plugins
#   --no-deps         don't install missing packages (only say what is missing)
#   --no-start        don't start the tray now
#   --uninstall       remove claude-accounts (accounts, their logins and your history are kept)
#   -h, --help
set -euo pipefail

REPO=anil0403/claude-accounts
REF=${CLAUDE_ACCOUNTS_REF:-main}
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
SHARE=$DATA/claude-accounts
BIN=$HOME/.local/bin
AUTOSTART=${XDG_CONFIG_HOME:-$HOME/.config}/autostart/claude-tray.desktop
SETTINGS=$HOME/.claude/settings.json
MARK="# added by claude-accounts"

if [ -t 1 ]; then B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' Z=$'\e[0m'; else B="" G="" Y="" R="" Z=""; fi
step() { echo "${B}==>${Z} $*"; }
ok()   { echo "  ${G}✓${Z} $*"; }
note() { echo "  ${Y}!${Z} $*"; }
die()  { echo "${R}error:${Z} $*" >&2; exit 1; }

tray=1 statusline=1 share=1 deps=1 start=1 uninstall=0
for arg in "$@"; do
  case $arg in
    --no-tray) tray=0 ;;
    --no-statusline) statusline=0 ;;
    --no-share) share=0 ;;
    --no-deps) deps=0 ;;
    --no-start) start=0 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,/^set -e/{/^set -e/d;s/^# \{0,1\}//;p}' "${BASH_SOURCE[0]}" 2>/dev/null ||
                 echo "See https://github.com/$REPO"; exit 0 ;;
    *) die "unknown option '$arg' (try --help)" ;;
  esac
done
[ "$(id -u)" != 0 ] || die "run this as your own user, not root (it installs into your home folder)"

# Write a JSON file through (not by renaming over it): accounts link to ~/.claude/settings.json.
json_edit() {  # $1 = file, rest = jq args
  local f=$1 tmp; shift
  tmp=$(mktemp)
  jq "$@" "$f" >"$tmp" && cat "$tmp" >"$f"
  rm -f "$tmp"
}

stop_tray() {  # this install's tray (and the one earlier versions put in ~/.local/bin)
  local pid args
  for pid in $(pgrep -f claude-tray 2>/dev/null); do
    args=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null) || continue
    case $args in
      *python*" $BIN/claude-tray "*|*python*" $SHARE/tray/claude-tray "*) kill "$pid" 2>/dev/null || true ;;
    esac
  done
}

# ---------------- uninstall ----------------

if [ "$uninstall" = 1 ]; then
  step "Removing claude-accounts"
  if [ -x "$SHARE/claude-acc" ] && command -v jq >/dev/null && [ -f "$SETTINGS" ]; then
    "$SHARE/claude-acc" share --no-auto >/dev/null 2>&1 || true
    ok "auto-share hook removed"
    if [ "$(jq -r '.statusLine.command // empty' "$SETTINGS")" = "$SHARE/statusline.sh" ]; then
      if [ -s "$SHARE/statusline-next" ]; then
        json_edit "$SETTINGS" --arg c "$(cat "$SHARE/statusline-next")" '.statusLine.command = $c'
        ok "status line: your previous one is back"
      else
        json_edit "$SETTINGS" 'del(.statusLine)'
        ok "status line setting removed"
      fi
    fi
  fi
  stop_tray
  rm -rf "$SHARE"
  for f in "$BIN/claude-acc" "$BIN/claude-tray"; do
    case $(readlink "$f" 2>/dev/null) in "$SHARE"/*) rm -f "$f" ;; esac
  done
  rm -f "$DATA/applications/claude-accounts.desktop" "$DATA/icons/hicolor/scalable/apps/claude-accounts.svg"
  if grep -qs "claude-tray" "$AUTOSTART"; then rm -f "$AUTOSTART"; fi
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$rc" ] && grep -qF "$MARK" "$rc"; then sed -i "/$MARK/d" "$rc"; fi
  done
  ok "program files, menu entry and autostart removed"
  echo
  echo "Kept: your accounts (~/.claude-accounts), their commands (claude-<name>), history and settings."
  echo "To remove an account as well, run before uninstalling: claude-acc rm <name>"
  exit 0
fi

# ---------------- files: from this clone, or downloaded ----------------

src=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  src=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi
if [ ! -f "$src/bin/claude-acc" ]; then
  step "Downloading claude-accounts ($REF)"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  url="https://github.com/$REPO/archive/$REF.tar.gz"
  if command -v curl >/dev/null; then curl -fsSL "$url" -o "$tmp/src.tar.gz" || die "could not download $url"
  elif command -v wget >/dev/null; then wget -qO "$tmp/src.tar.gz" "$url" || die "could not download $url"
  else die "need curl or wget to download $url"; fi
  mkdir "$tmp/src"
  tar -xzf "$tmp/src.tar.gz" -C "$tmp/src" --strip-components=1 || die "the download from $url is damaged"
  src=$tmp/src
  ok "downloaded"
fi

# ---------------- packages ----------------

step "Checking what is needed"
have_gi() {  # $1 = namespace, $2 = version
  python3 -c "import gi; gi.require_version('$1', '$2')" 2>/dev/null
}
desktop=$(echo "${XDG_CURRENT_DESKTOP:-}" | tr '[:upper:]' '[:lower:]')
missing=()  # generic names, mapped to packages below
command -v jq >/dev/null || missing+=(jq)
if [ "$tray" = 1 ]; then
  command -v python3 >/dev/null || missing+=(python)
  have_gi Gtk 3.0 || missing+=(gtk)
  python3 -c 'import gi; gi.require_foreign("cairo")' 2>/dev/null || missing+=(cairo)  # the frosted glass
  # Cinnamon, MATE and Xfce show the icon through XApp; other panels need an AppIndicator.
  if ! { [[ $desktop =~ cinnamon|mate|xfce ]] && have_gi XApp 1.0; }; then
    have_gi AyatanaAppIndicator3 0.1 || have_gi AppIndicator3 0.1 || missing+=(indicator)
  fi
  command -v notify-send >/dev/null || missing+=(notify)
fi

pkgs_for() {  # $1 = package manager, rest = generic names
  local pm=$1 n; shift
  for n in "$@"; do
    case $pm:$n in
      *:jq) echo jq ;;
      pacman:python) echo python ;;
      *:python) echo python3 ;;
      pacman:gtk) echo python-gobject gtk3 ;;
      apt:gtk) echo python3-gi gir1.2-gtk-3.0 ;;
      dnf:gtk) echo python3-gobject gtk3 ;;
      zypper:gtk) echo python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0 ;;
      pacman:cairo) echo python-cairo ;;
      apt:cairo) echo python3-gi-cairo ;;
      dnf:cairo|zypper:cairo) echo python3-cairo ;;
      pacman:indicator) echo libayatana-appindicator ;;
      apt:indicator) echo gir1.2-ayatanaappindicator3-0.1 ;;
      dnf:indicator) echo libayatana-appindicator-gtk3 ;;
      zypper:indicator) echo typelib-1_0-AyatanaAppIndicator3-0_1 ;;
      pacman:notify|dnf:notify) echo libnotify ;;
      apt:notify) echo libnotify-bin ;;
      zypper:notify) echo libnotify-tools ;;
    esac
  done
}

if [ ${#missing[@]} -gt 0 ]; then
  pm=""
  for p in pacman apt-get dnf zypper; do command -v $p >/dev/null && { pm=$p; break; }; done
  pm=${pm/apt-get/apt}
  read -ra pkgs <<<"$(pkgs_for "$pm" "${missing[@]}" | tr '\n' ' ')"
  if [ "$deps" = 1 ] && [ -n "$pm" ] && [ ${#pkgs[@]} -gt 0 ]; then
    sudo=""; command -v sudo >/dev/null && sudo=sudo
    echo "  installing: ${pkgs[*]} (with $pm; sudo may ask for your password)"
    case $pm in
      pacman) $sudo pacman -S --needed --noconfirm "${pkgs[@]}" ;;
      apt)    $sudo apt-get update -qq && $sudo apt-get install -y -qq "${pkgs[@]}" ;;
      dnf)    $sudo dnf install -y -q "${pkgs[@]}" ;;
      zypper) $sudo zypper --non-interactive install "${pkgs[@]}" ;;
    esac || note "some packages did not install; carrying on with what is there"
  else
    note "missing: ${missing[*]}${pm:+ (packages: ${pkgs[*]})}"
  fi
fi
command -v jq >/dev/null || die "jq is required. Install it with your package manager, then run this again."
ok "jq $(jq --version | sed 's/^jq-//')"
if [ "$tray" = 1 ] && ! have_gi Gtk 3.0; then
  note "no GTK 3 for Python: skipping the tray (install python3-gi / python-gobject, then run this again)"
  tray=0
fi
if command -v claude >/dev/null || [ -x "$BIN/claude" ]; then
  ok "Claude Code $( { claude --version 2>/dev/null || "$BIN/claude" --version 2>/dev/null; } | head -1)"
  have_claude=1
else
  note "Claude Code is not installed yet. Install it with:  curl -fsSL https://claude.ai/install.sh | bash"
  have_claude=0
fi

# ---------------- program files ----------------

step "Installing into $SHARE"
mkdir -p "$SHARE/tray" "$BIN"
install -m 755 "$src/bin/claude-acc" "$SHARE/claude-acc"
install -m 755 "$src/statusline/statusline.sh" "$SHARE/statusline.sh"
install -m 755 "$src/install.sh" "$SHARE/install.sh"
ln -sfn "$SHARE/claude-acc" "$BIN/claude-acc"
ok "claude-acc → $BIN/claude-acc"
if [ "$tray" = 1 ]; then
  install -m 755 "$src/tray/claude-tray" "$SHARE/tray/claude-tray"
  install -m 644 "$src/tray/core.py" "$SHARE/tray/core.py"
  rm -rf "$SHARE/tray/__pycache__"
  ln -sfn "$SHARE/tray/claude-tray" "$BIN/claude-tray"
  install -Dm 644 "$src/assets/claude-accounts.svg" "$DATA/icons/hicolor/scalable/apps/claude-accounts.svg"
  mkdir -p "$DATA/applications" "$(dirname "$AUTOSTART")"
  entry="[Desktop Entry]
Type=Application
Name=Claude Accounts
GenericName=Claude Code accounts and usage
Comment=Claude Code usage, accounts and sessions in the panel
Exec=$BIN/claude-tray
Icon=claude-accounts
Categories=Development;Utility;
Keywords=claude;ai;usage;accounts;
StartupNotify=false
Terminal=false"
  echo "$entry" >"$DATA/applications/claude-accounts.desktop"
  printf '%s\n%s\n' "${entry/Exec=$BIN\/claude-tray/Exec=$BIN/claude-tray --background}" \
    "X-GNOME-Autostart-enabled=true" >"$AUTOSTART"
  rm -f "$DATA/applications/claude-tray.desktop"  # from earlier versions
  ok "claude-tray → $BIN/claude-tray (app menu entry \"Claude Accounts\", starts at login)"
fi

# ---------------- status line ----------------

if [ "$statusline" = 1 ]; then
  step "Status line"
  mkdir -p "$HOME/.claude"
  [ -s "$SETTINGS" ] || echo '{}' >"$SETTINGS"
  jq -e . "$SETTINGS" >/dev/null 2>&1 || die "$SETTINGS is not valid JSON; fix it and run this again"
  [ -f "$SETTINGS.before-claude-accounts" ] || cp "$SETTINGS" "$SETTINGS.before-claude-accounts"
  cur=$(jq -r '.statusLine.command // empty' "$SETTINGS")
  cur_file=${cur/#\~/$HOME}; cur_file=${cur_file%% *}
  if [ "$cur" = "$SHARE/statusline.sh" ]; then
    ok "already set up"
  else
    if [ -n "$cur" ] && ! { [ -f "$cur_file" ] && grep -q 'claude-accounts/.usage' "$cur_file"; }; then
      # Someone else's status line: it keeps drawing, ours only records the usage.
      printf '%s\n' "$cur" >"$SHARE/statusline-next"
      ok "kept your status line ($cur); usage is recorded alongside it"
    else
      [ -z "$cur" ] || ok "replaced the earlier claude-accounts status line ($cur)"
      rm -f "$SHARE/statusline-next"
    fi
    json_edit "$SETTINGS" --arg c "$SHARE/statusline.sh" \
      '.statusLine = ((.statusLine // {}) + {type: "command", command: $c}) | .statusLine.padding //= 0'
    ok "statusLine → $SHARE/statusline.sh (backup: $SETTINGS.before-claude-accounts)"
  fi
fi

# ---------------- accounts ----------------

if [ "$have_claude" = 1 ]; then
  export PATH="$BIN:$PATH"
  step "Accounts"
  if [ "$share" = 1 ]; then
    "$BIN/claude-acc" share --auto >/dev/null && ok "skills and plugins added to a project are shared with every project and account"
  fi
  "$BIN/claude-acc" sync >/dev/null
  names=$(cd "$HOME/.claude-accounts" 2>/dev/null && for d in */; do d=${d%/}; if [[ $d =~ ^[a-z0-9][a-z0-9-]{0,19}$ ]]; then printf '%s ' "$d"; fi; done)
  if [ -n "$names" ]; then ok "accounts re-linked: $names"; else ok "no extra accounts yet"; fi
fi

# ---------------- PATH ----------------

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    line="export PATH=\"\$HOME/.local/bin:\$PATH\" $MARK"
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      if [ -f "$rc" ] && ! grep -qs '\.local/bin' "$rc"; then echo "$line" >>"$rc"; ok "added ~/.local/bin to PATH in $rc"; fi
    done
    if command -v fish >/dev/null && fish -c 'fish_add_path -q ~/.local/bin' 2>/dev/null; then ok "added ~/.local/bin to fish's PATH"; fi
    note "open a new terminal for the PATH change"
    ;;
esac

# ---------------- tray ----------------

if [ "$tray" = 1 ]; then
  stop_tray
  if [ "$start" = 1 ] && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    setsid -f "$BIN/claude-tray" --background >/dev/null 2>&1 </dev/null
    ok "tray started: click the ring in your panel"
  fi
fi

echo
echo "${G}${B}claude-accounts is installed.${Z}"
echo "  Add an account:   claude-acc add work      (then use it with: claude-work)"
echo "  See them all:     claude-acc list"
echo "  Check the setup:  claude-acc doctor"
echo "  Uninstall:        claude-acc uninstall"
