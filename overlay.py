#!/usr/bin/env python3
"""Tiny always-on-top overlay showing whether Claude Code is working or needs you.

State comes from hook.py (one JSON file per session in ~/.claude/overlay/state) and
plan limits from statusline.py. Settings live in ~/.claude/overlay/config.json and
are reloaded as soon as the file changes.

Drag to move. A single click shows the gear (settings) and close buttons; right-click
opens the menu.
"""
import contextlib
import math
import os
import signal
import sys
import time

# XWayland lets a bypass-window-manager window sit above everything (even fullscreen
# video) and be positioned freely, which native Wayland doesn't allow.
if sys.platform.startswith("linux") and os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    ANSWER_DIR,
    CONFIG_PATH,
    DEFAULT_CONFIG,
    HEARTBEAT_PATH,
    LIMITS_PATH,
    LOCK_PATH,
    POSITION_PATH,
    STATE_DIR,
    has_busy_child,
    is_alive,
    load_config,
    psutil,
    read_json,
    was_interrupted,
    write_json,
)

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

HEIGHT = 30
PAD = 9
LOGO = 18
GAP = 7

# status -> priority when several sessions are open
PRIORITY = {"waiting": 4, "working": 3, "done": 2, "idle": 1, "offline": 0}

# Ray lengths of the Claude spark, slightly irregular like the real logo.
RAYS = [1.0, 0.8, 0.94, 0.76, 0.98, 0.84, 0.9, 0.78, 1.0, 0.82, 0.92, 0.8]


# --- data ----------------------------------------------------------------------


def hook_alive(request):
    if psutil is None:
        return True
    try:
        return psutil.pid_exists(request.get("pid"))
    except (TypeError, ValueError):
        return False


def load_sessions():
    sessions = []
    now = time.time()
    if not os.path.isdir(STATE_DIR):
        return sessions
    for name in os.listdir(STATE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(STATE_DIR, name)
        s = read_json(path)
        if not isinstance(s, dict) or "status" not in s:
            continue
        pid = s.get("pid")
        alive = is_alive(pid, s.get("pid_start")) if pid else now - s.get("updated", 0) < 6 * 3600
        if not alive:
            for p in (path, path[:-5] + ".lock"):
                with contextlib.suppress(OSError):
                    os.remove(p)
            continue

        interrupted, mtime = False, 0
        if s.get("status") in ("working", "waiting") or s.get("requests"):
            interrupted, mtime = was_interrupted(s.get("transcript"))
            interrupted = interrupted and mtime > s.get("updated", 0)
        s["requests"] = [
            r for r in (s.get("requests") or {}).values()
            if hook_alive(r) and not (interrupted and mtime > r.get("created", 0))
        ]
        if interrupted:
            s["status"], s["since"] = "done", mtime
        if s["requests"]:
            if s.get("status") != "waiting":
                s["status"], s["since"] = "waiting", min(r["created"] for r in s["requests"])
        elif s.get("status") == "waiting" and pid and has_busy_child(pid, s.get("since", now)):
            s["status"] = "working"
        if s.get("status") not in PRIORITY:
            s["status"] = "idle"
        sessions.append(s)
    sessions.sort(key=lambda s: (PRIORITY[s["status"]], s.get("since", 0)), reverse=True)
    return sessions


def load_limits():
    data = read_json(LIMITS_PATH) or {}
    now = time.time()
    limits = {}
    for key in ("five_hour", "seven_day"):
        window = data.get(key)
        if not isinstance(window, dict) or window.get("used_percentage") is None:
            continue
        reset = window.get("resets_at") or 0
        if reset and reset < now:  # the window rolled over since Claude Code last reported
            limits[key] = (0.0, None)
        else:
            limits[key] = (float(window["used_percentage"]), reset or None)
    return limits


def send_answer(request_id, answer):
    os.makedirs(ANSWER_DIR, exist_ok=True)
    write_json(os.path.join(ANSWER_DIR, f"{request_id}.json"), answer)


def project_name(session):
    return os.path.basename((session.get("cwd") or "").rstrip("/\\")) or "?"


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


def fmt_left(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}m"


def rgba(color, alpha):
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {int(alpha * 255)})"


