#!/usr/bin/env python3
# claude-tray (Linux) — Claude usage and accounts in the panel. Built on claude-acc.
#
# Icon: a ring with the 5-hour usage of the account you used last (amber at 70%, red at 90%, grey
# when the numbers are over 30 minutes old). On Cinnamon the panel also shows text: pick what in
# Panel → (5-hour %, weekly %, account name, today's cost, reset countdown).
# Click it for the menu:
#  - each account: email, plan, when it was last used, and a 5-hour and a weekly bar. The tick on a
#    bar is where you'd be if you spent evenly; "over pace" means you'll hit the limit early.
#    Its submenu opens Claude in your home folder, a recent project, any folder, or the last session,
#    and logs the account in or removes it.
#  - Today: API-equivalent cost and activity across all accounts, from the session logs.
#  - Add account…: name (+ optional email), then the browser login. No terminal and no code to
#    paste: the login finishes by itself when you approve it in the browser.
#
# Data:
#  - usage: ~/.claude-accounts/.usage/<account>.json, written by shared/statusline.sh while you work.
#    When an account's numbers are over 5 minutes old the tray asks the usage API with that
#    account's own login (.credentials.json) and saves the answer to the same file. It backs off
#    after a 429. Turn this off under "Check usage via API when idle".
#  - login and plan: `claude auth status --json` per account, at start, every 30 min and on Refresh
#  - Today: ~/.claude/projects/**/*.jsonl (shared by all accounts), read incrementally every minute
#  - recent folders: the "project" field of ~/.claude/history.jsonl
#  - settings: ~/.config/claude-tray/settings.json
#
# Run:   claude-tray            (linux/install.sh --tray installs it and starts it at login)
# Test:  claude-tray --test     prints what the menu would show, no UI and no API calls
# Terminal: kitty, gnome-terminal, konsole, xfce4-terminal, alacritty, wezterm, foot, tilix or
# xterm, whichever is found first. Set CLAUDE_TRAY_TERMINAL to pick one.
import glob
import itertools
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime

HOME = os.path.expanduser("~")
MAIN = os.path.join(HOME, ".claude")
ROOT = os.path.join(HOME, ".claude-accounts")
USAGE = os.path.join(ROOT, ".usage")
BIN = os.path.join(HOME, ".local", "bin")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "claude-tray")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "claude-tray")
APP_ID = "io.github.claude_code_setup.ClaudeTray"
USAGE_PAGE = "https://claude.ai/settings/usage"
USAGE_API = "https://api.anthropic.com/api/oauth/usage"
SESSION_LEN = 5 * 3600
WEEK_LEN = 7 * 86400
STALE = 1800          # numbers older than this are drawn grey
API_EVERY = 300       # ask the API only when an account's numbers are older than this
BACKOFF = 600         # after a 429 with no Retry-After; doubles each time, max 1 h
LOGIN_TIMEOUT = 600

# Desktop sessions often start without ~/.local/bin on PATH; claude-acc and the launchers live there.
os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")


# ---------- settings ----------

DEFAULTS = {"label": "session", "label_account": True, "label_cost": False, "label_reset": False,
            "hide_emails": False, "api": True, "alerts": True, "pinned": [], "pinned_sessions": []}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(os.path.join(CONFIG, "settings.json")) as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    os.makedirs(CONFIG, exist_ok=True)
    write_json(os.path.join(CONFIG, "settings.json"), s)


def write_json(path, obj):
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# ---------- accounts ----------

