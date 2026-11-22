"""Whole-slide review: a slide at a time instead of a card at a time.

Every box on the slide starts covered. Click a box (or press Space for the next
one) to reveal it, then mark it right or wrong; each box is still its own Anki
card and is scheduled exactly as if it had been answered in the reviewer
(Again for wrong, Good for right). Boxes on the slide that are not due stay
covered, in a lighter grey, so they cannot give answers away, and can be peeked
at without being graded.

The box layout comes from the cards themselves: every card's front mask holds
all the boxes on its slide, with its own region drawn in the highlight stroke.

The window borrows the main PDF Occlusion window's look (its colours, stage and
segmented controls). Everything moving is painted with QPainter off one timer
per widget that only runs while something is animating; the stage, its shadow
and the scaled slide are cached as one pixmap per size, so a frame is a single
blit plus the boxes.
"""
import os
import re
import time
from typing import Optional

from aqt import mw
from aqt.qt import (QAbstractButton, QColor, QDialog, QFrame, QGridLayout,
                    QHBoxLayout, QLabel, QPainter, QPen, QPixmap, QPointF,
                    QRectF, QShortcut, QKeySequence, QSize, Qt,
                    QTimer, QVBoxLayout, QWidget)

_SHAPE = re.compile(r'<(rect|ellipse)\b([^>]*)/>')
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_GROUP_PAD = 4          # card_builder dilates grouped boxes by this much


def _shapes(svg: str):
    """(x, y, w, h, is_active) for every rect/ellipse in a mask SVG."""
    out = []
    for kind, attrs in _SHAPE.findall(svg):
        a = dict(_ATTR.findall(attrs))
        if kind == "rect":
            x, y, w, h = (float(a.get(k, 0)) for k in ("x", "y", "width", "height"))
        else:
            cx, cy, rx, ry = (float(a.get(k, 0)) for k in ("cx", "cy", "rx", "ry"))
            x, y, w, h = cx - rx, cy - ry, 2 * rx, 2 * ry
        # the tested region is the one drawn with the thick highlight stroke
        active = a.get("stroke", "none") != "none" and a.get("stroke-width") in ("3", "6")
        out.append((x, y, w, h, active))
    return out


def _media_name(field: str) -> Optional[str]:
    m = re.search(r'src="([^"]+)"', field or "")
    return m.group(1) if m else None


def slides_for(col, card_ids) -> list:
    """Cards grouped by slide: [{"image": fname, "W", "H", "regions": [{"cid", "rects"}], "others": [rects]}].
    A slide's regions are in reading order (top to bottom, then left to right),
    which is the order Space walks through them."""
    media = col.media.dir()
    by_slide: dict = {}
    for cid in card_ids:
        card = col.get_card(cid)
        note = card.note()
        if "Question Mask" not in note or "Image" not in note:
            continue
        img, mask = _media_name(note["Image"]), _media_name(note["Question Mask"])
        if not img or not mask or not os.path.exists(os.path.join(media, mask)):
            continue
        svg = open(os.path.join(media, mask), encoding="utf-8").read()
        size = re.search(r'width="(\d+)" height="(\d+)"', svg)
        W, H = (int(size.group(1)), int(size.group(2))) if size else (0, 0)
        shapes = _shapes(svg)
        mine = [s[:4] for s in shapes if s[4]]
        grouped = len(mine) > 1
        if grouped:   # undo the dilation card_builder applies to grouped boxes
            mine = [(x + _GROUP_PAD, y + _GROUP_PAD, w - 2 * _GROUP_PAD, h - 2 * _GROUP_PAD) for x, y, w, h in mine]
        s = by_slide.setdefault(img, {"image": img, "W": W, "H": H, "regions": [], "all": []})
        s["regions"].append({"cid": cid, "rects": mine})
        s["all"].extend(sh[:4] for sh in shapes if not sh[4])
    def near(r, rects):
        return any(abs(r[0] - d[0]) < 6 and abs(r[1] - d[1]) < 6 for d in rects)

    out = []
    for s in by_slide.values():
        due = [r for reg in s["regions"] for r in reg["rects"]]
        # boxes on the slide that are not due today: everything in the masks that is
        # not a due region, once each (every due card's mask lists them all again)
        s["others"] = []
        for r in s["all"]:
            if not near(r, due) and not near(r, s["others"]):
                s["others"].append(r)
        row = max(1.0, (s["H"] or 1000) * 0.03)

        def order(reg):
            if not reg["rects"]:
                return (0, 0)
            x, y, w, h = min(reg["rects"], key=lambda r: (r[1], r[0]))
            return (round((y + h / 2) / row), x)
        s["regions"].sort(key=order)
        out.append(s)
    return out


