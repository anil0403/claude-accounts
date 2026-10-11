"""Data for the claude-accounts tray: accounts, usage, today's activity, sessions. No GTK here."""
import glob
import json
import os
import re
import shutil
import subprocess
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
        "Accept": "application/json", "User-Agent": "claude-accounts-tray/1.0"})
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
    """Claude Code's installed plugins, enabled ones first: name, marketplace, version, folder."""
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
    return sorted(out, key=lambda x: (not x["on"], x["name"]))


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


def notify(title, body):
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Claude", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
