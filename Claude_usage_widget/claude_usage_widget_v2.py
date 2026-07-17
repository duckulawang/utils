"""
Claude Usage Widget v2 — floating always-on-top window for Windows
------------------------------------------------------------------
Shows ACCOUNT-WIDE quota (includes claude.ai web, mobile, and other
machines) by reusing your Claude Code login:

  1. GET /api/oauth/usage  (same read-only call Claude Code's /status
     makes — costs nothing)
  2. Fallback: 1-token Haiku probe, reading the
     anthropic-ratelimit-unified-* response headers
     (at most once per probe_min_interval_seconds)
  3. Fallback: local log estimation (this machine only)

Requires being logged into Claude Code on this machine (it reads the
OAuth token from ~/.claude/.credentials.json; set CLAUDE_CONFIG_DIR to
target a specific account). Unofficial mechanism — could break if
Anthropic changes it; the widget then degrades to local estimates.

No dependencies (stdlib only). Run with:  pythonw claude_usage_widget_v2.py
Drag to move. Right-click for menu (refresh / set limits / exit).

Data-source changes (server <-> local estimate) are logged to the console
with the reason. pythonw has no console, so run with `python` to see them.

NOTE: Numbers are estimates from local logs on THIS machine only.
Anthropic does not publish exact plan quotas; usage on other devices
or claude.ai chat is not visible here. Use /usage in Claude Code as
the authoritative view, and calibrate limits below to match it.
"""

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import simpledialog, messagebox
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ----------------------------- config ---------------------------------

CONFIG_PATH = Path(__file__).with_name("usage_widget_config.json")