# ------------------------------------------------------------------ look --

def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _out(x: float) -> float:
    """Ease out (cubic)."""
    x = _clamp(x)
    return 1 - (1 - x) ** 3


def _lerp(a: float, b: float, x: float) -> float:
    return a + (b - a) * x


def _lerp_rect(a: QRectF, b: QRectF, x: float) -> QRectF:
    return QRectF(_lerp(a.x(), b.x(), x), _lerp(a.y(), b.y(), x),
                  _lerp(a.width(), b.width(), x), _lerp(a.height(), b.height(), x))


def _alpha(col: QColor, a: float) -> QColor:
    out = QColor(col)
    out.setAlphaF(_clamp(col.alphaF() * a))
    return out


def _palette() -> dict:
    """The main window's colours, plus the few this window adds. Imported
    here, not at the top, so the deck screen's due-card check stays light."""
    from .pdf_occlusion_dialog import _colors, _night
    c = dict(_colors())
    night = _night()
    try:
        rgb = tuple(mw.addonManager.getConfig(__name__).get("mask_color") or (120, 120, 120))[:3]
    except Exception:       # noqa: BLE001
        rgb = (120, 120, 120)
    mask = QColor(*rgb)
    # boxes that are not due: the mask colour washed toward white, still opaque
    other = QColor(*(int(v + (255 - v) * 0.45) for v in rgb))
    c.update({
        "night": night,
        "q_stage": QColor(c["stage"]),
        "q_accent": QColor(c["accent"]),
        "q_mask": mask,
        "q_mask_hover": QColor(*(int(v + (255 - v) * 0.12) for v in rgb)),
        "q_other": other,
        "q_other_hover": QColor(*(int(v + (255 - v) * 0.55) for v in rgb)),
        "q_good": QColor("#5aa877" if night else "#3d8b5a"),
        "q_again": QColor("#d46c63" if night else "#c0554b"),
        "q_icon": QColor(c["icon"]),
        "q_icon_off": QColor(c["icon_off"]),
        "q_hover": QColor(255, 255, 255, 22) if night else QColor(0, 0, 0, 14),
        "q_down": QColor(255, 255, 255, 34) if night else QColor(0, 0, 0, 26),
        "q_seg_on": QColor(c["seg_on"]),
        "q_seg_on_line": QColor(c["seg_on_line"]),
        "q_line": QColor(c["line"]),
        "q_window": QColor(c["window"]),
        "q_sub": QColor(c["sub"]),
    })
    return c


class _Anim:
    """Named animations on one widget, ticking at about 60 fps only while at
    least one of them is still running."""

    def __init__(self, widget: QWidget):
        self._w = widget
        self._runs: dict = {}           # name -> (start, seconds)
        self._timer = QTimer(widget)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def start(self, name, seconds: float):
        self._runs[name] = (time.monotonic(), seconds)
        if not self._timer.isActive():
            self._timer.start()
        self._w.update()

    def t(self, name) -> float:
        """0..1 through the animation; 1 once it has finished (or never ran)."""
        run = self._runs.get(name)
        if run is None:
            return 1.0
        return _clamp((time.monotonic() - run[0]) / run[1])

    def running(self, name) -> bool:
        return self.t(name) < 1.0

    def clear(self):
        self._runs.clear()
        self._timer.stop()

    def _tick(self):
        now = time.monotonic()
        self._runs = {k: v for k, v in self._runs.items() if now - v[0] < v[1]}
        if not self._runs:
            self._timer.stop()
        self._w.update()