def accounts():
    names = []
    if os.path.isdir(ROOT):
        names = sorted(n for n in os.listdir(ROOT)
                       if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", n) and os.path.isdir(os.path.join(ROOT, n)))  # skips <name>.lock dirs
    return ["main"] + names


def account_dir(name):
    return MAIN if name == "main" else os.path.join(ROOT, name)


def account_env(name):
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_ACCOUNT", None)
    if name != "main":
        env["CLAUDE_CONFIG_DIR"] = account_dir(name)
        env["CLAUDE_ACCOUNT"] = name
    return env


def identity(name):
    """{'email', 'plan'} or {'error'}; `claude auth status` exits 1 when logged out but still prints JSON."""
    claude = shutil.which("claude")
    if not claude:
        return {"error": "claude not found"}
    try:
        out = subprocess.run([claude, "auth", "status", "--json"], env=account_env(name),
                             capture_output=True, text=True, timeout=30).stdout
        j = json.loads(out)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return {"error": "?"}
    if not j.get("loggedIn"):
        return {"error": "not logged in"}
    return {"email": j.get("email") or "logged in", "plan": (j.get("subscriptionType") or "").capitalize()}


def mask_email(e):
    """Masked: first 3 characters + *@*.tld."""
    if not e or "@" not in e[1:]:
        return e
    local, domain = e.rsplit("@", 1)
    vis = local[:max(1, min(3, len(local)))]
    dot = domain.rfind(".")
    if domain.startswith("[") or dot <= 0 or dot == len(domain) - 1:
        return f"{vis}*@*"
    return f"{vis}*@*.{domain[dot + 1:]}"


# ---------- usage ----------

def usage(name):
    try:
        with open(os.path.join(USAGE, name + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def last_used(u):
    """When Claude Code last reported for this account (API checks don't count as use)."""
    if not u:
        return 0
    return u.get("live", 0) if u.get("source") == "api" else u.get("updated", 0)


def window(u, key, now):
    """{'pct', 'resets'} for one usage window; 0% once it has reset; None if unknown."""
    w = (u or {}).get(key)
    if not isinstance(w, dict) or "pct" not in w:
        return None
    r = w.get("resets_at") or 0
    if r and r <= now:
        return {"pct": 0, "resets": None}
    return {"pct": max(0, min(100, int(round(w["pct"])))), "resets": r or None}


def pace(w, length, now):
    """Percent of the window that has passed: where usage would be if spent evenly."""
    if not w or not w["resets"]:
        return None
    left = w["resets"] - now
    return (length - left) / length * 100 if 0 < left <= length else None


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def fetch_api(name):
    """Ask the usage API with the account's own login. Returns (feed, error, http code, retry-after)."""
    try:
        with open(os.path.join(account_dir(name), ".credentials.json")) as f:
            c = json.load(f)
        token = (c.get("claudeAiOauth") or c).get("accessToken")
    except (OSError, ValueError, AttributeError):
        token = None
    if not token:
        return None, "no login token", 0, None
    req = urllib.request.Request(USAGE_API, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json", "User-Agent": "claude-tray/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After")
        return None, {401: "token expired", 429: "rate-limited"}.get(e.code, f"HTTP {e.code}"), e.code, \
            float(ra) if ra and ra.isdigit() else None
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, str(getattr(e, "reason", e)), 0, None

    def win(b):
        b = b or {}
        return {"pct": round(b.get("utilization") or 0), "resets_at": int(parse_iso(b.get("resets_at")))}
    return {"updated": int(time.time()), "session": win(j.get("five_hour")),
            "weekly": win(j.get("seven_day")), "source": "api"}, None, 200, None


def save_api_feed(name, feed):
    """Write an API answer into the status line's file unless Claude Code wrote something newer."""
    old = usage(name) or {}
    if old.get("updated", 0) >= feed["updated"]:
        return
    feed["live"] = last_used(old)
    os.makedirs(USAGE, exist_ok=True)
    write_json(os.path.join(USAGE, name + ".json"), feed)


# ---------- today's cost and activity ----------

# $ per million tokens: input, output, cache read. Cache writes are 1.25x input (5 min) / 2x (1 h).
# Longest matching prefix wins.
PRICES = [
    ("claude-fable-5-1", 10, 50, 0.25), ("claude-mythos-5-1", 10, 50, 0.25),
    ("claude-fable-5", 10, 50, 1), ("claude-mythos-5", 10, 50, 1),
    ("claude-opus-5-5", 4, 20, 0.20), ("claude-opus-5", 5, 25, 0.50), ("claude-opus-4", 5, 25, 0.50),
    ("claude-sonnet-5", 2, 10, 0.20), ("claude-sonnet-4", 3, 15, 0.30),
    ("claude-haiku-5-5", 0.10, 0.50, 0.01), ("claude-haiku-4", 1, 5, 0.10),
]


def price(model):
    m = [p for p in PRICES if model.startswith(p[0])]
    return max(m, key=lambda p: len(p[0]))[1:] if m else None


def family(model):
    for f in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if f in model:
            return "Fable" if f == "mythos" else f.capitalize()
    return None


def day_start():
    return time.mktime(date.today().timetuple())


class Stats:
    """Counting rules follow ccusage: messages deduped by message.id:requestId keeping the
    largest-output copy, active time = 10-minute-gap periods + 2 min each, lines = net lines added by
    Write/Edit. Files only grow, so each is read once and then only from where we stopped."""

    def __init__(self):
        self.reset(day_start())

    def reset(self, day):
        self.day, self.cost, self.turns, self.lines = day, 0.0, 0, 0
        self.replies, self.times, self.best, self.offsets = {}, [], {}, {}

    def update(self):
        d = day_start()
        if d != self.day:
            self.reset(d)
        for path in glob.iglob(os.path.join(MAIN, "projects", "**", "*.jsonl"), recursive=True):
            try:
                st = os.stat(path)
            except OSError:
                continue
            start = self.offsets.get(path, 0)
            if st.st_mtime < self.day or st.st_size <= start:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    data = f.read()
            except OSError:
                continue
            end = data.rfind(b"\n")
            if end < 0:
                continue
            for line in data[:end + 1].splitlines():
                if b'"type":"assistant"' in line or b'"type":"user"' in line:
                    self.parse(line)
            self.offsets[path] = start + end + 1
        return {"cost": self.cost, "turns": self.turns, "lines": self.lines,
                "active": self.active_minutes(), "replies": dict(self.replies)}

    def parse(self, line):
        try:
            j = json.loads(line)
        except ValueError:
            return
        kind = j.get("type")
        ts = parse_iso(j.get("timestamp"))
        if kind not in ("assistant", "user") or ts < self.day:
            return
        self.times.append(ts)
        msg = j.get("message") or {}
        if kind == "user":
            if j.get("isMeta") or j.get("isSidechain"):
                return
            c = msg.get("content")
            if isinstance(c, str):
                if c and not c.startswith("<"):
                    self.turns += 1
            elif isinstance(c, list) and not any(b.get("type") == "tool_result" for b in c if isinstance(b, dict)):
                self.turns += 1
            return
        mid = f"{msg.get('id', '')}:{j.get('requestId', '')}"
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if b.get("name") == "Write":
                    self.lines += count_lines(inp.get("content"))
                elif b.get("name") == "Edit":
                    self.lines += max(0, count_lines(inp.get("new_string")) - count_lines(inp.get("old_string")))
        model = msg.get("model") or ""
        p, u = price(model), msg.get("usage")
        if not p or not isinstance(u, dict):
            return

        def n(k, d=u):
            v = d.get(k) if isinstance(d, dict) else None
            return float(v) if isinstance(v, (int, float)) else 0.0
        w1h = n("ephemeral_1h_input_tokens", u.get("cache_creation"))
        w5m = n("cache_creation_input_tokens") - w1h
        cost = (n("input_tokens") * p[0] + n("output_tokens") * p[1] + n("cache_read_input_tokens") * p[2]
                + w5m * p[0] * 1.25 + w1h * p[0] * 2) / 1_000_000
        # A message is logged again as it streams; keep the copy with the most output.
        old = self.best.get(mid)
        if old:
            if n("output_tokens") <= old[0]:
                return
            self.cost += cost - old[1]
        else:
            self.cost += cost
            f = family(model)
            if f:
                self.replies[f] = self.replies.get(f, 0) + 1
        self.best[mid] = (n("output_tokens"), cost)

    def active_minutes(self):
        t = sorted(self.times)
        if not t:
            return 0
        start = end = t[0]
        total = 0.0
        for x in t[1:]:
            if x - end <= 600:
                end = x
            else:
                total += end - start + 120
                start = end = x
        total += end - start + 120
        return max(1, int(total / 60))


def count_lines(s):
    return s.count("\n") + 1 if isinstance(s, str) and s else 0


# ---------- formatting ----------

def dur(secs):
    secs = max(0, int(secs))
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{max(m, 1)}m"


def ago(t, now):
    return "just now" if now - t < 60 else f"{dur(now - t)} ago"


def clock(t):
    lt = time.localtime(t)
    hm = time.strftime("%-I:%M %p", lt)
    return hm if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d") else time.strftime("%a ", lt) + hm


def active_text(m):
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def history():
    """The last prompts of the shared prompt history, newest first."""
    try:
        with open(os.path.join(MAIN, "history.jsonl"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 512 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if isinstance(j, dict):
            out.append(j)
    return out


def recent_folders(limit=30):
    """Most recent project folders from the shared prompt history, newest first."""
    seen, out = set(), []
    for j in history():
        p = j.get("project")
        if p and p not in seen:
            seen.add(p)
            if os.path.isdir(p):
                out.append(p)
                if len(out) >= limit:
                    break
    return out


TITLES = {}  # transcript path -> (its mtime, the session's title)


def transcripts(sid):
    """A session's transcript file (one, in its project's folder; none once it is deleted)."""
    return glob.glob(os.path.join(MAIN, "projects", "*", glob.escape(sid) + ".jsonl"))


def session_title(sid):
    """A session's name: the one you gave it, else Claude's. From the end of its transcript."""
    for path in transcripts(sid):
        try:
            mtime = os.stat(path).st_mtime
            if TITLES.get(path, (0,))[0] != mtime:
                found = {}
                with open(path, "rb") as f:
                    f.seek(0, 2)
                    f.seek(max(0, f.tell() - 256 * 1024))
                    for line in f.read().decode("utf-8", "replace").splitlines():
                        if '-title"' in line:
                            try:
                                found.update(json.loads(line))
                            except ValueError:
                                pass
                TITLES[path] = mtime, found.get("customTitle") or found.get("aiTitle")
            return TITLES[path][1]
        except OSError:
            pass
    return None


def recent_sessions(limit=30):
    """The sessions last prompted in, newest first: id, folder, title, time of the last prompt."""
    seen, out = set(), []
    for j in history():
        sid, p = j.get("sessionId"), j.get("project")
        if not (sid and p) or sid in seen:
            continue
        seen.add(sid)
        if os.path.isdir(p) and transcripts(sid):
            title = session_title(sid) or " ".join(str(j.get("display") or "").split()) or "Untitled"
            out.append({"id": sid, "cwd": p, "title": title[:36] + ("…" if len(title) > 36 else ""),
                        "at": (j.get("timestamp") or 0) / 1000})
            if len(out) >= limit:
                break
    return out


def installed_plugins():
    """Claude Code's installed plugins, the favourites first: name, marketplace, version, folder."""
    first = ["ponytail", "impeccable", "claude-memory", "claude-seo"]
    try:
        with open(os.path.join(MAIN, "plugins", "installed_plugins.json")) as f:
            plugins = json.load(f)["plugins"]
        with open(os.path.join(MAIN, "settings.json")) as f:
            enabled = json.load(f).get("enabledPlugins") or {}
    except (OSError, ValueError, KeyError, AttributeError):
        return []
    out = []
    for key, installs in sorted(plugins.items()):
        if installs:
            name, _, market = key.partition("@")
            out.append({"name": name, "from": market, "version": str(installs[0].get("version") or "")[:12],
                        "path": installs[0].get("installPath") or "", "on": bool(enabled.get(key))})
    return sorted(out, key=lambda x: first.index(x["name"]) if x["name"] in first else len(first))


def installed_skills():
    """Your own skills: the folders of ~/.claude/skills that have a SKILL.md, with its description."""
    out = []
    for path in sorted(glob.glob(os.path.join(MAIN, "skills", "*", "SKILL.md"))):
        about = ""
        try:
            with open(path, errors="replace") as f:
                for line in f.read(4096).splitlines():
                    if line.startswith("description:"):
                        about = line.partition(":")[2].strip().strip("\"'")
                        break
        except OSError:
            pass
        out.append({"name": os.path.basename(os.path.dirname(path)), "path": os.path.dirname(path),
                    "about": about[:44] + ("…" if len(about) > 44 else "")})
    return out


def running_sessions():
    """Open Claude Code sessions, from each account's sessions/<pid>.json, busiest and newest first.
    A file only counts while its process is alive and is the same process (start time matches)."""
    out = []
    for name in accounts():
        d = os.path.join(account_dir(name), "sessions")
        try:
            files = [f for f in os.listdir(d) if f.endswith(".json")]
        except OSError:
            continue
        for f in files:
            try:
                with open(os.path.join(d, f)) as fh:
                    j = json.load(fh)
                with open(f"/proc/{int(j['pid'])}/stat") as fh:
                    started = fh.read().rsplit(")", 1)[1].split()[19]
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                continue
            if j.get("spare") or str(j.get("procStart", started)) != started:
                continue
            out.append({"account": name, "id": j.get("sessionId"), "name": j.get("name") or "", "cwd": j.get("cwd") or "",
                        "busy": j.get("status") == "busy", "bg": j.get("kind") == "bg",
                        "at": (j.get("statusUpdatedAt") or j.get("startedAt") or 0) / 1000})
    return sorted(out, key=lambda x: (not x["busy"], -x["at"]))


def best_account(names, use, ids, now):
    """The logged-in account with the most room: lowest of its higher 5-hour / weekly %."""
    def load(n):
        s, w = window(use[n], "session", now), window(use[n], "weekly", now)
        return max(s["pct"] if s else 0, w["pct"] if w else 0), s["pct"] if s else 0
    ok = [n for n in names if "error" not in (ids.get(n) or {})]
    return min(ok, key=lambda n: (*load(n), n != "main")) if len(ok) > 1 else None


class Alerts:
    """Desktop notifications when an account passes 80% / 95%, and when a full limit resets.
    Remembers per account and window what it already said, so a restart doesn't repeat it."""
    LEVELS = (95, 80)
    NAMES = {"session": "5-hour", "weekly": "weekly"}

    def __init__(self):
        self.path = os.path.join(CACHE, "alerts.json")
        try:
            with open(self.path) as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = None  # first run: learn the current levels quietly

    def check(self, names, use, now):
        quiet, self.state = self.state is None, self.state or {}
        changed = quiet
        for n in names:
            for key in ("session", "weekly"):
                raw = (use[n] or {}).get(key)
                if not isinstance(raw, dict) or "pct" not in raw:
                    continue
                resets, pct = raw.get("resets_at") or 0, raw["pct"]
                st = self.state.setdefault(n, {}).get(key)
                what = f"{n}'s {self.NAMES[key]} limit"
                if st and resets and resets <= now and st.get("resets") == resets:
                    if st["level"] >= 95 and not st.get("told_reset"):
                        st["told_reset"] = changed = True
                        quiet or notify("Claude: limit reset", f"{what} has reset. {n} is ready to use again.")
                    continue
                if not st or st.get("resets") != resets:
                    st = self.state[n][key] = {"resets": resets, "level": 0}
                    changed = True
                level = next((lv for lv in self.LEVELS if pct >= lv), 0)
                if level > st["level"]:
                    st["level"], changed = level, True
                    left = f" Resets in {dur(resets - now)}." if resets > now else ""
                    quiet or notify(f"Claude: {n} at {int(pct)}%", f"{what} is {int(pct)}% used.{left}")
        if changed:
            os.makedirs(CACHE, exist_ok=True)
            write_json(self.path, self.state)


def short(path):
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


# ---------- terminal ----------

TERMINALS = {  # name -> argv builder(cwd, cmd)
    "kitty": lambda d, c: ["kitty", "--directory", d] + c,
    "gnome-terminal": lambda d, c: ["gnome-terminal", f"--working-directory={d}", "--"] + c,
    "konsole": lambda d, c: ["konsole", "--workdir", d, "-e"] + c,
    "xfce4-terminal": lambda d, c: ["xfce4-terminal", f"--working-directory={d}", "-x"] + c,
    "alacritty": lambda d, c: ["alacritty", "--working-directory", d, "-e"] + c,
    "wezterm": lambda d, c: ["wezterm", "start", "--cwd", d, "--"] + c,
    "foot": lambda d, c: ["foot", "-D", d] + c,
    "tilix": lambda d, c: ["tilix", "-w", d, "-e"] + c,
    "xterm": lambda d, c: ["xterm", "-e"] + c,
}


def terminal():
    want = os.environ.get("CLAUDE_TRAY_TERMINAL")
    for name in ([want] if want else []) + list(TERMINALS):
        if name in TERMINALS and shutil.which(name):
            return name
    return None


def run_in_terminal(cwd, cmd, hold=False):
    """Open `cmd` in a terminal at `cwd`. The window stays open if the command fails (or always with hold)."""
    term = terminal()
    if not term:
        return "No supported terminal found. Set CLAUDE_TRAY_TERMINAL (e.g. kitty, gnome-terminal)."
    wrap = ('"$@"; s=$?; if [ "$HOLD" = 1 ] || [ $s -ne 0 ]; then echo; '
            'read -rp "[exited with $s] Press Enter to close." _; fi')
    argv = TERMINALS[term](cwd, ["bash", "-c", wrap, "claude-tray"] + cmd)
    env = dict(os.environ, HOLD="1" if hold else "0")
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_ACCOUNT", None)
    try:
        subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return f"Could not start {term}: {e}"
    return None


def claude_acc():
    return shutil.which("claude-acc")


# ---------- --test ----------

def usage_text(u, now):
    parts = []
    for label, key, length in (("5h", "session", SESSION_LEN), ("7d", "weekly", WEEK_LEN)):
        w = window(u, key, now)
        if w is None:
            continue
        s = f"{label} {w['pct']}%"
        if w["resets"]:
            s += f" (resets in {dur(w['resets'] - now)}, {clock(w['resets'])})"
        p = pace(w, length, now)
        if p is not None:
            diff = w["pct"] - p
            s += f" ▲ {round(diff)}% over pace" if diff > 5 else " under pace" if diff < -5 else " on pace"
        parts.append(s)
    return "   ".join(parts) or "no usage yet"


def test():
    now = time.time()
    print(f"terminal: {terminal() or 'none found'}   claude-acc: {claude_acc() or 'not found'}")
    for n in accounts():
        i = identity(n)
        u = usage(n)
        who = i.get("error") or f"{i['email']} ({i['plan']})"
        seen = f"used {ago(last_used(u), now)}" if last_used(u) else "never used"
        src = f", checked via API {ago(u['updated'], now)}" if u and u.get("source") == "api" else ""
        print(f"{n:10} {who}   {seen}{src}\n{'':10} {usage_text(u, now)}")
    t0 = time.time()
    s = Stats().update()
    print(f"today: ${s['cost']:.2f}  {s['turns']} prompts  {active_text(s['active'])} active  {s['lines']} lines  "
          f"replies {s['replies']}  ({time.time() - t0:.1f}s)")
    print("recent folders:", ", ".join(short(p) for p in recent_folders()) or "-")
    print("recent sessions:", ", ".join(x["title"] for x in recent_sessions()) or "-")
    return 0


if __name__ == "__main__" and len(sys.argv) > 1:
    if sys.argv[1] in ("--test", "-t"):
        sys.exit(test())
    with open(__file__) as f:
        print("".join(l[2:] for l in f.readlines()[1:32]), end="")
    sys.exit(0)


# ---------- UI ----------

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
import cairo  # noqa: E402
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp  # noqa: E402
except (ValueError, ImportError):
    XApp = None

# Status colours: a strong tone for dots, bars and badge backgrounds, and a text tone that reads on
# the menu. Text tones are picked for a dark or light menu at startup (install_css).
GREEN, AMBER, RED, GREY, BLUE, PURPLE = "#3fb950", "#d29922", "#f85149", "#8b949e", "#4c8bf5", "#a371f7"
TONES = {"green": GREEN, "amber": AMBER, "red": RED, "grey": GREY, "blue": BLUE, "purple": PURPLE}
TEXT_ON_DARK = {"green": "#56d364", "amber": "#e3b341", "red": "#ff7b72", "grey": "#b1bac4",
                "blue": "#79a8ff", "purple": "#c4a5ff"}
TEXT_ON_LIGHT = {"green": "#1a7f37", "amber": "#9a6700", "red": "#cf222e", "grey": "#57606a",
                 "blue": "#0969da", "purple": "#8250df"}


def heat(pct):
    return RED if pct >= 90 else AMBER if pct >= 70 else None


def tone_for(pct):
    return "red" if pct >= 90 else "amber" if pct >= 70 else "green"


def esc(s):
    return GLib.markup_escape_text(str(s))


def hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


def icon_path(pct, dim):
    """A 22px ring gauge as an SVG file (a new name per state so the panel doesn't serve a cached one)."""
    os.makedirs(CACHE, exist_ok=True)
    col = GREY if pct is None or dim else heat(pct) or GREEN
    key = "none" if pct is None else f"{min(pct, 100)}{'-dim' if dim else ''}"
    path = os.path.join(CACHE, f"ring-{key}.svg")
    if not os.path.exists(path):
        c, r = 11, 8
        arc = ""
        if pct:
            frac = min(pct, 100) / 100
            if frac >= 1:
                arc = f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{col}" stroke-width="3"/>'
            else:
                a = frac * 2 * math.pi
                x, y = c + r * math.sin(a), c - r * math.cos(a)
                arc = (f'<path d="M {c} {c - r} A {r} {r} 0 {1 if frac > 0.5 else 0} 1 {x:.2f} {y:.2f}" '
                       f'fill="none" stroke="{col}" stroke-width="3" stroke-linecap="round"/>')
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">'
               f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{GREY}" stroke-opacity="0.45" stroke-width="3"/>'
               f'{arc}<circle cx="{c}" cy="{c}" r="2.5" fill="{col}"/></svg>')
        with open(path + ".tmp", "w") as f:
            f.write(svg)
        os.replace(path + ".tmp", path)
    return path


CSS = """
menu, .menu, window.popup, window.popup decoration { background-color: @ct_bg; }
menu { background-image: linear-gradient(to bottom, alpha(white, 0.09), alpha(white, 0) 35%); }
menu label { font-size: 13.5pt; }
.ct-card { margin: 20px 10px; }
.ct-title { font-weight: bold; font-size: 15.5pt; }
.ct-dim { opacity: 0.8; }
.ct-small { font-size: 12.5pt; }
.ct-caption { font-size: 11pt; font-weight: bold; opacity: 0.8; }
.ct-pct { font-weight: bold; font-size: 14.5pt; }
.ct-pill { border-radius: 99px; padding: 2px 10px; font-size: 11pt; font-weight: bold; }
.ct-icon { border-radius: 7px; padding: 6px; background-color: alpha(currentColor, 0.14); }
.ct-big { font-size: 23pt; font-weight: bold; }
.ct-pin { padding: 6px; opacity: 0.3; }
.ct-pin.ct-amber { opacity: 1; }
.ct-trash { padding: 6px; opacity: 0.75; }
.ct-banner { border-radius: 8px; padding: 8px 12px; margin: 4px 6px; }
.ct-action { margin: 2px 0; }
menuitem.ct-filler { padding: 0; margin: 0; min-width: 0; min-height: 0; }
.ct-subhead { margin: 12px 6px 10px 6px; }
.ct-section { margin-top: 8px; margin-bottom: 4px; }
.ct-code { font-family: monospace; font-size: 11.5pt; border-radius: 6px; padding: 2px 8px;
           background-color: alpha(currentColor, 0.1); }
menu .ct-action { margin: 7px 0; }
menuitem.ct-gap { padding: 0; min-height: 0; }
menuitem:hover .ct-pill { color: #ffffff; background-color: rgba(255, 255, 255, 0.22); }
/* A menu of cards: glass() draws its background, with the cards on it. */
menu.ct-floating { background-color: transparent; background-image: none; padding: 10px; }
/* The same shadow, so the same room for it, on every menu window: align_top compares their heights. */
window.popup decoration { box-shadow: 0 8px 22px rgba(0, 0, 0, 0.4); }
/* The main menu has no background of its own: nothing to shade, but the same room. */
window.popup.ct-bare, window.popup.ct-bare decoration { background-color: transparent; box-shadow: 0 8px 22px transparent; }
menu.ct-bare { border-color: transparent; box-shadow: none; }
menu.ct-floating > menuitem { border-radius: 12px; }
menu.ct-floating > menuitem.ct-right { margin-left: 10px; }
menu.ct-floating > menuitem.ct-row { margin: 0 4px; border-radius: 8px; }
menu.ct-floating > menuitem.ct-row.ct-right { margin-left: 14px; }
menu.ct-floating > menuitem.ct-row.ct-first { margin-top: 4px; }
menu.ct-floating > menuitem.ct-row.ct-last { margin-bottom: 4px; }
"""


BACKDROP = [None]  # the blurred screen behind the menus, grabbed as the menu opens
BLUR = 8           # screen pixels per backdrop pixel
GUTTER = 10        # between the two columns of cards (menuitem.ct-right in the CSS)
TINT = [0.14, 0.15, 0.17, 1]  # the main menu's background over the backdrop (install_css sets it)


def grab_backdrop():
    """Frosted glass needs the screen behind the menu blurred. Muffin has no blur-behind for client
    windows, so grab the screen as the menu opens and shrink it; drawn back enlarged it is blurred.
    False where the screen can't be grabbed (Wayland): the menus stay opaque there."""
    # ponytail: a still, so what moves behind an open menu doesn't; a compositor blur would fix that
    root = Gdk.get_default_root_window()
    w, h = root.get_width(), root.get_height()
    pb = Gdk.pixbuf_get_from_window(root, 0, 0, w, h)
    if pb:
        for d in (4, 4 * BLUR, BLUR):  # down in two steps, then half way back up: no blocks
            pb = pb.scale_simple(max(1, w // d), max(1, h // d), GdkPixbuf.InterpType.BILINEAR)
    BACKDROP[0] = pb
    return pb is not None


def glass(menu, cards=None, panel=True):
    """Draw the backdrop under menu's (see-through) background. With cards (lists of items stacked
    in one column) also the background itself, and every card on it as a lighter, raised tile."""
    def shadow(cr, x, y, width, height, radius):
        """A soft drop shadow around a card, not under it (the card is see-through)."""
        cr.save()
        cr.rectangle(x - 40, y - 40, width + 80, height + 80)
        rounded(cr, x, y, width, height, radius)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.clip()
        cr.set_source_rgba(0, 0, 0, 0.055)
        for d in range(1, 21, 2):  # stacked, growing: darkest at the card's edge
            rounded(cr, x - d, y - d + 6, width + 2 * d, height + 2 * d, radius + d)
            cr.fill()
        cr.restore()

    def tile(cr, x, y, width, height, radius, fill):
        cr.save()
        rounded(cr, x, y, width, height, radius)
        cr.clip()
        cr.set_source_rgba(*fill)
        cr.paint()
        sheen = cairo.LinearGradient(0, y, 0, y + min(height, 160))
        sheen.add_color_stop_rgba(0, 1, 1, 1, 0.09)
        sheen.add_color_stop_rgba(1, 1, 1, 1, 0)
        cr.set_source(sheen)
        cr.paint()
        rounded(cr, x + 0.5, y + 0.5, width - 1, height - 1, radius)
        cr.set_source_rgba(1, 1, 1, 0.16)
        cr.set_line_width(1)
        cr.stroke()
        cr.restore()

    def draw(w, cr):
        pb, win = BACKDROP[0], w.get_window()
        if not win:
            return False
        ctx = w.get_style_context()
        width, height = w.get_allocated_width(), w.get_allocated_height()
        radius = ctx.get_property("border-radius", ctx.get_state())
        cr.save()
        rounded(cr, 0, 0, width, height, radius)
        cr.clip()

        def backdrop():
            if pb:
                _, x, y = win.get_origin()
                cr.save()
                cr.translate(-x, -y)
                cr.scale(BLUR, BLUR)
                Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
                cr.paint()
                cr.restore()
        if panel:
            backdrop()
        if cards is not None:  # a plain menu's background comes from the CSS, after this
            if panel:
                tile(cr, 0, 0, width, height, radius, TINT)
            boxes = []
            for items in cards:
                at, end = items[0].translate_coordinates(w, 0, 0), items[-1].translate_coordinates(w, 0, 0)
                if at and end:
                    gutter = GUTTER if items[0].get_style_context().has_class("ct-right") else 0
                    boxes.append((at[0] + gutter, at[1], items[0].get_allocated_width() - gutter,
                                  end[1] + items[-1].get_allocated_height() - at[1], 12))
            for box in boxes:
                shadow(cr, *box)
            for box in boxes:
                if not panel:  # each card is its own piece of glass
                    cr.save()
                    rounded(cr, *box)
                    cr.clip()
                    backdrop()
                    cr.set_source_rgba(*TINT)
                    cr.paint()
                    cr.restore()
                tile(cr, *box, (1, 1, 1, 0.07))
        cr.restore()
        return False
    menu.connect("draw", draw)
    if cards is not None:
        menu.get_style_context().add_class("ct-floating")
    if not panel:
        for w in (menu, menu.get_toplevel()):
            w.get_style_context().add_class("ct-bare")
    return menu


def install_css():
    """Glass menus where the screen can be blurred, else opaque ones (themes often make them
    see-through), and the badge colours for this menu's shade."""
    probe = Gtk.Menu()
    ctx = probe.get_style_context()
    fg = ctx.get_color(Gtk.StateFlags.NORMAL)
    dark = 0.299 * fg.red + 0.587 * fg.green + 0.114 * fg.blue > 0.5
    bg = ctx.get_property("background-color", Gtk.StateFlags.NORMAL)
    if bg.alpha < 0.05:  # theme draws the menu in a parent node; fall back to a plain shade
        bg = Gdk.RGBA(*((0.14, 0.15, 0.17) if dark else (0.97, 0.97, 0.98)), 1)
    alpha = 0.8 if Gdk.Screen.get_default().is_composited() and grab_backdrop() else 1
    TINT[:] = bg.red, bg.green, bg.blue, alpha
    css = (f"@define-color ct_bg alpha(rgb({int(bg.red * 255)},{int(bg.green * 255)},{int(bg.blue * 255)}), {alpha});\n"
           + CSS)
    text = TEXT_ON_DARK if dark else TEXT_ON_LIGHT
    for t, base in TONES.items():
        css += (f".ct-{t} {{ color: {text[t]}; }}\n"
                f".ct-pill.ct-{t}, .ct-banner.ct-{t} {{ background-color: alpha({base}, 0.2); }}\n"
                f".ct-icon.ct-{t} {{ background-color: {base}; color: #ffffff; }}\n")
    p = Gtk.CssProvider()
    p.load_from_data(css.encode())
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def lbl(text, *classes, markup=False, xalign=0.0, wrap=False):
    lb = Gtk.Label(xalign=xalign)
    lb.set_markup(text) if markup else lb.set_text(str(text))
    for c in classes:
        lb.get_style_context().add_class(c)
    if wrap:
        lb.set_line_wrap(True)
        lb.set_max_width_chars(44)
    return lb


def label(markup, xalign=0.0):
    return lbl(markup, markup=True, xalign=xalign)


def pill(text, tone):
    p = lbl(text, "ct-pill", f"ct-{tone}")
    p.set_valign(Gtk.Align.CENTER)
    return p


def dot(tone):
    d = Gtk.DrawingArea()
    d.set_size_request(10, 10)
    d.set_valign(Gtk.Align.CENTER)

    def draw(w, cr):
        cr.set_source_rgb(*hex_rgb(TONES[tone]))
        cr.arc(5, w.get_allocated_height() / 2, 5, 0, 2 * math.pi)
        cr.fill()
    d.connect("draw", draw)
    return d


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Meter(Gtk.Box):
    """One usage window: caption + time to the reset, a bar with a pace tick, the % under it."""

    def __init__(self, caption, w, length, now, dim):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        self.w, self.dim, self.pace = w, dim, pace(w, length, now)
        top = Gtk.Box()
        top.pack_start(lbl(caption, "ct-caption"), True, True, 0)
        when = f"Resets in {dur(w['resets'] - now)}" if w["resets"] else "Reset time unknown"
        top.pack_end(lbl(when, "ct-small", "ct-dim", xalign=1), False, False, 0)
        self.pack_start(top, False, False, 0)
        bar = Gtk.DrawingArea()
        bar.set_size_request(-1, 12)
        bar.connect("draw", self.draw)
        self.pack_start(bar, False, False, 0)
        self.pack_start(lbl(f"{w['pct']}%", "ct-pct", *([] if dim else [f"ct-{tone_for(w['pct'])}"]), xalign=1),
                        False, False, 0)

    def draw(self, widget, cr):
        width, h = widget.get_allocated_width(), widget.get_allocated_height()
        fg = widget.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        y, bh = (h - 8) / 2, 8
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.16)
        rounded(cr, 0, y, width, bh, 4)
        cr.fill()
        if self.w["pct"] > 0:
            cr.set_source_rgb(*hex_rgb(GREY if self.dim else TONES[tone_for(self.w["pct"])]))
            rounded(cr, 0, y, max(8, width * self.w["pct"] / 100), bh, 4)
            cr.fill()
        if self.pace is not None:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
            rounded(cr, width * self.pace / 100 - 1, 0, 2, h, 1)
            cr.fill()
        return False


class AccountCard(Gtk.Box):
    def __init__(self, name, ident, u, now, login, hide_emails, api_note):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.get_style_context().add_class("ct-card")
        self.set_size_request(360, -1)
        used = last_used(u)
        dim = not u or now - u.get("updated", 0) > STALE
        s = window(u, "session", now)
        logged_out = ident is not None and "error" in ident
        tone = "blue" if login else "grey" if logged_out or dim or not s else tone_for(s["pct"])
        self.tone = tone

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        row = Gtk.Box(spacing=8)
        row.pack_start(lbl(name, "ct-title"), False, False, 0)
        plan = (ident or {}).get("plan")
        if plan:
            row.pack_start(pill(plan, "purple" if plan.lower() in ("max", "team", "enterprise") else
                                "blue" if plan.lower() == "pro" else "grey"), False, False, 0)
        if login:
            row.pack_end(pill("Logging in…", "blue"), False, False, 0)
        elif logged_out:
            row.pack_end(pill("Not logged in", "amber"), False, False, 0)
        elif used and now - used < 180:
            row.pack_end(pill("Active now", "blue"), False, False, 0)
        elif used:
            row.pack_end(lbl(f"used {ago(used, now)}", "ct-small", "ct-dim", xalign=1), False, False, 0)
        head.pack_start(row, False, False, 0)
        if ident and "email" in ident:
            email = mask_email(ident["email"]) if hide_emails else ident["email"]
            e = lbl(email, "ct-small", "ct-dim")
            e.set_ellipsize(Pango.EllipsizeMode.END)
            head.pack_start(e, False, False, 0)
        top = Gtk.Box(spacing=10)
        img = Gtk.Image.new_from_icon_name("avatar-default-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        img.set_valign(Gtk.Align.CENTER)
        img.get_style_context().add_class(f"ct-{tone}")  # the account's state
        top.pack_start(img, False, False, 0)
        top.pack_start(head, True, True, 0)
        self.pack_start(top, False, False, 0)

        meters = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for caption, key, length in (("SESSION", "session", SESSION_LEN), ("WEEKLY", "weekly", WEEK_LEN)):
            w = window(u, key, now)
            if w:
                meters.pack_start(Meter(caption, w, length, now, dim), False, False, 0)
        if meters.get_children():
            self.pack_start(meters, False, False, 0)
        elif not logged_out:
            self.pack_start(lbl("No usage yet. Open it and send a message.", "ct-small", "ct-dim"), False, False, 0)
        if api_note:
            self.pack_start(lbl(api_note, "ct-small", "ct-amber", wrap=True), False, False, 0)


MODEL_TONES = {"Opus": "amber", "Sonnet": "blue", "Haiku": "green", "Fable": "purple"}


class TodayCard(Gtk.Box):
    """Today across all accounts: cost, then prompts / active time / lines, then replies per model."""
    def __init__(self, s):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.get_style_context().add_class("ct-card")
        self.set_size_request(360, -1)
        top = Gtk.Box(spacing=8)
        pulse = Gtk.Image.new_from_icon_name("utilities-system-monitor-symbolic", Gtk.IconSize.MENU)
        pulse.get_style_context().add_class("ct-red")
        top.pack_start(pulse, False, False, 0)
        top.pack_start(lbl("Today's Activity", "ct-title"), True, True, 0)
        cost = lbl(f"${s['cost']:.2f}", "ct-big", "ct-green", xalign=1)
        cost.set_tooltip_text("API-equivalent cost, all accounts")
        top.pack_end(cost, False, False, 0)
        self.pack_start(top, False, False, 0)
        rows = [[(s["turns"], "Prompts", "chat-symbolic"), (active_text(s["active"]), "Active", "preferences-system-time-symbolic"),
                 (s["lines"], "Lines", "text-x-generic-symbolic")]]
        models = sorted(s["replies"].items(), key=lambda kv: -kv[1])[:3]
        if models:
            rows.append([(v, k, MODEL_TONES.get(k, "grey")) for k, v in models])
        for row in rows:
            line = Gtk.Box(homogeneous=True, spacing=8)
            for v, k, tone in row + [("", "", None)] * (3 - len(row)):
                t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                t.pack_start(lbl(v, "ct-pct", xalign=0.5), False, False, 0)
                cap = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
                if tone in TONES:
                    cap.pack_start(dot(tone), False, False, 0)
                elif tone:  # an icon's name
                    icon = Gtk.Image.new_from_icon_name(tone, Gtk.IconSize.MENU)
                    icon.get_style_context().add_class("ct-dim")
                    cap.pack_start(icon, False, False, 0)
                cap.pack_start(lbl(k, "ct-small", "ct-dim"), False, False, 0)
                t.pack_start(cap, False, False, 0)
                line.pack_start(t, True, True, 0)
            self.pack_start(line, False, False, 0)


def card_box(title, note=None):
    """A compact right-column card: a small caption (and a note at its right) above the rows."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    box.get_style_context().add_class("ct-card")
    box.set_size_request(360, -1)
    head = Gtk.Box()
    head.pack_start(lbl(title, "ct-caption"), False, False, 0)
    if note:
        head.pack_end(lbl(note, "ct-caption", "ct-dim"), False, False, 0)
    box.pack_start(head, False, False, 0)
    return box


class BestCard(Gtk.Box):
    """The account with the most room left; clicking the item continues the last session on it."""
    def __init__(self, name, u, now, folder):
        super().__init__()
        s, w = window(u, "session", now), window(u, "weekly", now)
        used = max(s["pct"] if s else 0, w["pct"] if w else 0)
        box = card_box("BEST ACCOUNT NOW")
        row = Gtk.Box(spacing=10)
        row.pack_start(dot(tone_for(used)), False, False, 0)
        row.pack_start(lbl(name, "ct-pct"), False, False, 0)
        parts = [f"5h {s['pct']}%" if s else "5h unused", f"7d {w['pct']}%" if w else "7d unused"]
        row.pack_start(lbl(" · ".join(parts), "ct-small", "ct-dim"), False, False, 0)
        row.pack_end(pill(f"{100 - used}% left", tone_for(used)), False, False, 0)
        box.pack_start(row, False, False, 0)
        go = lbl(f"▶  Continue last session in {os.path.basename(folder) or folder}" if folder else "▶  Open in a folder…", "ct-small", "ct-green")
        go.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        go.set_margin_start(20)
        box.pack_start(go, False, False, 0)
        self.pack_start(box, True, True, 0)


class SessionsCard(Gtk.Box):
    """Claude Code sessions open right now, and which account each one uses."""
    MAX = 3

    def __init__(self, sessions):
        super().__init__()
        box = card_box("RUNNING SESSIONS", None if sessions else "none open")
        for x in sessions[:self.MAX]:
            row = Gtk.Box(spacing=10)
            row.pack_start(dot("green" if x["busy"] else "grey"), False, False, 0)
            title = lbl(x["name"] or os.path.basename(x["cwd"]) or "session", "ct-small")
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.set_tooltip_text(short(x["cwd"]) + (" (background)" if x["bg"] else ""))
            row.pack_start(title, True, True, 0)
            row.pack_end(pill(x["account"], "blue"), False, False, 0)
            box.pack_start(row, False, False, 0)
        if len(sessions) > self.MAX:
            box.pack_start(lbl(f"+ {len(sessions) - self.MAX} more", "ct-caption", "ct-dim"), False, False, 0)
        self.pack_start(box, True, True, 0)


class LoginWindow(Gtk.Window):
    """`claude auth login` without a terminal: it opens the browser itself and finishes on its own once
    you approve (the browser link points back to localhost). A code box is there for when it doesn't."""

    def __init__(self, app, name, email):
        super().__init__(title=f"Log in: {name}")
        self.app, self.name = app, name
        self.out, self.states, self.done = [], set(), False
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(16)
        self.set_default_size(460, -1)
        self.connect("delete-event", lambda *_: self.hide() or True)

        os.makedirs(CACHE, exist_ok=True)
        self.url_file = os.path.join(CACHE, f"login-{name}.url")
        hook = os.path.join(CACHE, "browser-hook")
        with open(hook, "w") as f:  # records the link Claude opens, then opens it as usual
            f.write('#!/bin/sh\nprintf "%s" "$1" > "$CLAUDE_TRAY_URL_FILE"\n'
                    'command -v xdg-open >/dev/null && exec xdg-open "$1"\n')
        os.chmod(hook, 0o755)
        try:
            os.remove(self.url_file)
        except OSError:
            pass
        env = dict(account_env(name), BROWSER=hook, CLAUDE_TRAY_URL_FILE=self.url_file)
        args = [shutil.which("claude") or "claude", "auth", "login"] + (["--email", email] if email else [])
        self.proc = subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, start_new_session=True)
        threading.Thread(target=self.reader, daemon=True).start()
        GLib.timeout_add_seconds(LOGIN_TIMEOUT, self.timeout)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(box)
        intro = label(f"<b>Your browser opened claude.ai.</b>\nSign in as the account you want for "
                      f"<b>{esc(name)}</b> and click <b>Authorize</b>. This window closes by itself "
                      "when the login finishes.")
        intro.set_line_wrap(True)
        box.pack_start(intro, False, False, 0)
        tip = label('<small>Browser already signed in as another account? Sign out there first, or copy '
                    'the link and open it in a private window (Ctrl+Shift+N or Ctrl+Shift+P).</small>')
        tip.set_line_wrap(True)
        box.pack_start(tip, False, False, 0)
        row = Gtk.Box(spacing=6)
        copy = Gtk.Button(label="Copy link")
        copy.connect("clicked", self.copy_link)
        again = Gtk.Button(label="Open browser again")
        again.connect("clicked", lambda *_: self.link() and subprocess.Popen(["xdg-open", self.link()]))
        row.pack_start(copy, False, False, 0)
        row.pack_start(again, False, False, 0)
        box.pack_start(row, False, False, 0)

        exp = Gtk.Expander(label="The browser showed a code instead?")
        cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6)
        self.entry = Gtk.Entry(visibility=False, placeholder_text="paste the code here (Ctrl+V)",
                               activates_default=True)
        self.entry.set_invisible_char("*")
        self.entry.connect("activate", self.submit)
        cbox.pack_start(self.entry, False, False, 0)
        crow = Gtk.Box(spacing=6)
        show = Gtk.CheckButton(label="Show code")
        show.connect("toggled", lambda b: self.entry.set_visibility(b.get_active()))
        send = Gtk.Button(label="Log in with code")
        send.connect("clicked", self.submit)
        crow.pack_start(show, False, False, 0)
        crow.pack_end(send, False, False, 0)
        cbox.pack_start(crow, False, False, 0)
        self.hint = label("")
        self.hint.set_line_wrap(True)
        cbox.pack_start(self.hint, False, False, 0)
        exp.add(cbox)
        box.pack_start(exp, False, False, 0)

        bottom = Gtk.Box(spacing=6)
        self.status = label('<small><span alpha="65%">Waiting for the browser…</span></small>')
        cancel = Gtk.Button(label="Cancel login")
        cancel.connect("clicked", lambda *_: self.cancel())
        bottom.pack_start(self.status, True, True, 0)
        bottom.pack_end(cancel, False, False, 0)
        box.pack_start(bottom, False, False, 0)
        self.show_all()

    def reader(self):
        for line in self.proc.stdout:
            self.out.append(line)
            self.states.update(re.findall(r"state=([A-Za-z0-9_-]+)", line))
        rc = self.proc.wait()
        GLib.idle_add(self.finished, rc)

    def link(self):
        try:
            with open(self.url_file) as f:
                return f.read().strip()
        except OSError:
            m = re.search(r"https://\S+?(?=\x1b|\s|$)", "".join(self.out))
            return m.group(0) if m else None

    def copy_link(self, *_):
        url = self.link()
        if url:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(url, -1)
            self.status.set_markup('<small>Link copied. Paste it into a private window.</small>')
        else:
            self.status.set_markup(f'<small><span foreground="{AMBER}">No link yet, try again in a moment.</span></small>')

    def submit(self, *_):
        code = re.sub(r"\s+", "", self.entry.get_text())
        half = len(code) // 2
        if len(code) % 2 == 0 and code and code[:half] == code[half:]:
            code = code[:half]  # pasted twice
        if "#" not in code:
            return self.say("That isn't a login code (it has no '#'). Copy it again from the browser.")
        self.states.update(re.findall(r"state=([A-Za-z0-9_-]+)", self.link() or ""))
        if self.states and code.split("#", 1)[1] not in self.states:
            return self.say("That code is from a different login attempt (an older browser tab). "
                            "Use the tab this login opened, or click Open browser again.")
        try:
            self.proc.stdin.write(code + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return self.say("The login already ended.")
        self.say("Checking the code…", ok=True)

    def say(self, text, ok=False):
        self.hint.set_markup(f'<small><span foreground="{BLUE if ok else AMBER}">{esc(text)}</span></small>')

    def timeout(self):
        if self.proc.poll() is None:
            self.cancel("Login timed out.")
        return False

    def cancel(self, why="Login cancelled."):
        self.cancelled = why
        if self.proc.poll() is None:
            self.proc.terminate()

    def finished(self, rc):
        self.done = True
        self.destroy()
        msg = getattr(self, "cancelled", None)
        if rc != 0 and not msg:
            last = [l.strip() for l in self.out if l.strip()]
            msg = re.sub(r".*prompted > ", "", last[-1]) if last else "Login failed."
        self.app.login_finished(self.name, rc == 0 and not getattr(self, "cancelled", None), msg)
        return False


def two_lines(title, sub, tone=None):
    """A menu entry's title with a smaller explanation under it."""
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    b.get_style_context().add_class("ct-action")
    b.pack_start(lbl(title, *([f"ct-{tone}"] if tone else [])), False, False, 0)
    if sub:
        b.pack_start(lbl(sub, "ct-small", "ct-dim"), False, False, 0)
    return b


SIDE = 12  # gap between the menu and an account's submenu beside it
SUBMENU_FIX = [0]  # learned: how much taller than asked a submenu window comes out (same for all)


def align_top(item, flush=False):
    """Open item's submenu level with the top of the parent menu, and exactly as tall.
    GTK places a submenu beside its item and computes the offset from the submenu's
    -GtkMenu-vertical-offset style property, so that is set just before each popup. Moving the
    window afterwards instead made the submenu close when the pointer moved onto it."""
    sub = item.get_submenu()
    css = Gtk.CssProvider()
    sub.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    tries = [0]  # corrections in this popup (content taller than the parent can't shrink further)

    def target():
        parent = item.get_parent()
        pw = parent and parent.get_toplevel().get_window()
        return pw.get_height() if pw else None

    def prepare(*a):
        parent = item.get_parent()
        if not parent:
            return
        x, y = item.translate_coordinates(parent, 0, 0)
        ctx = sub.get_style_context()
        state = ctx.get_state()
        edge = ctx.get_padding(state).top + ctx.get_border(state).top  # GTK subtracts these again
        rule = f"-GtkMenu-vertical-offset: {edge - y};"
        width = -1
        if flush:
            # GTK leaves (offset + both paddings, ~10px) between an item and its submenu. For an item
            # in the right column that strip is part of the account card beside it: crossing it
            # selected that card and closed this submenu. Overlap the item by 4px instead.
            pc = parent.get_style_context()
            pad = pc.get_padding(pc.get_state()).right + ctx.get_padding(state).left
            rule += f" -GtkMenu-horizontal-offset: {-pad - 4};"
            width = x + 4  # at least the column it opens over: no sliver of the cards beside it
        else:
            # GTK lets a submenu on this side reach over the parent by the item's x and both
            # paddings. Take that back, and SIDE more: a gap between the menu and the submenu.
            pc = parent.get_style_context()
            pads = pc.get_padding(pc.get_state()).left + ctx.get_padding(state).right
            over = x + pads
            # Without a panel the gap is seen between the cards, which sit inside both paddings.
            gap = SIDE - (pads if ctx.has_class("ct-bare") else 0)
            rule += f" -GtkMenu-horizontal-offset: {-over - gap};"
        css.load_from_data(f"menu {{ {rule} }}".encode())
        # As tall as the parent menu. Both windows add the same shadow room to that (see the CSS),
        # so they come out the same height; check() corrects it where a theme makes them differ.
        sub.set_size_request(width, max(1, parent.get_allocated_height() - SUBMENU_FIX[0]))

    def check(*a):
        sw, h = sub.get_toplevel().get_window(), target()
        if not (sw and h and sub.get_visible()):
            return False
        off = sw.get_height() - h
        if off and tries[0] < 2:
            tries[0] += 1
            # Ask GTK for the right size; it resizes the submenu window itself.
            SUBMENU_FIX[0] += off
            prepare()
            sub.queue_resize()
            print(f"submenu height corrected by {off}px (now asks {h} for the window)", file=sys.stderr)
        return False

    item.connect("enter-notify-event", lambda *a: tries.__setitem__(0, 0) or prepare() and False)  # before GTK opens it
    item.connect("select", prepare)
    sub.connect("popped-up", lambda *a: GLib.timeout_add(50, check))


GAP = 10  # space between two cards in a column (an unselectable row)


def gap_item():
    """An unselectable row between two cards, so hovering a card highlights just the card."""
    gap = Item()
    gap.blank = True
    gap.get_style_context().add_class("ct-gap")
    gap.set_sensitive(False)
    gap.add(Gtk.Box(margin_top=GAP))  # no set_size_request: grid_menu sets heights
    return gap


def carded(menu, panel=False):
    """Make a plain menu one of cards on a glass background. A card starts at every item marked
    `starts` (the captions) and takes the items after it."""
    cards = []
    for it in menu.get_children():
        if getattr(it, "starts", False) or not cards:
            cards.append([])
        cards[-1].append(it)
    for card in cards:
        if card is not cards[0]:
            menu.insert(gap_item(), menu.get_children().index(card[0]))
        for it in card:
            it.get_style_context().add_class("ct-row")
        card[0].get_style_context().add_class("ct-first")
        card[-1].get_style_context().add_class("ct-last")
    menu.cards = cards
    return glass(menu, cards, panel)


def fit(menu, height):
    """Drop rows from the end of menu's cards, the last card first, until its items are no taller
    than height. (A menu that isn't open yet reports no height of its own, so its items are added up.)"""
    menu.show_all()
    for rows in reversed(menu.cards):
        while len(rows) > 2 and sum(it.get_preferred_height()[1] for it in menu.get_children()) > height:
            menu.remove(rows.pop())
            rows[-1].get_style_context().add_class("ct-last")  # its margin counts too


class Item(Gtk.MenuItem):
    """A menu item whose height grid_menu can set exactly (forced = (min, natural) or None)."""
    forced = None

    def do_get_preferred_height(self):
        return self.forced or Gtk.MenuItem.do_get_preferred_height(self)

    def do_get_preferred_height_for_width(self, width):
        return self.forced or Gtk.MenuItem.do_get_preferred_height_for_width(self, width)


def grid_menu(columns, scratch):
    """A Gtk.Menu with two columns of cards on its background: accounts (columns[0]) left, the rest right.
    Each column is simply stacked, with the same gap between its cards. A card is one item, or
    several when the following ones are marked joined (the actions). The window is as tall as the
    taller column; an invisible filler ends the shorter one.
    Gtk.Menu can't do that directly: it lays items on shared rows, and sets each row's height from
    the items that START in it, adding only height/rows for an item spanning several rows. So the
    rows are cut at every item edge of both columns, and for each row one item starting there is
    made to ask exactly row height x its rows, the others nothing (see Item). Nothing is ever given
    less room than its content needs. Every column is as wide as the widest, as Gtk.Menu does."""
    for it in columns[1]:
        it.get_style_context().add_class("ct-right")  # gutter between the columns
    for m in scratch:
        for k, it in reversed(list(enumerate(m.get_children()))[1:]):
            if not getattr(it, "joined", False):
                m.insert(gap_item(), k)
    columns = [m.get_children() for m in scratch]
    left, right = columns
    for m in scratch:
        m.show_all()
    # Measure while the items are still in a menu, so the menu's CSS (fonts) applies.
    width = max(it.get_preferred_width()[1] for col in columns for it in col)
    heights = [[it.get_preferred_height_for_width(width)[1] for it in col] for col in columns]
    for m, col in zip(scratch, columns):
        for it in col:
            m.remove(it)
    cols = [list(left), list(right)]
    total = max(sum(h) for h in heights)
    for col, hs in zip(cols, heights):
        if sum(hs) < total:  # filler that ends the shorter column
            f = Item()
            f.blank = True
            f.get_style_context().add_class("ct-filler")
            f.set_sensitive(False)
            col.append(f)
            hs.append(total - sum(hs))
    edges = [list(itertools.accumulate(hs, initial=0)) for hs in heights]
    bounds = sorted(set(edges[0]) | set(edges[1]))
    row = {b: i for i, b in enumerate(bounds)}
    menu = glass(Gtk.Menu(), [list(g) for col in cols
                              for blank, g in itertools.groupby(col, lambda it: getattr(it, "blank", False))
                              if not blank], panel=False)
    starts = {}
    for x, (col, e) in enumerate(zip(cols, edges)):
        for it, top, bottom in zip(col, e, e[1:]):
            menu.attach(it, x, x + 1, row[top], row[bottom])
            it.forced = (0, 0)
            starts.setdefault(row[top], []).append((row[bottom] - row[top], it))
    for r, items in starts.items():
        span, it = min(items, key=lambda si: si[0])  # prefer an item that is just this row
        it.forced = ((bounds[r + 1] - bounds[r]) * span,) * 2
    for col in cols:
        for it in col:
            it.queue_resize()  # GTK caches sizes; make it ask again
    menu.show_all()
    menu.content = total  # height of its items
    return menu


class Tray(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.settings = load_settings()
        self.ids, self.ids_for, self.ids_time, self.fetching_ids = {}, (), 0, False
        self.api = {}            # account -> {'blocked', 'strikes', 'error', 'busy', 'last'}
        self.stats, self.today, self.stats_busy, self.stats_time = Stats(), None, False, 0
        self.note = None         # (text, bad) after add / login / remove
        self.login = None        # LoginWindow while a login runs
        self.icon = None
        self.menu = Gtk.Menu()
        self.menu_sig, self.hidden_at = None, 0
        self.retired = []        # (time, menu): replaced menus, destroyed later (see build_menu)
        self.alerts = Alerts()

    # Gtk.Application: a second launch just activates the running one.
    def do_activate(self):
        if self.icon:
            return
        self.hold()
        install_css()
        if XApp:
            self.icon = XApp.StatusIcon()
            self.icon.set_name("claude-tray")
            # The menu is popped up here, not handed to XApp (set_primary_menu): XApp makes a new
            # anchor window for every popup and frees the previous one while GDK still has it
            # registered, so a later X event for it crashed the tray (use after free in
            # gdk_display_get_event). One anchor window, kept for the tray's lifetime, avoids that.
            self.anchor = None
            self.icon.connect("button-release-event", self.popup_menu)
        else:
            self.icon = Gtk.StatusIcon()
            self.icon.connect("activate", lambda *_: (grab_backdrop(), self.menu.popup_at_pointer(None)))
            self.icon.connect("popup-menu", lambda *_: (grab_backdrop(), self.menu.popup_at_pointer(None)))
        self.tick()
        GLib.timeout_add_seconds(15, self.tick)

    # XApp panel position -> where the anchor goes and which gravities, as XApp itself does it.
    ANCHORS = {Gtk.PositionType.TOP: (0, -1, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST),
               Gtk.PositionType.LEFT: (-1, 0, Gdk.Gravity.NORTH_EAST, Gdk.Gravity.NORTH_WEST),
               Gtk.PositionType.RIGHT: (0, 0, Gdk.Gravity.NORTH_WEST, Gdk.Gravity.NORTH_EAST),
               Gtk.PositionType.BOTTOM: (0, 0, Gdk.Gravity.NORTH_WEST, Gdk.Gravity.SOUTH_WEST)}

    def popup_menu(self, icon, x, y, button, _time, position):
        if button not in (Gdk.BUTTON_PRIMARY, Gdk.BUTTON_SECONDARY) or self.menu.get_visible():
            return
        size = max(1, icon.get_icon_size() or 24)
        dx, dy, rect_anchor, menu_anchor = self.ANCHORS.get(position, self.ANCHORS[Gtk.PositionType.BOTTOM])
        x, y = x + dx * size, y + dy * size
        if self.anchor is None:
            attrs = Gdk.WindowAttr()
            attrs.window_type = Gdk.WindowType.CHILD
            attrs.wclass = Gdk.WindowWindowClass.INPUT_ONLY
            attrs.x, attrs.y, attrs.width, attrs.height = x, y, size, size
            self.anchor = Gdk.Window.new(None, attrs, Gdk.WindowAttributesType.X | Gdk.WindowAttributesType.Y)
        else:
            self.anchor.move_resize(x, y, size, size)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = 0, 0, size, size
        grab_backdrop()
        self.menu.set_property("anchor-hints", Gdk.AnchorHints.SLIDE | Gdk.AnchorHints.RESIZE)
        self.menu.popup_at_rect(self.anchor, rect, rect_anchor, menu_anchor, None)

    def tick(self):
        now = time.time()
        names = accounts()
        if (tuple(names) != self.ids_for or now - self.ids_time > 1800) and not self.fetching_ids:
            self.fetch_identities(names)
        if now - self.stats_time >= 60 and not self.stats_busy:
            self.refresh_stats()
        if self.settings["api"]:
            for n in names:
                self.maybe_fetch_api(n, now)
        self.render(now)
        return True

    # ----- background work; results come back to the UI thread with GLib.idle_add -----

    def background(self, work, done):
        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()

    def fetch_identities(self, names=None):
        names = names or accounts()
        self.fetching_ids = True

        def done(ids):
            self.fetching_ids = False
            self.ids, self.ids_for, self.ids_time = ids, tuple(names), time.time()
            self.render()
        self.background(lambda: {n: identity(n) for n in names}, done)

    def refresh_stats(self):
        self.stats_busy, self.stats_time = True, time.time()

        def done(s):
            self.stats_busy, self.today = False, s
            self.render()
        self.background(self.stats.update, done)

    def maybe_fetch_api(self, name, now, force=False):
        a = self.api.setdefault(name, {"blocked": 0, "strikes": 0, "error": None, "busy": False, "last": 0})
        u = usage(name) or {}
        logged_out = "error" in self.ids.get(name, {})
        if a["busy"] or now < a["blocked"] or logged_out:
            return
        if not force and (now - u.get("updated", 0) < API_EVERY or now - a["last"] < API_EVERY):
            return
        a["busy"], a["last"] = True, now

        def work():
            feed, err, code, retry = fetch_api(name)
            if code == 401:  # let the official CLI refresh the token, then try once more
                identity(name)
                feed, err, code, retry = fetch_api(name)
            return feed, err, code, retry

        def done(res):
            feed, err, code, retry = res
            a["busy"] = False
            if feed:
                a["strikes"], a["error"] = 0, None
                save_api_feed(name, feed)
            else:
                a["error"] = err
                if code == 429:
                    a["strikes"] += 1
                    a["blocked"] = time.time() + (retry or min(BACKOFF * 2 ** (a["strikes"] - 1), 3600))
            self.render()
        self.background(work, done)

    # ----- panel -----

    def render(self, now=None):
        now = now or time.time()
        names = accounts()
        use = {n: usage(n) for n in names}
        # The panel shows the account Claude Code reported for last, i.e. the one you're using.
        shown = max(names, key=lambda n: (last_used(use[n]), n == "main"))
        u = use[shown]
        s, w = window(u, "session", now), window(u, "weekly", now)
        dim = not u or now - u.get("updated", 0) > STALE
        st = self.settings

        if XApp:
            self.icon.set_icon_name(icon_path(s and s["pct"], dim))
            parts = []
            if st["label_account"] and len(names) > 1:
                parts.append(shown)
            if s and st["label"] in ("session", "both"):
                parts.append(f"{s['pct']}%" + (f" · {w['pct']}%" if w and st["label"] == "both" else ""))
            if st["label_reset"] and s and s["resets"]:
                parts.append(f"↻{dur(s['resets'] - now)}")
            if st["label_cost"] and self.today:
                parts.append(f"${self.today['cost']:.2f}")
            self.icon.set_label("  ".join(parts))
        else:
            self.icon.set_from_file(icon_path(s and s["pct"], dim))
        tip = [f"{n}: " + (f"5h {window(use[n], 'session', now)['pct']}% · 7d {window(use[n], 'weekly', now)['pct']}%"
                           if window(use[n], "session", now) and window(use[n], "weekly", now) else "no usage yet")
               for n in names]
        self.icon.set_tooltip_text("Claude\n" + "\n".join(tip))
        if self.settings["alerts"]:
            self.alerts.check(names, use, now)
        self.build_menu(names, use, now)

    # ----- menu -----

    def build_menu(self, names, use, now):
        if self.menu.get_visible() or time.time() - self.hidden_at < 5:
            return  # never swap a menu that is open or just closed; the next tick will
        # Rebuild only when something shown changed; times are shown in minutes.
        sig = json.dumps([names, use, self.ids, self.today, self.note, self.login and self.login.name,
                          self.settings, self.api, recent_folders(), recent_sessions(), installed_plugins(), installed_skills(), int(now // 60),
                          [(x["account"], x["name"], x["cwd"], x["busy"]) for x in running_sessions()]],
                         sort_keys=True, default=str)
        if sig == self.menu_sig:
            return
        self.menu_sig = sig
        # Two columns so the menu fits without scrolling: accounts on the left, Today and the
        # actions on the right. Each side is built in its own scratch menu, then laid out in a grid.
        left, right = Gtk.Menu(), Gtk.Menu()
        folders = recent_folders()
        self.sessions, self.project_menus = recent_sessions(), []
        for n in names:  # grid_menu adds the dividers between these, evenly spaced
            left.append(self.account_item(n, use[n], now, folders))

        if self.today:
            self.widget_item(right, TodayCard(self.today))
        best = best_account(names, use, self.ids, now)
        if best:
            it = self.widget_item(right, BestCard(best, use[best], now, folders[0] if folders else None))
            it.connect("activate", lambda *_: self.open(best, folders[0], ["--continue"]) if folders
                       else self.choose_folder(best))
        self.widget_item(right, SessionsCard(running_sessions()))
        cards = len(right.get_children())  # what follows is one card
        if self.login:
            self.banner(right, f"Waiting for the browser login of {self.login.name}…", "blue")
            self.action(right, "dialog-password-symbolic", "Show login window", lambda *_: self.login and self.login.present())
            self.action(right, "process-stop-symbolic", "Cancel login", lambda *_: self.login and self.login.cancel())
        elif self.note:
            self.banner(right, self.note[0], "amber" if self.note[1] else "green")
        self.action(right, "list-add-symbolic", "Add account…", self.add_account).set_sensitive(self.login is None)
        self.action(right, "view-refresh-symbolic", "Refresh now", self.refresh_now)
        self.action(right, "web-browser-symbolic", "Open usage page…", lambda *_: subprocess.Popen(["xdg-open", USAGE_PAGE]))
        settings = self.action(right, "preferences-system-symbolic", "Settings", None)
        settings.set_submenu(self.settings_menu())
        align_top(settings, flush=True)
        self.action(right, "system-shutdown-symbolic", "Quit", lambda *_: self.quit(), tone="red")
        rows = right.get_children()[cards:]
        for it in rows:
            it.joined = it is not rows[0]
            it.get_style_context().add_class("ct-row")
        rows[0].get_style_context().add_class("ct-first")
        rows[-1].get_style_context().add_class("ct-last")

        menu = grid_menu([left.get_children(), right.get_children()], (left, right))
        for pm in self.project_menus:  # they open as tall as this menu: list what fits in that
            fit(pm, menu.content)
        left.destroy()
        right.destroy()
        menu.connect("hide", lambda *_: setattr(self, "hidden_at", time.time()))
        # A replaced menu can still get a late event (pointer leaving, a release). Freeing it right
        # away made GTK crash, so keep it alive for two minutes and destroy it then.
        now_t = time.time()
        self.retired.append((now_t, self.menu))
        keep = []
        for t0, old in self.retired:
            if now_t - t0 > 120 and not old.get_visible():
                old.destroy()
            else:
                keep.append((t0, old))
        self.retired = keep
        self.menu = menu

    def account_item(self, name, u, now, folders):
        i = self.ids.get(name)
        a = self.api.get(name, {})
        note = None
        if a.get("blocked", 0) > now:
            note = f"Usage API paused until {clock(a['blocked'])} (rate-limited)"
        elif a.get("error") and a["error"] != "no login token" and (not u or now - u.get("updated", 0) > STALE):
            note = f"Usage API: {a['error']}"
        card = AccountCard(name, i, u, now, bool(self.login and self.login.name == name),
                           self.settings["hide_emails"], note)
        it = Item()
        it.add(card)
        # Accounts are the left column and the panel icon is near the right screen edge, so open the
        # submenu leftwards, where it covers nothing. Only the item is right-to-left; its card is not.
        it.set_direction(Gtk.TextDirection.RTL)
        card.set_direction(Gtk.TextDirection.LTR)
        it.set_submenu(self.account_menu(name, folders, i is not None and "error" not in i, card.tone))
        align_top(it)
        return it

    def account_menu(self, name, folders, logged_in, tone="grey"):
        m = Gtk.Menu()
        busy = self.login is not None
        ident = self.ids.get(name) or {}
        cmd = "claude" if name == "main" else f"claude-{name}"

        # Header: who this is, and its command (click to copy).
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        head.get_style_context().add_class("ct-subhead")
        row = Gtk.Box(spacing=8)
        row.pack_start(dot(tone), False, False, 0)
        row.pack_start(lbl(name, "ct-title"), False, False, 0)
        if ident.get("plan"):
            plan = ident["plan"]
            row.pack_start(pill(plan, "purple" if plan.lower() in ("max", "team", "enterprise") else
                                "blue" if plan.lower() == "pro" else "grey"), False, False, 0)
        head.pack_start(row, False, False, 0)
        who = ident.get("email") or ident.get("error") or ""
        if who:
            e = lbl(mask_email(who) if self.settings["hide_emails"] and "email" in ident else who, "ct-small", "ct-dim")
            e.set_margin_start(18)
            head.pack_start(e, False, False, 0)
        crow = Gtk.Box(spacing=8, margin_start=18, margin_top=4)
        crow.pack_start(lbl(cmd, "ct-code"), False, False, 0)
        crow.pack_start(lbl("click to copy", "ct-small", "ct-dim"), False, False, 0)
        head.pack_start(crow, False, False, 0)
        hi = Item()
        hi.add(head)
        hi.connect("activate", lambda *_: self.copy(cmd))
        m.append(hi)

        projects = self.projects_menu(name, folders)
        if folders or projects:
            self.caption(m, "OPEN CLAUDE")
        if folders:
            self.action(m, "media-playback-start-symbolic", "Continue last session", lambda *_: self.open(name, folders[0], ["--continue"]),
                        sub=f"in {short(folders[0])}", tone="green")
        if projects:
            it = self.action(m, "folder-symbolic", "Recent Projects", None, sub="And sessions, pinned first")
            it.set_submenu(projects)
            it.set_direction(Gtk.TextDirection.RTL)  # opens leftwards, like this menu (see account_item)
            it.get_child().set_direction(Gtk.TextDirection.LTR)
            align_top(it)
            self.project_menus.append(projects)
        self.caption(m, "NEW SESSION")
        self.action(m, "folder-symbolic", "Choose a folder…", lambda *_: self.choose_folder(name), sub="Pick any project")
        extras = self.extras_menu()
        if extras:
            self.caption(m, "CLAUDE CODE")
            it = self.action(m, "application-x-addon-symbolic", "Plugins and Skills", None, sub="What is installed")
            it.set_submenu(extras)
            it.set_direction(Gtk.TextDirection.RTL)  # opens leftwards, like Recent Projects
            it.get_child().set_direction(Gtk.TextDirection.LTR)
            align_top(it)
            self.project_menus.append(extras)  # trimmed to the height like them
        self.caption(m, "ACCOUNT")
        self.action(m, "changes-prevent-symbolic", "Log in again…" if logged_in else "Log in…",
                    lambda *_: self.start_login(name, ident.get("email", "")),
                    tone=None if logged_in else "amber").set_sensitive(not busy)
        self.action(m, "utilities-terminal-symbolic", "Log in in a terminal…", lambda *_: self.acc(["login", name], HOME))
        if name != "main":
            self.action(m, "user-trash-symbolic", "Remove account…", lambda *_: self.remove_account(name),
                        tone="red").set_sensitive(not busy)
        return carded(m)

    def projects_menu(self, name, folders):
        """What is pinned, a few recent projects, then the recent sessions: as many as fit
        (build_menu drops the rest)."""
        m = Gtk.Menu()
        icon = "view-pin-symbolic" if Gtk.IconTheme.get_default().has_icon("view-pin-symbolic") else "starred-symbolic"
        now = time.time()

        def row(image, title, sub, cb, key, item, on, delete=None):
            it = self.action(m, image, title, cb, sub=sub)
            if delete:  # a bin at the very end; its click is checked before the pin's
                trash = Gtk.Image.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.MENU)
                trash.set_valign(Gtk.Align.CENTER)
                trash.set_tooltip_text("Delete this session")
                for c in ("ct-trash", "ct-red"):
                    trash.get_style_context().add_class(c)
                it.get_child().pack_end(trash, False, False, 0)
                it.connect("button-release-event", lambda it, ev: ev.x >= trash.translate_coordinates(it, 0, 0)[0] - 4
                           and (delete() or True))
            pin = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU)
            pin.set_valign(Gtk.Align.CENTER)
            pin.get_style_context().add_class("ct-pin")
            self.show_pin(pin, on)
            it.get_child().pack_end(pin, False, False, 0)
            it.connect("button-release-event", self.pin_clicked, pin, key, item)

        def project(p, on):
            row("folder-symbolic", os.path.basename(p) or p, short(os.path.dirname(p)) or "/",
                lambda *_: self.open(name, p), "pinned", p, on)

        running = {r["id"] for r in running_sessions()}  # an open session can't be deleted

        def session(x, on):
            where = short(x["cwd"]) + (f" · {ago(x['at'], now)}" if x.get("at") else "")
            row("chat-symbolic", x["title"], where, lambda *_: self.open(name, x["cwd"], ["--resume", x["id"]]),
                "pinned_sessions", {k: x[k] for k in ("id", "cwd", "title")}, on,
                None if x["id"] in running else lambda: self.delete_session(x))

        fresh = {x["id"]: x for x in self.sessions}  # a pinned session's title and time, if still in the history
        pinned_s = [fresh.get(x["id"], x) for x in self.settings["pinned_sessions"] if os.path.isdir(x["cwd"])]
        pinned_p = [p for p in self.settings["pinned"] if os.path.isdir(p)]
        if pinned_s or pinned_p:
            self.caption(m, "PINNED")
        for x in pinned_s:
            session(x, True)
        for p in pinned_p:
            project(p, True)
        projects = [f for f in folders if f != HOME and f not in pinned_p][:3]  # pin one to keep it; the room is for sessions
        if projects:
            self.caption(m, "RECENT PROJECTS")
        for p in projects:
            project(p, False)
        ids = {x["id"] for x in pinned_s}
        sessions = [x for x in self.sessions if x["id"] not in ids]
        if sessions:
            self.caption(m, "RECENT SESSIONS")
        for x in sessions:
            session(x, False)
        return carded(m) if m.get_children() else None

    def extras_menu(self):
        """The installed plugins and skills (the same for every account). A click opens the folder."""
        m = Gtk.Menu()

        def show(path):
            return lambda *_: subprocess.Popen(["xdg-open", path])
        plugins, skills = installed_plugins(), installed_skills()
        if plugins:
            self.caption(m, "PLUGINS")
        for x in plugins:
            it = self.action(m, "application-x-addon-symbolic", x["name"], show(x["path"]))
            it.set_tooltip_text(" · ".join(t for t in (x["from"], x["version"]) if t))
            badge = pill("on" if x["on"] else "off", "green" if x["on"] else "grey")
            badge.set_valign(Gtk.Align.CENTER)
            it.get_child().pack_end(badge, False, False, 0)
        if skills:
            self.caption(m, "SKILLS")
        for x in skills:
            self.action(m, "starred-symbolic", x["name"], show(x["path"])).set_tooltip_text(x["about"] or short(x["path"]))
        return carded(m) if m.get_children() else None

    def show_pin(self, pin, on):
        pin.set_tooltip_text("Unpin" if on else "Pin: keep at the top")
        getattr(pin.get_style_context(), "add_class" if on else "remove_class")("ct-amber")

    def pin_clicked(self, it, ev, pin, key, item):
        if ev.x < pin.translate_coordinates(it, 0, 0)[0] - 8:
            return False  # on the row itself: open it

        def ident(p):
            return p["id"] if isinstance(p, dict) else p
        pins = self.settings[key]
        on = not any(ident(p) == ident(item) for p in pins)
        self.set_setting(key, [p for p in pins if ident(p) != ident(item)] + ([item] if on else []))
        self.show_pin(pin, on)
        return True  # keeps the menu open; the row changes card when the menu is next built

    def settings_menu(self):
        m = Gtk.Menu()
        self.caption(m, "PANEL SHOWS")
        group = None
        for key, text, sub in (("session", "Session %", "main 92%"), ("both", "Session and weekly %", "main 92% · 43%"),
                               ("none", "Ring only", "Just the coloured ring")):
            r = Gtk.RadioMenuItem.new_from_widget(group)
            group = r
            r.add(two_lines(text, sub))
            r.set_active(self.settings["label"] == key)
            r.connect("toggled", lambda b, k=key: b.get_active() and self.set_setting("label", k))
            m.append(r)
        self.caption(m, "ALSO SHOW")
        self.check(m, "Account name", "label_account", "When you have several accounts")
        self.check(m, "Reset countdown", "label_reset", "Time until the session limit resets")
        self.check(m, "Today's cost", "label_cost", "API-equivalent, all accounts")
        if not XApp:
            self.caption(m, "Panel text needs Cinnamon's XApp status icon")
        self.caption(m, "NOTIFICATIONS")
        self.check(m, "Usage alerts", "alerts", "At 80% and 95%, and when a full limit resets")
        self.caption(m, "PRIVACY AND DATA")
        self.check(m, "Hide emails", "hide_emails", "Show dev*@*.com instead of the address")
        self.check(m, "Check usage via API when idle", "api", "Every 5 min while Claude Code isn't running")
        return carded(m, panel=True)  # it opens over the account cards

    def action(self, menu, icon, text, cb, sub=None, tone=None):
        it = Item()
        box = Gtk.Box(spacing=12)
        box.get_style_context().add_class("ct-action")
        img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU)
        img.set_valign(Gtk.Align.CENTER)
        for c in ("ct-icon",) + ((f"ct-{tone}",) if tone else ()):
            img.get_style_context().add_class(c)
        box.pack_start(img, False, False, 0)
        box.pack_start(two_lines(text, sub, tone) if sub else lbl(text, *([f"ct-{tone}"] if tone else [])), False, False, 0)
        it.add(box)
        if cb:
            it.connect("activate", cb)
        menu.append(it)
        return it

    def check(self, menu, text, key, sub=None):
        it = Gtk.CheckMenuItem()
        it.add(two_lines(text, sub) if sub else lbl(text))
        it.set_active(bool(self.settings[key]))
        it.connect("toggled", lambda b: self.set_setting(key, b.get_active()))
        menu.append(it)

    def copy(self, text):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        notify("Claude", f"Copied: {text}")

    def caption(self, menu, text):
        it = Item()
        it.starts = True  # of a card (see carded)
        it.add(lbl(text, "ct-caption", "ct-section"))
        it.set_sensitive(False)
        menu.append(it)

    def banner(self, menu, text, tone):
        it = Item()
        it.add(lbl(text, "ct-banner", f"ct-{tone}", "ct-small", wrap=True))
        menu.append(it)

    def widget_item(self, menu, widget):
        it = Item()
        it.add(widget)
        menu.append(it)
        return it

    def set_setting(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)
        if key == "api" and value:
            for n in accounts():
                self.maybe_fetch_api(n, time.time())
        GLib.idle_add(self.render)

    def refresh_now(self, *_):
        self.fetch_identities()
        self.refresh_stats()
        if self.settings["api"]:
            now = time.time()
            for n in accounts():
                u = usage(n) or {}
                if now - u.get("updated", 0) >= 60:  # Claude Code's own numbers are fresher; keep them
                    self.maybe_fetch_api(n, now, force=True)
        self.render()

    # ----- actions -----

    def open(self, name, cwd, args=()):
        self.acc(["run", name, *args], cwd, hold=False)

    def acc(self, args, cwd, hold=True):
        exe = claude_acc()
        if not exe:
            return self.error("claude-acc was not found in ~/.local/bin. Run linux/install.sh first.")
        err = run_in_terminal(cwd, [exe, *args], hold=hold)
        if err:
            self.error(err)

    def choose_folder(self, name):
        d = Gtk.FileChooserDialog(title=f"Open Claude ({name}) in…", action=Gtk.FileChooserAction.SELECT_FOLDER)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        d.set_current_folder(HOME)
        d.set_keep_above(True)
        if d.run() == Gtk.ResponseType.OK and d.get_filename():
            self.open(name, d.get_filename())
        d.destroy()

    def start_login(self, name, email=""):
        if self.login:
            return self.login.present()
        self.note = None
        self.login = LoginWindow(self, name, email)
        self.render()

    def login_finished(self, name, ok, msg):
        self.login = None
        if not ok:
            self.note = (f"{name}: {msg}", True)
            return self.render()

        def work():
            names = accounts()
            return {n: identity(n) for n in names}

        def done(ids):
            self.ids, self.ids_for, self.ids_time = ids, tuple(ids), time.time()
            me = ids.get(name, {})
            twin = next((n for n, i in ids.items() if n != name and i.get("email") and i.get("email") == me.get("email")), None)
            if "error" in me:
                self.note = (f"{name} is still not logged in.", True)
            elif twin:
                self.note = (f"{name} is the same account as {twin}. Log in again with a different one.", True)
            else:
                email = mask_email(me["email"]) if self.settings["hide_emails"] else me["email"]
                self.note = (f"{name} logged in as {email}.", False)
            notify("Claude", self.note[0])
            self.render()
        self.background(work, done)

    def add_account(self, *_):
        d = Gtk.Dialog(title="Add Claude account")
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Continue", Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        d.set_keep_above(True)
        box = d.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        intro = label("Pick a short name. You'll get a command <b>claude-&lt;name&gt;</b> that shares all your "
                      "history. Your browser opens next so you can sign in to that account.")
        intro.set_line_wrap(True)
        intro.set_max_width_chars(50)
        box.add(intro)
        name_e = Gtk.Entry(activates_default=True, placeholder_text="name, e.g. work")
        mail_e = Gtk.Entry(activates_default=True, placeholder_text="email (optional, pre-fills the login page)")
        box.add(name_e)
        box.add(mail_e)
        d.show_all()
        while d.run() == Gtk.ResponseType.OK:
            name, email = name_e.get_text().strip().lower(), mail_e.get_text().strip()
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", name) and name not in ("main", "acc") and name not in accounts():
                d.destroy()
                return self.create_account(name, email)
            name_e.grab_focus()
            self.error(f"'{name}' can't be used: it must be new, 1–20 lowercase letters, digits or -, "
                       "and not 'main' or 'acc'.")
        d.destroy()

    def create_account(self, name, email):
        exe = claude_acc()
        if not exe:
            return self.error("claude-acc was not found in ~/.local/bin. Run linux/install.sh first.")
        r = subprocess.run([exe, "add", name, "--no-login"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60)
        if r.returncode != 0:
            return self.error((r.stderr or r.stdout).strip() or "claude-acc add failed")
        self.start_login(name, email)

    def delete_session(self, x):
        self.menu.cancel()  # the menu holds the pointer; a dialog can't be used under it
        d = Gtk.MessageDialog(message_type=Gtk.MessageType.WARNING, text=f"Delete the session “{x['title']}”?",
                              secondary_text="Its conversation is moved to the trash, for all accounts.")
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Delete", Gtk.ResponseType.OK)
        d.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("destructive-action")
        d.set_keep_above(True)
        ok = d.run() == Gtk.ResponseType.OK
        d.destroy()
        if not ok:
            return
        try:
            for path in transcripts(x["id"]):
                for f in (path, path[:-len(".jsonl")]):  # the transcript, and its folder of side files
                    if os.path.exists(f):
                        Gio.File.new_for_path(f).trash(None)
        except GLib.Error as e:
            return self.error(f"Could not delete the session: {e.message}")
        self.hidden_at = 0  # rebuild the menu now, not in a few seconds
        self.set_setting("pinned_sessions", [p for p in self.settings["pinned_sessions"] if p["id"] != x["id"]])

    def remove_account(self, name):
        d = Gtk.MessageDialog(message_type=Gtk.MessageType.WARNING, text=f"Remove account {name}?",
                              secondary_text=f"This logs {name} out and deletes claude-{name}. "
                                             "Shared history and sessions are kept.")
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Remove", Gtk.ResponseType.OK)
        d.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("destructive-action")
        d.set_keep_above(True)
        ok = d.run() == Gtk.ResponseType.OK
        d.destroy()
        if not ok:
            return
        exe = claude_acc()

        def done(r):
            self.note = (f"Removed {name}.", False) if r.returncode == 0 else (f"Could not remove {name}.", True)
            self.ids.pop(name, None)
            self.render()
        self.background(lambda: subprocess.run([exe, "rm", name, "--yes"], capture_output=True, text=True,
                                               stdin=subprocess.DEVNULL), done)

    def error(self, msg):
        d = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=msg)
        d.set_keep_above(True)
        d.run()
        d.destroy()


def notify(title, body):
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Claude", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    if not sys.stderr.isatty():  # started from the menu or at login: keep errors and crashes
        os.makedirs(CACHE, exist_ok=True)
        sys.stderr = open(os.path.join(CACHE, "tray.log"), "a", buffering=1)
        if not os.environ.get("CLAUDE_TRAY_CHILD"):
            print(f"--- start {time.ctime()}", file=sys.stderr)
    import faulthandler
    faulthandler.enable(sys.stderr)
    if os.environ.get("CLAUDE_TRAY_CHILD"):
        sys.exit(Tray().run([]))
    # GTK occasionally segfaults inside its X11 event handling while menus open and close. Run the
    # tray as a child and start it again after a crash, so the panel icon doesn't just disappear.
    crashes = []
    while True:
        rc = subprocess.call([sys.executable, os.path.abspath(__file__)],
                             env={**os.environ, "CLAUDE_TRAY_CHILD": "1"})
        if -rc not in (signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS):  # Quit, killed, already running
            sys.exit(rc)
        now = time.time()
        crashes = [t for t in crashes if now - t < 600] + [now]
        print(f"--- crashed (signal {-rc}); {'restarting' if len(crashes) <= 5 else 'giving up'}", file=sys.stderr)
        if len(crashes) > 5:
            sys.exit(1)
        time.sleep(2)