DEFAULT_CONFIG = {
    # Set these after checking /usage in Claude Code, e.g. if /usage says
    # you're at 40% and the widget shows 2.0M weighted tokens, your limit
    # is ~5.0M. Leave 0 to hide the percentage bars.
    "session_limit_tokens": 0,
    "weekly_limit_tokens": 0,
    "refresh_seconds": 60,
    # Server mode: read account-wide usage (all devices + web) via your
    # Claude Code login. "oauth" endpoint costs nothing; the header probe
    # sends a 1-token Haiku message, so it's rate-limited by min interval.
    "server_mode": True,
    "probe_min_interval_seconds": 600,
    # After a 429 from the oauth endpoint, stop calling it for this long
    # instead of retrying every refresh (hammering can prolong the limit).
    "oauth_cooldown_seconds": 300,
    # When a live server fetch fails, keep showing the last good percentages
    # (marked stale) up to this age before dropping to local token counts.
    "max_stale_seconds": 900,
    "cache_read_weight": 0.1,   # cache reads are far cheaper; weight them down
    "opacity": 0.92,
    "x": 60,
    "y": 60,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# ------------------------- log scanning -------------------------------

def claude_projects_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def iter_usage_events(since):
    """Yield (timestamp_utc, weighted_tokens) from Claude Code JSONL logs."""
    root = claude_projects_dir()
    if not root.is_dir():
        return
    cutoff_mtime = since.timestamp() - 3600  # skip files untouched since window
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff_mtime:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("timestamp")
                    usage = (rec.get("message") or {}).get("usage")
                    if not ts or not isinstance(usage, dict):
                        continue
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if when.tzinfo is None:
                            when = when.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if when < since:
                        continue
                    yield when, usage
        except OSError:
            continue


def weighted_tokens(usage, cache_read_weight):
    return (
        usage.get("input_tokens", 0)
        + usage.get("output_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0) * cache_read_weight
    )


def compute_usage(cfg):
    """Return dict with session + weekly stats."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    events = sorted(iter_usage_events(week_ago), key=lambda e: e[0])

    w = cfg.get("cache_read_weight", 0.1)
    weekly_total = sum(weighted_tokens(u, w) for _, u in events)

    # Reconstruct 5h session windows: a window opens at the first event
    # after the previous window expired, and lasts exactly 5 hours.
    session_start = None
    session_total = 0.0
    for when, usage in events:
        if session_start is None or when >= session_start + timedelta(hours=5):
            session_start = when
            session_total = 0.0
        session_total += weighted_tokens(usage, w)

    in_session = session_start is not None and now < session_start + timedelta(hours=5)
    reset_at = session_start + timedelta(hours=5) if session_start else None

    return {
        "now": now,
        "in_session": in_session,
        "session_tokens": session_total if in_session else 0.0,
        "session_reset": reset_at if in_session else None,
        "weekly_tokens": weekly_total,
        "found_logs": claude_projects_dir().is_dir(),
        "event_count": len(events),
    }


# ---------------------- console logging --------------------------------

def log(msg):
    """Timestamped console line. Silently no-ops under pythonw, which has
    no stdout to write to."""
    stream = sys.stdout or sys.stderr
    if stream is None:
        return
    try:
        stream.write(f"[{datetime.now():%H:%M:%S}] {msg}\n")
        stream.flush()
    except (OSError, ValueError):
        pass


_last_line = {"msg": None}


def log_change(msg):
    """Log only when the message differs from the last one, so the 60s poll
    loop stays quiet while the state is steady."""
    if _last_line["msg"] == msg:
        return
    _last_line["msg"] = msg
    log(msg)


# ---------------------- server-side usage ------------------------------
# Account-wide utilization (includes web/mobile/other machines), obtained
# via the same mechanisms Claude Code itself uses. Unofficial — may break.

API = "https://api.anthropic.com"
OAUTH_BETA = "oauth-2025-04-20"
_last_probe = {"t": 0.0}
_why = {"oauth": "not tried yet", "probe": "not tried yet"}
# Skip the oauth endpoint until this monotonic-ish wall time after a 429.
_oauth_cooldown_until = {"t": 0.0}
# Last successful server reading, reused (marked stale) when a fetch fails.
_server_cache = {"windows": None, "source": None, "t": 0.0}


def load_oauth_token():
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    try:
        data = json.loads((base / ".credentials.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") or {}
    tok = oauth.get("accessToken")
    exp = oauth.get("expiresAt")  # ms epoch
    if tok and exp and exp / 1000 < datetime.now(timezone.utc).timestamp():
        return "EXPIRED"
    return tok


def _http(url, headers, body=None, timeout=12):
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        # headers are often present even on 4xx (esp. 429)
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, OSError):
        return None, {}, b""


def _norm_util(v):
    """Accept 0..1 fraction or 0..100 percent; return fraction 0..1."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f / 100.0 if f > 1.0 else f


def _extract_windows_from_json(obj):
    """Defensively pull {window: {utilization, resets_at}} from unknown JSON."""
    out = {}
    aliases = {"five_hour": "5h", "5h": "5h", "seven_day": "7d", "7d": "7d",
               "seven_day_sonnet": "7d_sonnet", "7d_sonnet": "7d_sonnet",
               "seven_day_opus": "7d_opus", "7d_opus": "7d_opus"}

    def walk(node, hint=None):
        if isinstance(node, dict):
            name = None
            raw = node.get("name") or node.get("claim") or node.get("window") or hint
            if isinstance(raw, str):
                name = aliases.get(raw.lower())
            util = node.get("utilization")
            if util is None and "used" in node and node.get("limit"):
                try:
                    util = float(node["used"]) / float(node["limit"])
                except (TypeError, ValueError, ZeroDivisionError):
                    util = None
            if name and util is not None:
                reset = node.get("resets_at") or node.get("reset") or node.get("resets")
                out[name] = {"utilization": _norm_util(util), "reset": reset}
            for k, v in node.items():
                walk(v, hint=k)
        elif isinstance(node, list):
            for item in node:
                walk(item, hint=hint)

    walk(obj)
    return out


def _parse_reset(val):
    if val is None:
        return None
    try:  # unix seconds
        return datetime.fromtimestamp(float(val), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        pass
    try:  # ISO string
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_oauth_usage(token, cfg):
    """Free read-only endpoint used by Claude Code's /status."""
    status, headers, body = _http(
        f"{API}/api/oauth/usage",
        {"Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA,
         "Content-Type": "application/json"})
    if status != 200:
        if status is None:
            _why["oauth"] = "no response (network/DNS)"
        elif status == 429:
            retry = {k.lower(): v for k, v in headers.items()}.get("retry-after")
            _why["oauth"] = f"HTTP 429 rate limited (retry-after: {retry})"
            # retry-after is usually 0/absent here, so use our own cooldown.
            cooldown = cfg.get("oauth_cooldown_seconds", 300)
            try:
                cooldown = max(cooldown, int(retry))
            except (TypeError, ValueError):
                pass
            _oauth_cooldown_until["t"] = (
                datetime.now(timezone.utc).timestamp() + cooldown)
        else:
            _why["oauth"] = f"HTTP {status}"
        return None
    try:
        wins = _extract_windows_from_json(json.loads(body))
    except json.JSONDecodeError:
        _why["oauth"] = "HTTP 200 but body was not JSON"
        return None
    if not wins:
        _why["oauth"] = "HTTP 200 but no usage windows found in payload"
        return None
    _why["oauth"] = "ok"
    return wins


def probe_rate_limit_headers(token, cfg):
    """Fallback: 1-token Haiku call; utilization comes back in headers.
    Rate-limited by probe_min_interval_seconds to avoid wasting quota."""
    now_ts = datetime.now(timezone.utc).timestamp()
    interval = cfg.get("probe_min_interval_seconds", 600)
    if now_ts - _last_probe["t"] < interval:
        wait = int(interval - (now_ts - _last_probe["t"]))
        _why["probe"] = f"skipped, next probe allowed in {wait}s"
        return None
    _last_probe["t"] = now_ts
    body = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 1,
                       "messages": [{"role": "user", "content": "."}]}).encode()
    status, headers, _ = _http(
        f"{API}/v1/messages",
        {"Authorization": f"Bearer {token}", "anthropic-beta": OAUTH_BETA,
         "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        body=body)
    if status is None:
        _why["probe"] = "no response (network/DNS)"
        return None
    hdrs = {k.lower(): v for k, v in headers.items()}
    wins = {}
    for key, val in hdrs.items():
        m = re.match(r"anthropic-ratelimit-unified-(5h|7d|7d_sonnet|7d_opus)-utilization", key)
        if m:
            win = m.group(1)
            wins.setdefault(win, {})["utilization"] = _norm_util(val)
            wins[win]["reset"] = hdrs.get(f"anthropic-ratelimit-unified-{win}-reset")
    if not wins:
        _why["probe"] = f"HTTP {status}, no ratelimit-unified-* headers returned"
        return None
    _why["probe"] = "ok"
    return wins


def fetch_server_usage(cfg):
    """Returns {'windows': {...}, 'source': str} or {'error': str} or None."""
    if not cfg.get("server_mode", True):
        log_change("LOCAL estimate (token counts) — server_mode is off in config")
        return None
    token = load_oauth_token()
    if token is None:
        log_change("LOCAL estimate (token counts) — no login found (.credentials.json)")
        return {"error": "no login found (.credentials.json)"}
    if token == "EXPIRED":
        log_change("LOCAL estimate (token counts) — login expired, open Claude Code to refresh")
        return {"error": "login expired — open Claude Code once to refresh"}

    now_ts = datetime.now(timezone.utc).timestamp()

    # 1. Free oauth endpoint — unless we're backing off after a 429.
    cooling = now_ts < _oauth_cooldown_until["t"]
    if cooling:
        _why["oauth"] = (f"cooling down after 429, "
                         f"{int(_oauth_cooldown_until['t'] - now_ts)}s left")
    else:
        wins = fetch_oauth_usage(token, cfg)
        if wins:
            _server_cache.update(windows=wins, source="server", t=now_ts)
            log_change("SERVER (percentages) — via GET /api/oauth/usage")
            return {"windows": wins, "source": "server"}

    # 2. Header probe (its own longer interval).
    wins = probe_rate_limit_headers(token, cfg)
    if wins:
        _server_cache.update(windows=wins, source="server (probe)", t=now_ts)
        log_change(f"SERVER (percentages) — via header probe; "
                   f"oauth endpoint: {_why['oauth']}")
        return {"windows": wins, "source": "server (probe)"}

    # 3. Both live paths failed. Reuse the last good server reading if it's
    #    still fresh, so a transient miss doesn't flip the whole display to
    #    token counts.
    age = now_ts - _server_cache["t"]
    if _server_cache["windows"] and age < cfg.get("max_stale_seconds", 900):
        log_change(f"SERVER (percentages, stale {int(age)}s) — live fetch "
                   f"failed; oauth: {_why['oauth']}, probe: {_why['probe']}")
        return {"windows": _server_cache["windows"],
                "source": f"{_server_cache['source']} · stale {int(age // 60)}m"}

    # 4. No fresh server data — fall back to local logs (token counts).
    log_change(f"LOCAL estimate (token counts) — oauth endpoint: {_why['oauth']}; "
               f"probe: {_why['probe']}")
    return None


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:.0f}"


def fmt_countdown(reset_at, now):
    delta = reset_at - now
    total = max(0, int(delta.total_seconds()))
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


# ----------------------------- UI -------------------------------------

BG = "#1e1e2e"
FG = "#cdd6f4"
DIM = "#7f849c"
ACCENT = "#89b4fa"
WARN = "#f9e2af"
DANGER = "#f38ba8"
BAR_BG = "#313244"


class UsageWidget(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.overrideredirect(True)          # frameless
        self.attributes("-topmost", True)    # always on top
        try:
            self.attributes("-alpha", self.cfg.get("opacity", 0.92))
        except tk.TclError:
            pass
        self.configure(bg=BG)
        self.geometry(f"+{self.cfg.get('x', 60)}+{self.cfg.get('y', 60)}")

        pad = {"padx": 12}
        self.title_lbl = tk.Label(self, text="Claude Code Usage", bg=BG, fg=ACCENT,
                                  font=("Segoe UI", 10, "bold"), anchor="w")
        self.title_lbl.pack(fill="x", pady=(8, 2), **pad)

        self.session_lbl = tk.Label(self, text="…", bg=BG, fg=FG,
                                    font=("Segoe UI", 9), anchor="w", justify="left")
        self.session_lbl.pack(fill="x", **pad)
        self.session_bar = self._make_bar()

        self.weekly_lbl = tk.Label(self, text="…", bg=BG, fg=FG,
                                   font=("Segoe UI", 9), anchor="w", justify="left")
        self.weekly_lbl.pack(fill="x", pady=(6, 0), **pad)
        self.weekly_bar = self._make_bar()

        self.foot_lbl = tk.Label(self, text="", bg=BG, fg=DIM,
                                 font=("Segoe UI", 7), anchor="w")
        self.foot_lbl.pack(fill="x", pady=(4, 8), **pad)

        # dragging
        for wdg in (self, self.title_lbl):
            wdg.bind("<Button-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)

        # right-click menu
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Refresh now", command=self.refresh)
        menu.add_command(label="Set session limit…", command=lambda: self._ask_limit("session_limit_tokens"))
        menu.add_command(label="Set weekly limit…", command=lambda: self._ask_limit("weekly_limit_tokens"))
        menu.add_separator()
        menu.add_command(label="Exit", command=self._quit)
        self.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

        log(f"Claude Usage Widget started — refresh every "
            f"{self.cfg.get('refresh_seconds', 60)}s, "
            f"server_mode={self.cfg.get('server_mode', True)}, "
            f"probe interval {self.cfg.get('probe_min_interval_seconds', 600)}s")
        self.refresh()

    def _make_bar(self):
        canvas = tk.Canvas(self, width=210, height=6, bg=BAR_BG,
                           highlightthickness=0)
        canvas.pack(padx=12, pady=(2, 0), anchor="w")
        return canvas

    def _draw_bar(self, canvas, frac):
        canvas.delete("all")
        if frac is None:
            return
        frac = max(0.0, min(frac, 1.0))
        color = ACCENT if frac < 0.7 else (WARN if frac < 0.9 else DANGER)
        canvas.create_rectangle(0, 0, 210 * frac, 6, fill=color, width=0)

    # ---- drag ----
    def _drag_start(self, e):
        self._ox, self._oy = e.x, e.y

    def _drag_move(self, e):
        x = self.winfo_x() + e.x - self._ox
        y = self.winfo_y() + e.y - self._oy
        self.geometry(f"+{x}+{y}")
        self.cfg["x"], self.cfg["y"] = x, y

    # ---- actions ----
    def _ask_limit(self, key):
        pretty = "session (5h)" if "session" in key else "weekly"
        val = simpledialog.askstring(
            "Set limit",
            f"Weighted-token limit for the {pretty} window\n"
            "(e.g. 5000000 or 5M — 0 hides the bar).\n"
            "Tip: calibrate against /usage in Claude Code.",
            parent=self)
        if val is None:
            return
        val = val.strip().upper().replace(",", "")
        try:
            mult = 1
            if val.endswith("M"):
                mult, val = 1_000_000, val[:-1]
            elif val.endswith("K"):
                mult, val = 1_000, val[:-1]
            self.cfg[key] = int(float(val) * mult)
            save_config(self.cfg)
            self.refresh()
        except ValueError:
            messagebox.showerror("Invalid value", "Please enter a number like 5000000, 5M, or 750K.")

    def _quit(self):
        log("Exiting Claude Usage Widget") # Add quit log message
        save_config(self.cfg)
        self.destroy()

    # ---- refresh loop ----
    def refresh(self):
        threading.Thread(target=self._scan, daemon=True).start()
        self.after(self.cfg.get("refresh_seconds", 30) * 1000, self.refresh)

    def _scan(self):
        try:
            server = fetch_server_usage(self.cfg)
        except Exception as exc:  # never crash the UI thread
            log(f"server usage lookup failed: {exc!r}")
            server = None
        try:
            stats = compute_usage(self.cfg)
        except Exception as exc:
            log(f"local log scan failed: {exc!r}")
            stats = {"error": str(exc)}
        stats["server"] = server
        self.after(0, lambda: self._render(stats))

    def _render(self, s):
        server = s.get("server")
        if server and server.get("windows"):
            self._render_server(server, s["now"])
            return
        if "error" in s:
            self.session_lbl.config(text=f"Error: {s['error']}", fg=DANGER)
            return
        if not s["found_logs"]:
            self.session_lbl.config(
                text="No Claude Code logs found\n(~/.claude/projects missing)", fg=WARN)
            self.weekly_lbl.config(text="")
            self._draw_bar(self.session_bar, None)
            self._draw_bar(self.weekly_bar, None)
            return

        # session
        if s["in_session"]:
            txt = (f"Session: {fmt_tokens(s['session_tokens'])} tokens"
                   f"  ·  resets in {fmt_countdown(s['session_reset'], s['now'])}")
        else:
            txt = "Session: idle — next prompt starts a fresh 5h window"
        lim = self.cfg.get("session_limit_tokens") or 0
        frac = (s["session_tokens"] / lim) if (lim and s["in_session"]) else None
        if frac is not None:
            txt += f"  ({frac * 100:.0f}%)"
        self.session_lbl.config(text=txt, fg=FG)
        self._draw_bar(self.session_bar, frac)

        # weekly
        wtxt = f"7-day: {fmt_tokens(s['weekly_tokens'])} tokens"
        wlim = self.cfg.get("weekly_limit_tokens") or 0
        wfrac = (s["weekly_tokens"] / wlim) if wlim else None
        if wfrac is not None:
            wtxt += f"  ({wfrac * 100:.0f}%)"
        self.weekly_lbl.config(text=wtxt, fg=FG)
        self._draw_bar(self.weekly_bar, wfrac)

        note = ""
        srv = s.get("server")
        if srv and srv.get("error"):
            note = f" · {srv['error']}"
        self.foot_lbl.config(
            text=f"local estimate · this machine only{note} · {s['now'].astimezone():%H:%M}")

    def _render_server(self, server, now):
        wins = server["windows"]

        def line(win, label):
            info = wins.get(win)
            if not info or info.get("utilization") is None:
                return None, None
            frac = info["utilization"]
            txt = f"{label}: {frac * 100:.0f}% used"
            reset = _parse_reset(info.get("reset"))
            if reset and reset > now:
                txt += f"  ·  resets in {fmt_countdown(reset, now)}"
            return txt, frac

        stxt, sfrac = line("5h", "Session (5h)")
        self.session_lbl.config(text=stxt or "Session (5h): n/a", fg=FG)
        self._draw_bar(self.session_bar, sfrac)

        wtxt, wfrac = line("7d", "Week (all models)")
        extra, _ = line("7d_sonnet", "Week (Sonnet)")
        if extra:
            wtxt = (wtxt or "") + "\n" + extra
        self.weekly_lbl.config(text=wtxt or "Week: n/a", fg=FG)
        self._draw_bar(self.weekly_bar, wfrac)

        self.foot_lbl.config(
            text=f"{server['source']} · account-wide (all devices + web) · "
                 f"{now.astimezone():%H:%M}")


if __name__ == "__main__":
    if sys.platform == "win32":
        try:  # crisp text on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    UsageWidget().mainloop()