# --- pill ----------------------------------------------------------------------


class Pill(QWidget):
    """One status pill. The first pill also carries the plan limits and the controls."""

    def __init__(self, overlay, header):
        super().__init__()
        self.overlay = overlay
        self.header = header
        self.session = None
        self.others = 0
        self.hover = None
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def set_session(self, session, others):
        self.session = session
        self.others = others
        self.setToolTip(self.overlay.tooltip if self.header else "")

    # --- content ---------------------------------------------------------------

    def status(self):
        return self.session["status"] if self.session else "offline"

    def segments(self, t):
        """Small dim text after the label, as (text, colour) pairs."""
        cfg, colors = self.overlay.config, self.overlay.colors
        show = cfg["show"]
        s, status = self.session or {}, self.status()
        parts = []
        if self.session and (show["project"] or cfg["sessions"] == "all"):
            parts.append((project_name(s), colors["text"]))
        if show["elapsed"]:
            if status == "working":
                parts.append((fmt_duration(t - s.get("turn_start", t)), colors["dim"]))
            elif status in ("waiting", "done"):
                parts.append((fmt_ago(t - s.get("since", t)), colors["dim"]))
        if show["tool"] and status == "working" and s.get("tool"):
            parts.append((s["tool"], colors["dim"]))
        if self.others > 0:
            parts.append((f"+{self.others}", colors["dim"]))
        if self.header:
            limits = self.overlay.limits
            for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
                if show[key] and key in limits:
                    pct = limits[key][0]
                    color = (colors["alert"] if pct >= cfg["limit_alert"]
                             else colors["waiting"] if pct >= cfg["limit_warn"] else colors["done"])
                    parts.append((f"{label} {pct:.0f}%", color))
            if show["reset_in"] and limits.get("five_hour", (0, None))[1]:
                parts.append((f"↻ {fmt_left(limits['five_hour'][1] - t)}", colors["dim"]))
        return parts

    def controls_width(self):
        o = self.overlay
        return int(o.h * 0.72) * 2 + 4 if self.header and o.controls_visible else 0

    def measure(self, t):
        o = self.overlay
        status = self.status()
        width = o.pad + o.logo + o.gap + o.fm_main.horizontalAdvance(o.config["labels"][status])
        if status == "working":
            width += o.fm_main.horizontalAdvance("...")
        for text, _ in self.segments(t):
            width += o.sep + o.fm_small.horizontalAdvance(text)
        return width + o.pad + 2 + self.controls_width()

    def sizeHint(self):
        return QSize(self.measure(time.time()), self.overlay.h)

    def control_rects(self):
        o = self.overlay
        size = int(o.h * 0.72)
        y = (o.h - size) / 2
        right = self.width() - o.pad + 3
        close = QRectF(right - size, y, size, size)
        gear = QRectF(right - 2 * size - 4, y, size, size)
        return gear, close

    def control_at(self, pos):
        if not (self.header and self.overlay.controls_visible):
            return None
        gear, close = self.control_rects()
        if gear.contains(QPointF(pos)):
            return "gear"
        if close.contains(QPointF(pos)):
            return "close"
        return None

    # --- painting --------------------------------------------------------------

    def paintEvent(self, _event):
        o = self.overlay
        t = time.time()
        status = self.status()
        colors, h, w = o.colors, o.h, self.width()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)

        # background, with a pulsing border while Claude waits for you
        bg = QColor(colors["background"])
        bg.setAlphaF(o.config["opacity"])
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), h / 2 - 1, h / 2 - 1)
        p.fillPath(path, bg)
        if status == "waiting":
            border = QColor(colors["waiting"])
            border.setAlphaF(0.35 + 0.65 * (0.5 + 0.5 * math.sin(t * 5)))
            p.setPen(QPen(border, 1.5))
        else:
            p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.drawPath(path)

        # logo
        radius = o.logo / 2
        alpha = 1.0
        if status == "waiting":
            radius *= 0.88 + 0.12 * math.sin(t * 5)
        elif status in ("done", "idle", "offline"):
            alpha = 0.72 + 0.28 * math.sin(t * 1.6)  # slow breathing: still alive
        logo_color = QColor(colors["accent"] if status != "offline" else colors["offline"])
        logo_color.setAlphaF(alpha)
        p.save()
        p.translate(o.pad + o.logo / 2, h / 2)
        p.rotate(math.degrees(o.angle))
        p.setPen(QPen(logo_color, radius * 0.24, Qt.SolidLine, Qt.RoundCap))
        for i, length in enumerate(RAYS):
            a = i * 2 * math.pi / len(RAYS)
            p.drawLine(QPointF(math.cos(a) * radius * 0.15, math.sin(a) * radius * 0.15),
                       QPointF(math.cos(a) * radius * length, math.sin(a) * radius * length))
        p.restore()

        # label
        label = o.config["labels"][status]
        x = o.pad + o.logo + o.gap
        baseline = (h + o.fm_main.ascent() - o.fm_main.descent()) / 2
        label_w = o.fm_main.horizontalAdvance(label)
        if status == "working":
            # shimmer sweeping across the text, like Claude Code's own spinner
            text_path = QPainterPath()
            text_path.addText(x, baseline, o.font_main, label)
            gx = x + ((t * 0.9) % 1.6 - 0.3) * label_w
            grad = QLinearGradient(gx - 22, 0, gx + 22, 0)
            grad.setColorAt(0, QColor(colors["text"]))
            grad.setColorAt(0.5, QColor(colors["accent"]))
            grad.setColorAt(1, QColor(colors["text"]))
            p.setPen(Qt.NoPen)
            p.fillPath(text_path, grad)
        else:
            color = QColor({"waiting": colors["waiting"], "done": colors["done"],
                            "idle": colors["dim"], "offline": colors["offline"]}[status])
            if status == "waiting":
                color.setAlphaF(0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 5)))
            p.setFont(o.font_main)
            p.setPen(color)
            p.drawText(QPointF(x, baseline), label)
        x += label_w

        if status == "working":
            p.setFont(o.font_main)
            dot_w = o.fm_main.horizontalAdvance(".")
            for i in range(3):
                phase = (t * 2.2 - i * 0.35) % 1.5
                color = QColor(colors["text"])
                color.setAlphaF(1.0 if phase < 0.75 else 0.2)
                p.setPen(color)
                p.drawText(QPointF(x + i * dot_w, baseline), ".")
            x += o.fm_main.horizontalAdvance("...")

        p.setFont(o.font_small)
        small_base = (h + o.fm_small.ascent() - o.fm_small.descent()) / 2 + 0.5
        for text, color in self.segments(t):
            x += o.sep
            p.setPen(QColor(color))
            p.drawText(QPointF(x, small_base), text)
            x += o.fm_small.horizontalAdvance(text)

        if self.header and o.controls_visible:
            self.paint_controls(p)
        p.end()

    def paint_controls(self, p):
        colors = self.overlay.colors
        gear, close = self.control_rects()
        for name, rect in (("gear", gear), ("close", close)):
            if self.hover == name:
                hl = QColor(colors["alert"] if name == "close" else colors["text"])
                hl.setAlphaF(0.22)
                p.setPen(Qt.NoPen)
                p.setBrush(hl)
                p.drawEllipse(rect)
        # gear: a ring of 8 square teeth around a disc with a hole
        c = gear.center()
        r = gear.width() * 0.36
        teeth = QPainterPath()
        for i in range(16):
            a0, a1 = (i - 0.5) * math.pi / 8, (i + 0.5) * math.pi / 8
            radius = r if i % 2 == 0 else r * 0.74
            if i == 0:
                teeth.moveTo(c.x() + math.cos(a0) * radius, c.y() + math.sin(a0) * radius)
            else:
                teeth.lineTo(c.x() + math.cos(a0) * radius, c.y() + math.sin(a0) * radius)
            teeth.lineTo(c.x() + math.cos(a1) * radius, c.y() + math.sin(a1) * radius)
        teeth.closeSubpath()
        teeth.addEllipse(c, r * 0.32, r * 0.32)
        p.setPen(Qt.NoPen)
        p.fillPath(teeth, QColor(colors["text"]))

        c = close.center()
        r = close.width() * 0.2
        pen = QPen(QColor(colors["text"]), max(1.4, r * 0.35), Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(c.x() - r, c.y() - r), QPointF(c.x() + r, c.y() + r))
        p.drawLine(QPointF(c.x() - r, c.y() + r), QPointF(c.x() + r, c.y() - r))

    # --- mouse -----------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.overlay.press(event.globalPosition().toPoint(), self.control_at(event.position().toPoint()))
        elif event.button() == Qt.RightButton:
            self.overlay.menu.popup(event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event):
        # touchpads report tap-and-drag as a double click; treat it as a fresh press
        self.mousePressEvent(event)

    def mouseMoveEvent(self, event):
        hover = self.control_at(event.position().toPoint())
        if hover != self.hover:
            self.hover = hover
            self.update()
        if event.buttons() & Qt.LeftButton:
            self.overlay.drag_to(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.overlay.release(self.control_at(event.position().toPoint()))

    def leaveEvent(self, _event):
        if self.hover:
            self.hover = None
            self.update()


# --- prompt card -----------------------------------------------------------------


class RequestCard(QFrame):
    """Buttons for a permission prompt or a question Claude is waiting on."""

    def __init__(self, overlay, session, request):
        super().__init__()
        self.request = request
        self.choices = {}
        self.setObjectName("card")
        self.setFixedWidth(int(340 * overlay.scale))
        layout = QVBoxLayout(self)
        m = int(10 * overlay.scale)
        layout.setContentsMargins(m, m - 2, m, m)
        layout.setSpacing(int(6 * overlay.scale))

        head = request["tool"] if request["kind"] == "permission" else (
            "Question" if request["kind"] == "question" else "Plan")
        if overlay.config["sessions"] == "all" or len(overlay.sessions) > 1:
            head += f" · {project_name(session)}"
        caption = QLabel(head)
        caption.setObjectName("caption")
        layout.addWidget(caption)

        if request["kind"] == "question":
            self.build_questions(layout, request["questions"])
        else:
            title = QLabel(self.shorten(request["title"]))
            title.setObjectName("code" if request["kind"] == "permission" else "")
            title.setWordWrap(True)
            title.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(title)
            row = QHBoxLayout()
            row.setSpacing(int(6 * overlay.scale))
            plan = request["kind"] == "plan"
            row.addWidget(self.button("Approve" if plan else "Allow", "primary", {"action": "allow"}))
            if request.get("always"):
                row.addWidget(self.button(request["always"], "", {"action": "always"}))
            row.addWidget(self.button("Keep planning" if plan else "Deny", "danger", {"action": "deny"}))
            row.addStretch(1)
            row.addWidget(self.button("Hide", "ghost", {"action": "defer"}))
            layout.addLayout(row)

    @staticmethod
    def shorten(text, lines=4, chars=280):
        parts = text.splitlines() or [""]
        text = "\n".join(parts[:lines])
        if len(text) > chars or len(parts) > lines:
            text = text[:chars].rstrip() + "…"
        return text

    def button(self, text, role, answer):
        b = QPushButton(text)
        b.setObjectName(role)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda: self.send(answer))
        return b

    def build_questions(self, layout, questions):
        instant = len(questions) == 1 and not questions[0]["multiSelect"]
        for q in questions:
            label = QLabel(q["question"])
            label.setWordWrap(True)
            layout.addWidget(label)
            for option in q["options"]:
                if instant:
                    b = self.button(option, "option", {"action": "answer", "answers": {q["question"]: option}})
                else:
                    b = QPushButton(option)
                    b.setObjectName("option")
                    b.setCheckable(True)
                    b.setCursor(Qt.PointingHandCursor)
                    b.toggled.connect(lambda on, q=q, o=option, btn=b: self.pick(q, o, on, btn))
                layout.addWidget(b)
        row = QHBoxLayout()
        if not instant:
            self.send_button = self.button("Send", "primary", None)
            self.send_button.clicked.disconnect()
            self.send_button.clicked.connect(self.send_choices)
            self.send_button.setEnabled(False)
            self.questions = questions
            row.addWidget(self.send_button)
        row.addStretch(1)
        row.addWidget(self.button("Answer in terminal", "ghost", {"action": "defer"}))
        layout.addLayout(row)

    def pick(self, question, option, on, button):
        chosen = self.choices.setdefault(question["question"], [])
        if on and option not in chosen:
            if not question["multiSelect"]:
                for other in self.findChildren(QPushButton):
                    if other is not button and other.isChecked() and other.text() in chosen:
                        other.setChecked(False)
                chosen.clear()
            chosen.append(option)
        elif not on and option in chosen:
            chosen.remove(option)
        self.send_button.setEnabled(all(self.choices.get(q["question"]) for q in self.questions))

    def send_choices(self):
        answers = {q["question"]: ", ".join(self.choices[q["question"]]) for q in self.questions}
        self.send({"action": "answer", "answers": answers})

    def send(self, answer):
        send_answer(self.request["id"], dict(answer, id=self.request["id"]))
        self.setEnabled(False)


