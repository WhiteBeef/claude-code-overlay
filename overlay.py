#!/usr/bin/env python3
"""Tiny always-on-top overlay showing whether Claude Code is working or needs you.

State comes from hook.py (one JSON file per session in ~/.claude/overlay/state).
Drag with the left mouse button, right-click for the menu.
"""
import os

# XWayland lets an override-redirect window sit above everything (even fullscreen video)
# and be positioned freely, which native Wayland doesn't allow.
os.environ.setdefault("GDK_BACKEND", "x11")

import fcntl
import glob
import json
import math
import signal
import sys
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

BASE_DIR = os.path.expanduser("~/.claude/overlay")
STATE_DIR = os.path.join(BASE_DIR, "state")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOCK_PATH = os.path.join(BASE_DIR, "overlay.lock")

HEIGHT = 30
PAD = 9
LOGO = 18
GAP = 7

CLAUDE = (0.851, 0.467, 0.341)  # #D97757
TEXT = (0.93, 0.93, 0.93)
DIM = (0.62, 0.62, 0.62)
GREEN = (0.45, 0.85, 0.52)
AMBER = (1.0, 0.66, 0.25)
GREY = (0.55, 0.55, 0.55)

# status -> (label, label colour, priority when several sessions are open)
STATUS = {
    "waiting": ("Needs you", AMBER, 4),
    "working": ("Working", TEXT, 3),
    "done": ("Your turn", GREEN, 2),
    "idle": ("Ready", DIM, 1),
    "offline": ("No session", GREY, 0),
}

# Ray lengths of the Claude spark, slightly irregular like the real logo.
RAYS = [1.0, 0.8, 0.94, 0.76, 0.98, 0.84, 0.9, 0.78, 1.0, 0.82, 0.92, 0.8]

CLK_TCK = os.sysconf("SC_CLK_TCK")


def boot_time():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime"):
                return int(line.split()[1])
    return 0


BOOT_TIME = boot_time()


