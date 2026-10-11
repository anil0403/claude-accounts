"""Tests for tray/core.py. Run: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import time
import unittest

HOME = tempfile.mkdtemp(prefix="claude-accounts-test-")
os.environ["HOME"] = HOME  # core reads ~/.claude and ~/.claude-accounts at import
os.environ.pop("XDG_CACHE_HOME", None)
os.environ.pop("XDG_CONFIG_HOME", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tray"))
import core  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


class Usage(unittest.TestCase):
    def test_window(self):
        now = 1000
        self.assertIsNone(core.window(None, "session", now))
        self.assertIsNone(core.window({"session": {}}, "session", now))
        self.assertEqual(core.window({"session": {"pct": 42.6, "resets_at": 2000}}, "session", now),
                         {"pct": 43, "resets": 2000})
        # a window that has reset reads 0%
        self.assertEqual(core.window({"session": {"pct": 90, "resets_at": 900}}, "session", now),
                         {"pct": 0, "resets": None})
        self.assertEqual(core.window({"weekly": {"pct": 140}}, "weekly", now)["pct"], 100)

    def test_pace(self):
        w = {"pct": 50, "resets": 1000 + core.SESSION_LEN // 2}
        self.assertAlmostEqual(core.pace(w, core.SESSION_LEN, 1000), 50)
        self.assertIsNone(core.pace({"pct": 1, "resets": None}, core.SESSION_LEN, 1000))

    def test_last_used(self):
        self.assertEqual(core.last_used({"updated": 5}), 5)
        self.assertEqual(core.last_used({"updated": 9, "source": "api", "live": 3}), 3)

    def test_best_account(self):
        now = 1000
        use = {"main": {"session": {"pct": 80, "resets_at": 5000}, "weekly": {"pct": 10, "resets_at": 9000}},
               "work": {"session": {"pct": 20, "resets_at": 5000}, "weekly": {"pct": 30, "resets_at": 9000}},
               "spare": {"session": {"pct": 0, "resets_at": 5000}, "weekly": {"pct": 0, "resets_at": 9000}}}
        ids = {"main": {"email": "a"}, "work": {"email": "b"}, "spare": {"error": "not logged in"}}
        self.assertEqual(core.best_account(["main", "work", "spare"], use, ids, now), "work")
        self.assertIsNone(core.best_account(["main"], use, ids, now))


class Format(unittest.TestCase):
    def test_mask_email(self):
        self.assertEqual(core.mask_email("developer@example.com"), "dev*@*.com")
        self.assertEqual(core.mask_email("ab@example.org"), "ab*@*.org")
        self.assertEqual(core.mask_email("x@localhost"), "x*@*")
        self.assertEqual(core.mask_email("not an email"), "not an email")
        self.assertEqual(core.mask_email(""), "")

    def test_dur(self):
        self.assertEqual(core.dur(30), "1m")
        self.assertEqual(core.dur(3 * 3600 + 5 * 60), "3h 05m")
        self.assertEqual(core.dur(2 * 86400 + 3600), "2d 1h")

    def test_price_longest_prefix(self):
        self.assertEqual(core.price("claude-opus-5-5"), (4, 20, 0.20))
        self.assertEqual(core.price("claude-opus-5"), (5, 25, 0.50))
        self.assertEqual(core.price("claude-sonnet-5-5"), (2, 10, 0.20))
        self.assertIsNone(core.price("gpt-5"))


class TodayStats(unittest.TestCase):
    def test_counts_and_dedupes(self):
        now = time.time()
        msg = {"id": "m1", "model": "claude-opus-5-5", "content": [
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {"content": "a\nb\nc"}}],
            "usage": {"input_tokens": 1000, "output_tokens": 10, "cache_read_input_tokens": 0}}
        streamed = dict(msg, usage=dict(msg["usage"], output_tokens=2000))
        lines = [
            {"type": "user", "timestamp": iso(now), "message": {"content": "hello"}},
            {"type": "user", "timestamp": iso(now), "message": {"content": [{"type": "tool_result"}]}},
            {"type": "assistant", "timestamp": iso(now), "requestId": "r1", "message": msg},
            {"type": "assistant", "timestamp": iso(now), "requestId": "r1", "message": streamed},
        ]
        write(os.path.join(HOME, ".claude", "projects", "p", "s.jsonl"),
              "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))  # compact, as Claude Code writes
        s = core.Stats().update()
        self.assertEqual(s["turns"], 1)            # the tool result isn't a prompt
        self.assertEqual(s["lines"], 3)            # counted once though logged twice
        self.assertEqual(s["replies"], {"Opus": 1})  # one reply, logged twice while streaming
        self.assertAlmostEqual(s["cost"], (1000 * 4 + 2000 * 20) / 1e6)  # the larger copy counts


class History(unittest.TestCase):
    def test_recent_sessions_use_titles(self):
        proj = os.path.join(HOME, "proj")
        os.makedirs(proj, exist_ok=True)
        write(os.path.join(HOME, ".claude", "history.jsonl"),
              json.dumps({"display": "first prompt", "project": proj, "sessionId": "s1", "timestamp": 1000}) + "\n"
              + json.dumps({"display": "gone", "project": proj, "sessionId": "s2", "timestamp": 2000}) + "\n")
        write(os.path.join(HOME, ".claude", "projects", "-proj", "s1.jsonl"),
              json.dumps({"type": "custom-title", "customTitle": "My session"}) + "\n")
        got = core.recent_sessions()
        self.assertEqual([x["id"] for x in got], ["s1"])  # s2 has no transcript any more
        self.assertEqual(got[0]["title"], "My session")
        self.assertEqual(core.recent_folders(), [proj])


if __name__ == "__main__":
    unittest.main()