# --- settings ----------------------------------------------------------------------


class Settings(QFrame):
    """Popup with the most common options; everything else is in config.json."""

    def __init__(self, overlay):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint)
        self.overlay = overlay
        self.setObjectName("settings")
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        cfg = overlay.config
        form = QFormLayout(self)
        form.setContentsMargins(14, 12, 14, 12)
        form.setVerticalSpacing(6)

        title = QLabel("Overlay settings")
        title.setObjectName("heading")
        form.addRow(title)

        sessions = QComboBox()
        sessions.addItem("Most important", "top")
        sessions.addItem("All sessions", "all")
        sessions.setCurrentIndex(0 if cfg["sessions"] == "top" else 1)
        sessions.currentIndexChanged.connect(lambda _i: self.set(("sessions",), sessions.currentData()))
        form.addRow("Show", sessions)

        for key, text in (("five_hour", "5-hour limit, %"), ("seven_day", "Weekly limit, %"),
                          ("reset_in", "Time to 5-hour reset"), ("elapsed", "Elapsed time"),
                          ("tool", "Current tool"), ("project", "Project name")):
            form.addRow(self.check(text, ("show", key)))
        form.addRow(self.check("Answer prompts from the overlay", ("interactive", "enabled")))

        controls = QComboBox()
        controls.addItem("Click", "click")
        controls.addItem("Hover", "hover")
        controls.setCurrentIndex(0 if cfg["controls"] == "click" else 1)
        controls.currentIndexChanged.connect(lambda _i: self.set(("controls",), controls.currentData()))
        form.addRow("Buttons on", controls)

        form.addRow("Size", self.slider(("scale",), 70, 200, cfg["scale"] * 100, 100))
        form.addRow("Opacity", self.slider(("opacity",), 30, 100, cfg["opacity"] * 100, 100))

        row = QHBoxLayout()
        edit = QPushButton("Edit config…")
        edit.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(CONFIG_PATH)))
        reset = QPushButton("Reset position")
        reset.clicked.connect(overlay.reset_position)
        row.addWidget(edit)
        row.addWidget(reset)
        form.addRow(row)

    def check(self, text, path):
        box = QCheckBox(text)
        box.setChecked(bool(self.get(path)))
        box.toggled.connect(lambda on: self.set(path, on))
        return box

    def slider(self, path, low, high, value, divisor):
        s = QSlider(Qt.Horizontal)
        s.setRange(low, high)
        s.setValue(int(value))
        s.valueChanged.connect(lambda v: self.set(path, round(v / divisor, 2)))
        return s

    def get(self, path):
        node = self.overlay.config
        for key in path:
            node = node[key]
        return node

    def set(self, path, value):
        node = self.overlay.config
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        self.overlay.save_config()