# ----------------------------------------------------------------- slide --

class _SlideView(QWidget):
    """The slide on the stage with its boxes; reports clicks on a region."""

    MARGIN = 24             # room round the slide for its shadow, as in the main window
    REVEAL_S = 0.15
    RING_S = 0.22
    MOVE_S = 0.15
    PEEK_S = 0.12
    SLIDE_S = 0.22

    def __init__(self, parent, on_click):
        super().__init__(parent)
        self.on_click = on_click
        self.pixmap: Optional[QPixmap] = None
        self.slide = None
        self.revealed: set = set()      # cids revealed
        self.graded: dict = {}          # cid -> ease
        self.peeked: set = set()        # indexes into slide["others"]
        self.current: Optional[int] = None
        self.c = _palette()
        self.anim = _Anim(self)
        self._gen = 0                   # bumps per slide, keys the cache
        self._base: Optional[QPixmap] = None
        self._base_key = None
        self._snap: Optional[QPixmap] = None     # the previous slide, fading out
        self._cur_from: Optional[QRectF] = None  # where the highlight moves from
        self._hover = None              # ("r", i) or ("o", k)
        # while the window is being dragged to a new size the slide is scaled
        # the fast way; a moment after it stops, once more the smooth way
        self._resizing = False
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(120)
        self._settle.timeout.connect(self._settled)
        self.setMinimumSize(640, 360)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    # --- state ------------------------------------------------------------
    def set_slide(self, pixmap: QPixmap, slide: dict, animate: bool):
        if animate and self.slide is not None and self.isVisible():
            self._snap = self.grab()
        else:
            self._snap = None
        self.anim.clear()
        self.pixmap, self.slide = pixmap, slide
        self.revealed, self.graded, self.peeked = set(), {}, set()
        self.current = 0 if slide["regions"] else None
        self._cur_from = None
        self._hover = None
        self._gen += 1
        if self._snap is not None:
            self.anim.start("slide", self.SLIDE_S)
        self.update()

    def set_current(self, i: Optional[int]):
        if i == self.current:
            return
        old = self._region_rects(self.current)
        self._cur_from = old[0] if len(old) == 1 else None
        self.current = i
        self.anim.start("move", self.MOVE_S)

    def reveal(self, cid):
        if cid not in self.revealed:
            self.revealed.add(cid)
            self.anim.start(("rev", cid), self.REVEAL_S)

    def grade(self, cid, ease: int):
        self.graded[cid] = ease
        self.anim.start(("ring", cid), self.RING_S)

    def toggle_peek(self, k: int):
        self.peeked ^= {k}
        self.anim.start(("peek", k), self.PEEK_S)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._base is not None:
            self._resizing = True
            self._settle.start()

    def _settled(self):
        self._resizing = False
        self.update()

    # --- geometry ---------------------------------------------------------
    def _frame(self) -> QRectF:
        if not self.pixmap or self.pixmap.isNull():
            return QRectF()
        pw, ph = self.pixmap.width(), self.pixmap.height()
        m = self.MARGIN
        aw, ah = max(1, self.width() - 2 * m), max(1, self.height() - 2 * m)
        s = min(aw / pw, ah / ph)
        w, h = round(pw * s), round(ph * s)
        return QRectF(round((self.width() - w) / 2), round((self.height() - h) / 2), w, h)

    def _to_screen(self, r, f: Optional[QRectF] = None) -> QRectF:
        f = f or self._frame()
        sx, sy = f.width() / self.slide["W"], f.height() / self.slide["H"]
        return QRectF(f.x() + r[0] * sx, f.y() + r[1] * sy, r[2] * sx, r[3] * sy)

    def _region_rects(self, i: Optional[int], f: Optional[QRectF] = None) -> list:
        if i is None or not self.slide or i >= len(self.slide["regions"]):
            return []
        return [self._to_screen(r, f) for r in self.slide["regions"][i]["rects"]]

    def _hit(self, pos):
        if not self.slide or self.pixmap is None:
            return None
        f = self._frame()
        for i, reg in enumerate(self.slide["regions"]):
            if any(self._to_screen(r, f).adjusted(-2, -2, 2, 2).contains(pos) for r in reg["rects"]):
                return ("r", i)
        for k, r in enumerate(self.slide["others"]):
            if self._to_screen(r, f).contains(pos):
                return ("o", k)
        return None

    # --- painting ---------------------------------------------------------
    def _base_pixmap(self) -> QPixmap:
        """Stage, shadow and slide scaled to the window, in device pixels.
        Rebuilt only when the slide, the size or the screen changes."""
        dpr = self.devicePixelRatioF() or 1.0
        key = (self._gen, self.width(), self.height(), dpr, self._resizing)
        if self._base_key == key and self._base is not None:
            return self._base
        c = self.c
        base = QPixmap(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
        base.setDevicePixelRatio(dpr)
        base.fill(c["q_stage"])
        f = self._frame()
        if not f.isEmpty():
            p = QPainter(base)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            steps, peak = 18, c["shadow"]
            soft = f.translated(0, 5)
            for i in range(1, steps + 1):
                a = peak * (1 - i / (steps + 1)) ** 2
                p.setPen(QPen(QColor(0, 0, 0, int(a)), 1))
                p.drawRoundedRect(soft.adjusted(-i + .5, -i + .5, i - .5, i - .5), 2 + i, 2 + i)
            p.setPen(QPen(QColor(0, 0, 0, 22 if peak < 60 else 60), 1))
            p.drawRoundedRect(f.adjusted(-.5, -.5, .5, .5), 2, 2)
            scaled = self.pixmap.scaled(max(1, round(f.width() * dpr)), max(1, round(f.height() * dpr)),
                                        Qt.AspectRatioMode.IgnoreAspectRatio,
                                        Qt.TransformationMode.FastTransformation if self._resizing
                                        else Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            p.drawPixmap(f.topLeft(), scaled)
            p.end()
        self._base, self._base_key = base, key
        return base

    def paintEvent(self, _e):
        p = QPainter(self)
        c = self.c
        if not self.pixmap or not self.slide:
            p.fillRect(self.rect(), c["q_stage"])
            p.end()
            return
        base = self._base_pixmap()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        snap = self._snap if self.anim.running("slide") else None
        if snap is None:
            self._snap = None
            p.drawPixmap(0, 0, base)
            self._paint_boxes(p)
        else:
            # the old slide fades out drifting left, the new one fades in from the right
            e = _out(self.anim.t("slide"))
            p.fillRect(self.rect(), c["q_stage"])
            p.setOpacity(1 - e)
            p.drawPixmap(QPointF(-14 * e, 0), snap)
            p.setOpacity(e)
            p.translate(22 * (1 - e), 0)
            p.drawPixmap(0, 0, base)
            self._paint_boxes(p)
        p.end()

    def _paint_boxes(self, p: QPainter):
        c, A = self.c, self.anim
        f = self._frame()
        p.setPen(Qt.PenStyle.NoPen)

        # boxes that are not due: lighter, opaque; a peeked one leaves a faint outline
        for k, r in enumerate(self.slide["others"]):
            q = self._to_screen(r, f)
            e = _out(A.t(("peek", k)))
            cover = (1 - e) if k in self.peeked else e
            if cover > 0:
                fill = c["q_other_hover"] if self._hover == ("o", k) else c["q_other"]
                p.setBrush(_alpha(fill, cover))
                p.drawRect(q)
            if k in self.peeked and cover < 1:
                pen = QPen(_alpha(c["q_sub"], 0.7 * (1 - cover)), 1, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(q.adjusted(-2, -2, 2, 2), 3, 3)
                p.setPen(Qt.PenStyle.NoPen)

        cur_cid = None
        if self.current is not None:
            cur_cid = self.slide["regions"][self.current]["cid"]

        for i, reg in enumerate(self.slide["regions"]):
            cid = reg["cid"]
            rects = [self._to_screen(r, f) for r in reg["rects"]]
            if cid not in self.revealed:
                fill = c["q_mask_hover"] if self._hover == ("r", i) else c["q_mask"]
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(fill)
                for q in rects:
                    p.drawRect(q)
                continue
            # revealed: the mask lifts off, shrinking a little as it fades
            t = A.t(("rev", cid))
            if t < 1:
                e = _out(t)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_alpha(c["q_mask"], 1 - e))
                for q in rects:
                    s = 1 - 0.08 * e
                    w, h = q.width() * s, q.height() * s
                    p.drawRect(QRectF(q.center().x() - w / 2, q.center().y() - h / 2, w, h))
            ease = self.graded.get(cid)
            if ease:
                # a soft ring settles round the answer, green for right, red for wrong
                e = _out(A.t(("ring", cid)))
                col = c["q_good"] if ease > 1 else c["q_again"]
                off = 3 + 7 * (1 - e)
                for q in rects:
                    rr = q.adjusted(-off, -off, off, off)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(_alpha(col, 0.10 * e))
                    p.drawRoundedRect(rr, 4, 4)
                    p.setPen(QPen(_alpha(col, 0.9 * e), 2))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(rr, 4, 4)
            elif cid != cur_cid:
                # revealed but not marked yet: a quiet accent ring until it is
                p.setPen(QPen(_alpha(c["q_accent"], 0.4), 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                for q in rects:
                    p.drawRoundedRect(q.adjusted(-3, -3, 3, 3), 4, 4)

        # the current box: an accent ring that glides from the last one
        if cur_cid is not None and cur_cid not in self.graded:
            rects = self._region_rects(self.current, f)
            t = A.t("move")
            e = _out(t)
            pen = QPen(c["q_accent"], 2)
            p.setBrush(Qt.BrushStyle.NoBrush)
            if self._cur_from is not None and len(rects) == 1 and t < 1:
                p.setPen(pen)
                q = _lerp_rect(self._cur_from, rects[0], e)
                p.drawRoundedRect(q.adjusted(-3, -3, 3, 3), 4, 4)
            else:
                p.setPen(QPen(_alpha(c["q_accent"], e), 2))
                for q in rects:
                    p.drawRoundedRect(q.adjusted(-3, -3, 3, 3), 4, 4)

    # --- input ------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position() if hasattr(e, "position") else e.pos()
        hit = self._hit(pos)
        if hit is None:
            return
        kind, i = hit
        if kind == "r":
            self.on_click(i)
        else:
            self.toggle_peek(i)

    def mouseMoveEvent(self, e):
        pos = e.position() if hasattr(e, "position") else e.pos()
        hit = self._hit(pos)
        if hit and hit[0] == "r" and self.slide["regions"][hit[1]]["cid"] in self.graded:
            hit = None      # nothing left to do on a marked box
        if hit != self._hover:
            self._hover = hit
            self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, _e):
        if self._hover is not None:
            self._hover = None
            self.update()


# ------------------------------------------------------------ bottom bar --

class _GradeButton(QAbstractButton):
    """One side of the grade control: a drawn cross or check. Lit (raised,
    coloured) while a revealed box waits for a mark, flat grey otherwise."""

    FADE_S = 0.12
    FLASH_S = 0.16

    def __init__(self, kind: str, palette: dict, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.c = palette
        self.anim = _Anim(self)
        self._lit = False
        self.setFixedSize(52, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setEnabled(False)

    def sizeHint(self):
        return QSize(52, 32)

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if self.isEnabled() != self._lit:
            self._lit = self.isEnabled()
            self.anim.start("lit", self.FADE_S)

    def flash(self):
        """The press, shown when the mark came from the keyboard."""
        self.anim.start("flash", self.FLASH_S)

    def enterEvent(self, e):
        super().enterEvent(e)
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.update()

    def paintEvent(self, _e):
        c = self.c
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        e = _out(self.anim.t("lit"))
        lit = e if self._lit else 1 - e
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if lit > 0:
            p.setPen(QPen(_alpha(c["q_seg_on_line"], lit), 1))
            p.setBrush(_alpha(c["q_seg_on"], lit))
            p.drawRoundedRect(r, 6, 6)
        flash = 1 - _out(self.anim.t("flash")) if self.anim.running("flash") else 0
        if self.isEnabled() and (self.isDown() or flash > 0):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_alpha(c["q_down"], max(flash, 1.0 if self.isDown() else 0)))
            p.drawRoundedRect(r, 6, 6)
        elif self.isEnabled() and self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c["q_hover"])
            p.drawRoundedRect(r, 6, 6)
        on = c["q_again"] if self.kind == "again" else c["q_good"]
        off = c["q_icon_off"]
        ink = QColor(int(_lerp(off.red(), on.red(), lit)), int(_lerp(off.green(), on.green(), lit)),
                     int(_lerp(off.blue(), on.blue(), lit)))
        pen = QPen(ink, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = self.width() / 2, self.height() / 2
        if self.kind == "again":
            d = 5.0
            p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
            p.drawLine(QPointF(cx + d, cy - d), QPointF(cx - d, cy + d))
        else:
            p.drawPolyline([QPointF(cx - 6, cy + 0.5), QPointF(cx - 1.8, cy + 4.6),
                            QPointF(cx + 6.2, cy - 4.6)])
        p.end()


class _Bar(QFrame):
    """The window's foot: a thin progress line along its top edge, then the
    grade control centred and the slide count on the right."""

    LINE_S = 0.3

    def __init__(self, palette: dict, parent=None):
        super().__init__(parent)
        self.c = palette
        self.anim = _Anim(self)
        self._from = self._to = 0.0
        self.setFixedHeight(56)

    def set_progress(self, frac: float, animate: bool = True):
        frac = _clamp(frac)
        if abs(frac - self._to) < 1e-6:
            return
        self._from = self._now()
        self._to = frac
        if animate:
            self.anim.start("line", self.LINE_S)
        else:
            self._from = frac
            self.update()

    def _now(self) -> float:
        return _lerp(self._from, self._to, _out(self.anim.t("line")))

    def paintEvent(self, _e):
        c = self.c
        p = QPainter(self)
        p.fillRect(self.rect(), c["q_window"])
        p.fillRect(QRectF(0, 0, self.width(), 1), c["q_line"])
        w = self.width() * self._now()
        if w > 0:
            p.fillRect(QRectF(0, 0, w, 2), c["q_accent"])
        p.end()


class SlideReview(QDialog):
    """Review due PDF Occlusion cards a slide at a time."""

    ADVANCE_MS = 480        # time to see the last ring before the slide moves on

    def __init__(self, parent, card_ids):
        super().__init__(parent)
        self.setObjectName("slideReview")
        self.setWindowTitle("Whole Slide Review")
        self.col = mw.col
        self.slides = slides_for(self.col, card_ids)
        self.total = sum(len(s["regions"]) for s in self.slides) or 1
        self.done_n = 0
        self.index = -1
        self.cards: dict = {}
        self._advancing = False
        c = self.c = _palette()
        self.setStyleSheet(
            f"QDialog#slideReview {{ background:{c['stage']}; }}"
            f"QFrame#seg {{ background:{c['seg']}; border:none; border-radius:8px; }}"
            f"QLabel#count {{ color:{c['sub']}; font-size:13px; }}")

        self.view = _SlideView(self, self._reveal)
        self.wrong = _GradeButton("again", c)
        self.right = _GradeButton("good", c)
        self.wrong.setToolTip("Again (1)")
        self.right.setToolTip("Good (3)")
        self.wrong.clicked.connect(lambda: self._grade(1))
        self.right.clicked.connect(lambda: self._grade(3))
        seg = QFrame()
        seg.setObjectName("seg")
        srow = QHBoxLayout(seg)
        srow.setContentsMargins(3, 3, 3, 3)
        srow.setSpacing(2)
        srow.addWidget(self.wrong)
        srow.addWidget(self.right)
        self.count = QLabel()
        self.count.setObjectName("count")
        self.count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.bar = _Bar(c, self)
        grid = QGridLayout(self.bar)
        grid.setContentsMargins(18, 2, 18, 0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        grid.addWidget(seg, 0, 1, Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self.count, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.bar)
        QShortcut(QKeySequence("Space"), self, activated=self._space)
        QShortcut(QKeySequence("1"), self, activated=lambda: self._grade(1, key=True))
        QShortcut(QKeySequence("3"), self, activated=lambda: self._grade(3, key=True))
        self.resize(1200, 780)
        self._next_slide()

    # --- flow -----------------------------------------------------------
    def _next_slide(self):
        self._advancing = False
        self.index += 1
        if self.index >= len(self.slides):
            self.accept()
            return
        s = self.slides[self.index]
        path = os.path.join(self.col.media.dir(), s["image"])
        pm = QPixmap(path)
        if not s["W"]:
            s["W"], s["H"] = pm.width(), pm.height()
        self.view.set_slide(pm, s, animate=self.index > 0)
        self.cards = {}
        for reg in s["regions"]:
            c = self.col.get_card(reg["cid"])
            c.start_timer()
            self.cards[reg["cid"]] = c
        self.count.setText(f"{self.index + 1} of {len(self.slides)}")
        self.bar.set_progress(self.done_n / self.total, animate=self.index > 0)
        self._sync()

    def _reveal(self, i: int):
        reg = self.view.slide["regions"][i]
        if reg["cid"] in self.view.graded or self._advancing:
            return
        self.view.set_current(i)
        self.view.reveal(reg["cid"])
        self._sync()

    def _space(self):
        """Reveal the current box; a second press marks it right."""
        if self.view.current is None or self._advancing:
            return
        cid = self.view.slide["regions"][self.view.current]["cid"]
        if cid in self.view.revealed:
            self._grade(3, key=True)
        else:
            self._reveal(self.view.current)

    def _grade(self, ease: int, key: bool = False):
        i = self.view.current
        if i is None or self._advancing:
            return
        reg = self.view.slide["regions"][i]
        cid = reg["cid"]
        if cid not in self.view.revealed or cid in self.view.graded:
            return
        answer_card(self.col, self.cards[cid], ease)
        if key:
            (self.wrong if ease == 1 else self.right).flash()
        self.view.grade(cid, ease)
        self.done_n += 1
        self.bar.set_progress(self.done_n / self.total)
        regions = self.view.slide["regions"]
        left = [k for k, r in enumerate(regions) if r["cid"] not in self.view.graded]
        # the next box after this one in reading order, wrapping round
        nxt = next((k for k in left if k > i), left[0] if left else None)
        self.view.set_current(nxt)
        self._sync()
        if not left:
            self._advancing = True
            QTimer.singleShot(self.ADVANCE_MS, self._next_slide)

    def _sync(self):
        i = self.view.current
        regions = self.view.slide["regions"] if self.view.slide else []
        open_ = (i is not None and not self._advancing
                 and regions[i]["cid"] in self.view.revealed
                 and regions[i]["cid"] not in self.view.graded)
        self.wrong.setEnabled(open_)
        self.right.setEnabled(open_)
        self.view.update()

    def done(self, r):
        try:
            mw.reset()
        except Exception:   # noqa: BLE001
            pass
        super().done(r)


def answer_card(col, card, ease: int) -> None:
    """Schedule one card as if answered in the reviewer (1 = Again, 3 = Good)."""
    sched = col.sched
    if hasattr(sched, "answerCard"):
        sched.answerCard(card, ease)
    else:   # very new Anki: build the answer from the card's scheduling states
        states = sched.get_scheduling_states(card.id)
        answer = sched.build_answer(card=card, states=states, rating=ease - 1)
        sched.answer_card(answer)


def due_card_ids(col, deck_name: Optional[str] = None) -> list:
    """Due and new PDF Occlusion cards, optionally in one deck."""
    from .card_builder import is_own_note_type
    names = [nt["name"] for nt in col.models.all() if is_own_note_type(nt)]
    if not names:
        return []
    nts = " or ".join(f'"note:{n}"' for n in names)
    q = f"({nts}) (is:due or is:new) -is:suspended"
    if deck_name:
        q = f'"deck:{deck_name}" ' + q
    return list(col.find_cards(q))