def proc_stat(pid):
    """Return (ppid, starttime ticks) or None if the process is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def has_busy_child(pid, after):
    """True if the process has a child started after `after` that has run for over a second.

    Covers the gap between approving a permission prompt and the tool finishing
    (e.g. a long build), since Claude Code sends no hook event for an approval.
    """
    now = time.time()
    for children in glob.glob(f"/proc/{pid}/task/*/children"):
        try:
            with open(children) as f:
                kids = f.read().split()
        except OSError:
            continue
        for kid in kids:
            stat = proc_stat(kid)
            if not stat:
                continue
            started = BOOT_TIME + int(stat[1]) / CLK_TCK
            if started > after and now - started > 1.0:
                return True
    return False


_interrupt_cache = {}


def was_interrupted(transcript):
    """True if the transcript ends with Claude Code's "interrupted by user" marker.

    Pressing Esc or rejecting a tool ends the turn without a Stop hook.
    """
    try:
        mtime = os.path.getmtime(transcript)
    except (OSError, TypeError):
        return False, 0
    cached = _interrupt_cache.get(transcript)
    if cached and cached[0] == mtime:
        return cached[1], mtime
    result = False
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read()
        for line in reversed(tail.splitlines()):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            kind = entry.get("type")
            if kind == "assistant":
                break
            if kind == "user":
                result = "[Request interrupted by user" in json.dumps(entry.get("message"))
                break
    except OSError:
        pass
    _interrupt_cache[transcript] = (mtime, result)
    return result, mtime


def load_sessions():
    sessions = []
    now = time.time()
    for path in glob.glob(os.path.join(STATE_DIR, "*.json")):
        try:
            with open(path) as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        pid = s.get("pid")
        if pid:
            stat = proc_stat(pid)
            alive = stat is not None and stat[1] == s.get("pid_start")
        else:
            alive = now - s.get("updated", 0) < 6 * 3600
        if not alive:
            try:
                os.remove(path)
            except OSError:
                pass
            continue

        if s.get("status") in ("working", "waiting"):
            interrupted, mtime = was_interrupted(s.get("transcript"))
            if interrupted and mtime > s.get("updated", 0):
                s["status"], s["since"] = "done", mtime
        if s.get("status") == "waiting" and pid and has_busy_child(pid, s.get("since", now)):
            s["status"] = "working"
        sessions.append(s)
    return sessions


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def fmt_ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def draw_logo(cr, cx, cy, radius, angle, color, alpha):
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(angle)
    cr.set_source_rgba(*color, alpha)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_width(radius * 0.24)
    for i, length in enumerate(RAYS):
        a = i * 2 * math.pi / len(RAYS)
        cr.move_to(math.cos(a) * radius * 0.15, math.sin(a) * radius * 0.15)
        cr.line_to(math.cos(a) * radius * length, math.sin(a) * radius * length)
        cr.stroke()
    cr.restore()


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Overlay(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_keep_above(True)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)

        self.font = Pango.FontDescription.from_string("Sans Bold 9.5")
        self.small = Pango.FontDescription.from_string("Sans 8.5")
        self.sessions = []
        self.angle = 0.0
        self.last_frame = time.monotonic()
        self.drag = None
        self.width = 0
        self.tooltip = None

        self.menu = Gtk.Menu()
        for label, callback in (("Reset position", self.reset_position), ("Quit", Gtk.main_quit)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _w, cb=callback: cb())
            self.menu.append(item)
        self.menu.show_all()

        self.move(*self.load_position())
        self.resize(160, HEIGHT)
        self.refresh()
        GLib.timeout_add(500, self.refresh)
        GLib.timeout_add(33, self.tick)

    # --- position -------------------------------------------------------

    def load_position(self):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            x, y = int(cfg["x"]), int(cfg["y"])
        except (OSError, ValueError, KeyError, TypeError):
            return 16, 48
        screen = self.get_screen()
        x = min(max(0, x), max(0, screen.get_width() - 60))
        y = min(max(0, y), max(0, screen.get_height() - HEIGHT))
        return x, y

    def save_position(self):
        x, y = self.get_position()
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump({"x": x, "y": y}, f)
        except OSError:
            pass

    def reset_position(self):
        self.move(16, 48)
        self.save_position()

    def on_press(self, _widget, event):
        if event.button == 1:
            x, y = self.get_position()
            self.drag = (event.x_root - x, event.y_root - y)
        elif event.button == 3:
            self.menu.popup_at_pointer(event)
        return True

    def on_motion(self, _widget, event):
        if self.drag:
            self.move(int(event.x_root - self.drag[0]), int(event.y_root - self.drag[1]))
        return True

    def on_release(self, _widget, event):
        if event.button == 1 and self.drag:
            self.drag = None
            self.save_position()
        return True

    # --- state ----------------------------------------------------------

    def refresh(self):
        self.sessions = load_sessions()
        lines = []
        for s in sorted(self.sessions, key=lambda s: -STATUS.get(s.get("status"), STATUS["idle"])[2]):
            project = os.path.basename(s.get("cwd") or "") or "?"
            tool = f" ({s['tool']})" if s.get("status") == "working" and s.get("tool") else ""
            lines.append(f"{project}: {STATUS.get(s.get('status'), STATUS['idle'])[0]}{tool}")
        tooltip = "\n".join(lines) or "No Claude Code sessions"
        if tooltip != self.tooltip:
            self.tooltip = tooltip
            self.set_tooltip_text(tooltip)
        return True

    def current(self):
        """The most urgent session, or None."""
        if not self.sessions:
            return None
        return max(
            self.sessions,
            key=lambda s: (STATUS.get(s.get("status"), STATUS["idle"])[2], s.get("since", 0)),
        )

    # --- drawing --------------------------------------------------------

    def tick(self):
        now = time.monotonic()
        dt, self.last_frame = now - self.last_frame, now
        session = self.current()
        status = session.get("status") if session else "offline"
        if status == "working":
            self.angle += dt * 1.6
        elif status == "waiting":
            self.angle += dt * 0.4
        self.queue_draw()
        return True

    def layout(self, cr, text, font):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(font)
        layout.set_text(text, -1)
        return layout

    def on_draw(self, _widget, cr):
        t = time.time()
        session = self.current()
        status = session.get("status") if session else "offline"
        if status not in STATUS:
            status = "idle"
        session = session or {}
        label, color, _ = STATUS[status]

        detail = ""
        if status == "working":
            detail = fmt_duration(t - session.get("turn_start", t))
            if session.get("tool"):
                detail += f" · {session['tool']}"
        elif status in ("waiting", "done"):
            detail = fmt_ago(t - session.get("since", t))
        others = len(self.sessions) - 1
        if others > 0:
            detail += f"  +{others}"

        main = self.layout(cr, label, self.font)
        dots = self.layout(cr, "...", self.font)
        small = self.layout(cr, detail, self.small)
        main_w, main_h = main.get_pixel_size()
        dots_w = dots.get_pixel_size()[0] if status == "working" else 0
        small_w, small_h = small.get_pixel_size()
        width = PAD + LOGO + GAP + main_w + dots_w + (6 + small_w if detail else 0) + PAD + 2
        if width != self.width:
            self.width = width
            self.resize(width, HEIGHT)

        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        # background, with an amber pulse while Claude waits for you
        rounded_rect(cr, 0.5, 0.5, width - 1, HEIGHT - 1, HEIGHT / 2 - 1)
        cr.set_source_rgba(0.11, 0.11, 0.12, 0.88)
        cr.fill_preserve()
        if status == "waiting":
            pulse = 0.5 + 0.5 * math.sin(t * 5)
            cr.set_source_rgba(*AMBER, 0.35 + 0.65 * pulse)
            cr.set_line_width(1.5)
        else:
            cr.set_source_rgba(1, 1, 1, 0.12)
            cr.set_line_width(1)
        cr.stroke()

        # logo
        cx, cy = PAD + LOGO / 2, HEIGHT / 2
        radius = LOGO / 2
        logo_alpha = 1.0
        if status == "waiting":
            radius *= 0.88 + 0.12 * math.sin(t * 5)
        elif status in ("done", "idle", "offline"):
            logo_alpha = 0.72 + 0.28 * math.sin(t * 1.6)  # slow breathing: still alive
        draw_logo(cr, cx, cy, radius, self.angle, CLAUDE if status != "offline" else GREY, logo_alpha)

        # label
        x = PAD + LOGO + GAP
        y = (HEIGHT - main_h) / 2
        cr.move_to(x, y)
        if status == "working":
            # shimmer sweeping across the text, like Claude Code's own spinner
            PangoCairo.layout_path(cr, main)
            sweep = (t * 0.9) % 1.6 - 0.3
            gx = x + sweep * main_w
            grad = cairo.LinearGradient(gx - 22, 0, gx + 22, 0)
            grad.add_color_stop_rgba(0, *TEXT, 1)
            grad.add_color_stop_rgba(0.5, *CLAUDE, 1)
            grad.add_color_stop_rgba(1, *TEXT, 1)
            cr.set_source(grad)
            cr.fill()
        else:
            alpha = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 5)) if status == "waiting" else 1
            cr.set_source_rgba(*color, alpha)
            PangoCairo.show_layout(cr, main)
        x += main_w

        if status == "working":
            for i in range(3):
                dot = self.layout(cr, ".", self.font)
                phase = (t * 2.2 - i * 0.35) % 1.5
                cr.set_source_rgba(*TEXT, 1.0 if phase < 0.75 else 0.2)
                cr.move_to(x + i * dots_w / 3, y)
                PangoCairo.show_layout(cr, dot)
            x += dots_w

        if detail:
            cr.set_source_rgba(*DIM, 1)
            cr.move_to(x + 6, (HEIGHT - small_h) / 2 + 0.5)
            PangoCairo.show_layout(cr, small)
        return True


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Claude overlay is already running", file=sys.stderr)
        return 1

    window = Overlay()
    window.show_all()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: Gtk.main_quit())
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