# --- window ----------------------------------------------------------------------


class Overlay(QWidget):
    def __init__(self):
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
        if QGuiApplication.platformName() == "xcb":
            flags |= Qt.X11BypassWindowManagerHint
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)

        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSizeConstraint(QVBoxLayout.SetFixedSize)
        self.sessions = []
        self.limits = {}
        self.tooltip = ""
        self.pills = []
        self.cards = {}
        self.order = None
        self.angle = 0.0
        self.last_frame = time.monotonic()
        self.controls_visible = False
        self.press_pos = None
        self.dragging = False
        self.last_beat = 0
        self.config_mtime = None
        self.settings = None

        self.toggle_timer = QTimer(self, singleShot=True, timeout=self.toggle_controls)
        self.hide_timer = QTimer(self, singleShot=True, timeout=self.auto_hide_controls)

        self.menu = QMenu()
        self.menu.addAction("Settings…", self.open_settings)
        self.menu.addAction("Reset position", self.reset_position)
        self.menu.addSeparator()
        self.menu.addAction("Quit", QApplication.quit)

        self.load_config()
        self.move(self.load_position())
        self.refresh()
        QTimer(self, interval=500, timeout=self.refresh).start()
        QTimer(self, interval=33, timeout=self.tick).start()

    # --- config ----------------------------------------------------------------

    def load_config(self):
        raw = read_json(CONFIG_PATH)
        if not isinstance(raw, dict) or "show" not in raw:
            # first run, or a config from the old version that only held the position
            if isinstance(raw, dict) and "x" in raw and not os.path.exists(POSITION_PATH):
                write_json(POSITION_PATH, {"x": raw["x"], "y": raw["y"]})
            self.config = load_config()
            self.save_config()
        else:
            self.config = load_config()
        with contextlib.suppress(OSError):
            self.config_mtime = os.path.getmtime(CONFIG_PATH)
        self.apply_config()

    def save_config(self):
        with contextlib.suppress(OSError):
            write_json(CONFIG_PATH, self.config, indent=2)
            self.config_mtime = os.path.getmtime(CONFIG_PATH)
        self.apply_config()

    def apply_config(self):
        cfg = self.config
        self.scale = max(0.5, min(3.0, float(cfg["scale"])))
        self.colors = {k: v if QColor.isValidColorName(v) else DEFAULT_CONFIG["colors"][k]
                       for k, v in cfg["colors"].items()}
        self.h = round(HEIGHT * self.scale)
        self.pad, self.logo, self.gap = round(PAD * self.scale), round(LOGO * self.scale), round(GAP * self.scale)
        self.sep = round(6 * self.scale)
        self.font_main = QFont(cfg["font"]) if cfg["font"] else QFont()
        self.font_main.setPointSizeF(9.5 * self.scale)
        self.font_main.setBold(True)
        self.font_small = QFont(self.font_main)
        self.font_small.setPointSizeF(8.5 * self.scale)
        self.font_small.setBold(False)
        self.fm_main, self.fm_small = QFontMetrics(self.font_main), QFontMetrics(self.font_small)
        self.setStyleSheet(self.stylesheet())
        for pill in self.pills:
            pill.setFixedHeight(self.h)
        for card in self.cards.values():
            card.deleteLater()
        self.cards = {}
        self.order = None

    def stylesheet(self):
        c, s, alpha = self.colors, self.scale, self.config["opacity"]
        font = int(9 * s)
        return f"""
            #card, #settings {{
                background: {rgba(c['background'], max(alpha, 0.92))};
                border: 1px solid {rgba(c['waiting'], 0.6)};
                border-radius: {int(12 * s)}px;
            }}
            #settings {{ border-color: rgba(255, 255, 255, 40); }}
            QLabel, QCheckBox {{ color: {c['text']}; font-size: {font}pt; }}
            QLabel#caption {{ color: {c['waiting']}; font-weight: bold; }}
            QLabel#heading {{ font-weight: bold; }}
            QLabel#code {{ font-family: monospace; color: {c['text']}; }}
            QPushButton {{
                color: {c['text']}; font-size: {font}pt;
                background: rgba(255, 255, 255, 20);
                border: 1px solid rgba(255, 255, 255, 38);
                border-radius: {int(7 * s)}px; padding: {int(4 * s)}px {int(10 * s)}px;
            }}
            QPushButton:hover {{ background: rgba(255, 255, 255, 46); }}
            QPushButton:disabled {{ color: {c['dim']}; }}
            QPushButton#primary, QPushButton#option:checked {{
                background: {c['accent']}; border-color: {c['accent']}; color: white;
            }}
            QPushButton#option {{ text-align: left; }}
            QPushButton#danger:hover {{ background: {rgba(c['alert'], 0.35)}; }}
            QPushButton#ghost {{ background: transparent; border: none; color: {c['dim']}; }}
            QComboBox, QSlider {{ font-size: {font}pt; min-width: {int(120 * s)}px; }}
        """

    def check_config(self):
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            return
        if mtime != self.config_mtime:
            self.config_mtime = mtime
            self.config = load_config()
            self.apply_config()

    # --- position --------------------------------------------------------------

    def load_position(self):
        pos = read_json(POSITION_PATH) or {}
        try:
            point = QPoint(int(pos["x"]), int(pos["y"]))
        except (KeyError, TypeError, ValueError):
            return QPoint(16, 48)
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(area.left(), point.x()), area.right() - 60)
        y = min(max(area.top(), point.y()), area.bottom() - self.h)
        return QPoint(x, y)

    def save_position(self):
        with contextlib.suppress(OSError):
            write_json(POSITION_PATH, {"x": self.x(), "y": self.y()})

    def reset_position(self):
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.left() + 16, area.top() + 48)
        self.save_position()

    # --- mouse -----------------------------------------------------------------

    def press(self, global_pos, control):
        self.toggle_timer.stop()  # second tap of a tap-and-drag
        self.press_pos = global_pos
        self.press_offset = global_pos - self.pos()
        self.press_control = control
        self.dragging = False

    def drag_to(self, global_pos):
        if self.press_pos is None:
            return
        if not self.dragging and (global_pos - self.press_pos).manhattanLength() > 4:
            self.dragging = True
        if self.dragging:
            self.move(global_pos - self.press_offset)

    def release(self, control):
        if self.press_pos is None:
            return
        self.press_pos = None
        if self.dragging:
            self.dragging = False
            self.save_position()
        elif control and control == self.press_control:
            if control == "gear":
                self.open_settings()
            else:
                QApplication.quit()
        elif self.config["controls"] == "click":
            # wait out the double-click interval so tap-and-drag doesn't toggle
            self.toggle_timer.start(QApplication.doubleClickInterval())

    def toggle_controls(self):
        self.set_controls(not self.controls_visible)

    def set_controls(self, visible):
        if visible == self.controls_visible:
            return
        self.controls_visible = visible
        if visible:
            self.hide_timer.start(6000)
        if self.pills:
            self.pills[0].updateGeometry()

    def auto_hide_controls(self):
        if self.underMouse() or (self.settings and self.settings.isVisible()):
            self.hide_timer.start(2000)
        else:
            self.set_controls(False)

    def enterEvent(self, _event):
        if self.config["controls"] == "hover":
            self.set_controls(True)

    def leaveEvent(self, _event):
        if self.config["controls"] == "hover":
            self.hide_timer.start(800)

    def open_settings(self):
        if self.settings:
            self.settings.close()
            self.settings.deleteLater()
        self.settings = Settings(self)
        self.settings.setStyleSheet(self.styleSheet())
        self.settings.adjustSize()
        pill = self.pills[0]
        pos = pill.mapToGlobal(QPoint(max(0, pill.width() - self.settings.width()), pill.height() + 6))
        area = (QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()).availableGeometry()
        pos.setX(min(max(area.left(), pos.x()), area.right() - self.settings.width()))
        if pos.y() + self.settings.height() > area.bottom():
            pos.setY(pill.mapToGlobal(QPoint(0, 0)).y() - self.settings.height() - 6)
        self.settings.move(pos)
        self.settings.show()

    # --- state -----------------------------------------------------------------

    def refresh(self):
        self.check_config()
        now = time.time()
        if now - self.last_beat > 2:
            self.last_beat = now
            with contextlib.suppress(OSError):
                with open(HEARTBEAT_PATH, "w") as f:
                    f.write(str(os.getpid()))
        self.sessions = load_sessions()
        self.limits = load_limits()
        self.tooltip = "\n".join(
            f"{project_name(s)}: {self.config['labels'][s['status']]}"
            + (f" ({s['tool']})" if s["status"] == "working" and s.get("tool") else "")
            for s in self.sessions
        ) or "No Claude Code sessions"
        self.rebuild()
        return True

    def rebuild(self):
        """Lay out one pill per shown session, each followed by cards for its prompts."""
        if self.config["sessions"] == "all":
            shown = self.sessions or [None]
        else:
            shown = self.sessions[:1] or [None]
        interactive = self.config["interactive"]["enabled"]
        order = []
        for i, session in enumerate(shown):
            order.append(("pill", i))
            if interactive and session:
                order.extend(("card", r["id"]) for r in sorted(session["requests"], key=lambda r: r["created"]))

        while len(self.pills) < len(shown):
            pill = Pill(self, header=not self.pills)
            pill.setFixedHeight(self.h)
            self.pills.append(pill)
        for i, session in enumerate(shown):
            others = len(self.sessions) - 1 if self.config["sessions"] == "top" else 0
            self.pills[i].set_session(session, others)
            self.pills[i].updateGeometry()

        wanted = {key[1] for key in order if key[0] == "card"}
        for request_id in list(self.cards):
            if request_id not in wanted:
                self.cards.pop(request_id).deleteLater()
        for session in shown:
            for r in (session or {}).get("requests", []) if interactive else []:
                if r["id"] not in self.cards:
                    self.cards[r["id"]] = RequestCard(self, session, r)

        if order != self.order:
            self.order = order
            while self.box.count():
                item = self.box.takeAt(0)
                if item.widget():
                    item.widget().hide()
            for kind, key in order:
                widget = self.pills[key] if kind == "pill" else self.cards[key]
                self.box.addWidget(widget, 0, Qt.AlignLeft)
                widget.show()
            self.box.setSpacing(int(5 * self.scale))
            for pill in self.pills[len(shown):]:
                pill.hide()

    def tick(self):
        now = time.monotonic()
        dt, self.last_frame = now - self.last_frame, now
        status = self.sessions[0]["status"] if self.sessions else "offline"
        if status == "working":
            self.angle += dt * 1.6
        elif status == "waiting":
            self.angle += dt * 0.4
        for pill in self.pills:
            if pill.isVisible():
                if pill.width() != pill.sizeHint().width():
                    pill.updateGeometry()
                pill.update()


# --- platform ------------------------------------------------------------------


def single_instance():
    """Hold an exclusive lock for the life of the process; None if another overlay has it."""
    f = open(LOCK_PATH, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def macos_float(widget):
    """Hide the Dock icon and keep the window above fullscreen apps on every Space."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p

        def send(obj, selector, *args, argtypes=()):
            objc.objc_msgSend.restype = ctypes.c_void_p
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
            return objc.objc_msgSend(obj, objc.sel_registerName(selector.encode()), *args)

        app = send(objc.objc_getClass(b"NSApplication"), "sharedApplication")
        send(app, "setActivationPolicy:", 1, argtypes=(ctypes.c_long,))  # accessory
        window = send(int(widget.winId()), "window")
        send(window, "setLevel:", 25, argtypes=(ctypes.c_long,))  # NSStatusWindowLevel
        # canJoinAllSpaces | stationary | fullScreenAuxiliary
        send(window, "setCollectionBehavior:", 1 | 16 | 256, argtypes=(ctypes.c_ulong,))
    except Exception:
        pass


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = single_instance()
    if lock is None:
        print("Claude overlay is already running", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = Overlay()
    window.show()
    macos_float(window)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    code = app.exec()
    with contextlib.suppress(OSError):
        os.remove(HEARTBEAT_PATH)  # hooks stop waiting for clicks right away
    lock.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
