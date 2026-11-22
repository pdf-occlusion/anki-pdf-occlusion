"""Hover explainers for the top bar.

Rest the pointer on a control and, after a moment, a small card fades in
below it: a looping, abstract animation of what the control does, with its
name and shortcut underneath. Everything is painted with QPainter: a slide
is a rounded rectangle, text is rounded grey bars, a figure is a soft blob,
boxes are the app's own grey masks with the purple "this box" outline.

    install(widget, key, colors_fn)     # key: one of PAINTERS
    install_all(owner, [(attr, key), ...], colors_fn)

colors_fn() returns the window's colour dict (_LIGHT or _DARK in
pdf_occlusion_dialog). The card never takes focus or mouse clicks; its
timer only runs while it is on screen.
"""
import math
import time
from typing import Callable, Optional

from aqt.qt import (
    QApplication, QColor, QEvent, QFont, QFontMetrics, QKeySequence, QObject,
    QPainter, QPainterPath, QPen, QPoint, QPointF, QPolygonF, QRectF, Qt,
    QTimer, QWidget,
)


# ---------------------------------------------------------------- timing --

def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _ph(t: float, a: float, b: float) -> float:
    """0..1 progress of t through the span a..b."""
    return _clamp((t - a) / (b - a))


def _io(x: float) -> float:
    """Ease in and out (cubic)."""
    x = _clamp(x)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def _out(x: float) -> float:
    x = _clamp(x)
    return 1 - (1 - x) ** 3


def _back(x: float, s: float = 1.6) -> float:
    """Ease out with a small overshoot, for things that pop into place."""
    x = _clamp(x) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


def _lerp(a, b, x):
    return a + (b - a) * x


def _lerp_pt(a: QPointF, b: QPointF, x: float) -> QPointF:
    return QPointF(_lerp(a.x(), b.x(), x), _lerp(a.y(), b.y(), x))


def _lerp_rect(a: QRectF, b: QRectF, x: float) -> QRectF:
    return QRectF(_lerp(a.x(), b.x(), x), _lerp(a.y(), b.y(), x),
                  _lerp(a.width(), b.width(), x), _lerp(a.height(), b.height(), x))


def _scaled(r: QRectF, s: float) -> QRectF:
    c = r.center()
    w, h = r.width() * s, r.height() * s
    return QRectF(c.x() - w / 2, c.y() - h / 2, w, h)


# --------------------------------------------------------------- palette --

class _K:
    """The colours one frame is painted with, from the window's dict."""

    def __init__(self, c: dict):
        q = lambda v: QColor(v) if not isinstance(v, QColor) else QColor(v)
        self.card = q(c["window"])
        self.line = q(c["line"])
        self.text = q(c["text"])
        self.sub = q(c["sub"])
        self.stage = q(c["stage"])
        self.slide = q(c.get("placeholder", "#ffffff"))
        self.btn = q(c.get("btn", "#ffffff"))
        self.btn_line = q(c.get("btn_line", "#d2d2d7"))
        self.rail_on = q(c.get("rail_on", "#dcd6ee"))
        self.shadow = int(c.get("shadow", 26))
        self.night = self.card.lightness() < 128
        # on the slide, which is light in both themes
        self.slide_line = QColor(0, 0, 0, 22)
        self.bar = QColor(0, 0, 0, 34)
        self.title = QColor(0, 0, 0, 64)
        self.blob = QColor(0, 0, 0, 20)
        self.ink = QColor(28, 28, 30, 190)
        # the canvas's own mask and selection colours
        self.mask = QColor(120, 120, 120, 165)
        self.mask_line = QColor(72, 72, 72, 225)
        self.sel_fill = QColor(102, 102, 102, 175)
        self.accent = QColor(110, 90, 168)
        self.band = QColor(131, 110, 170, 40)
        self.band_line = QColor(131, 110, 170, 220)
        self.hint = QColor(131, 110, 170, 150)


def _a(col: QColor, alpha: float) -> QColor:
    out = QColor(col)
    out.setAlphaF(_clamp(col.alphaF() * alpha))
    return out


# ------------------------------------------------------------ primitives --

def _slide_rect(art: QRectF) -> QRectF:
    return art.adjusted(18, 12, -18, -12)


def _u(s: QRectF, x: float, y: float, w: float, h: float) -> QRectF:
    """A rect in slide units (0..1 across, 0..1 down)."""
    return QRectF(s.x() + x * s.width(), s.y() + y * s.height(),
                  w * s.width(), h * s.height())


def _up(s: QRectF, x: float, y: float) -> QPointF:
    return QPointF(s.x() + x * s.width(), s.y() + y * s.height())


def _rrect(p: QPainter, r: QRectF, radius: float, fill: Optional[QColor],
           line: Optional[QColor] = None, width: float = 1.0):
    p.setPen(QPen(line, width) if line is not None else Qt.PenStyle.NoPen)
    p.setBrush(fill if fill is not None else Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(r, radius, radius)


def _slide(p: QPainter, s: QRectF, k: _K, alpha: float = 1.0):
    _rrect(p, s.adjusted(.5, .5, -.5, -.5), 3, _a(k.slide, alpha),
           _a(k.slide_line, alpha))


def _bar(p: QPainter, r: QRectF, col: QColor):
    if r.width() <= 0.5:
        return
    h = r.height()
    _rrect(p, r, min(h / 2, r.width() / 2), col)


# The slide every animation starts from: a title, a figure, three labels.
_TITLE = (.08, .10, .40, .08)
_LABELS = [(.58, .30, .30, .075), (.58, .49, .24, .075), (.58, .68, .28, .075)]
_BOXES = [(.54, .2625, .38, .15), (.54, .4525, .32, .15), (.54, .6425, .36, .15)]


def _content(p: QPainter, s: QRectF, k: _K, alpha: float = 1.0,
             labels: bool = True, figure: bool = True):
    _bar(p, _u(s, *_TITLE), _a(k.title, alpha))
    if figure:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_a(k.blob, alpha))
        c = _up(s, .27, .60)
        r = .25 * s.height()
        p.drawEllipse(c, r * 1.12, r)
    if labels:
        for lab in _LABELS:
            _bar(p, _u(s, *lab), _a(k.bar, alpha))


def _text_slide(p: QPainter, s: QRectF, k: _K, alpha: float = 1.0):
    """A second, different-looking slide: a title and bullet lines."""
    _bar(p, _u(s, .08, .10, .48, .08), _a(k.title, alpha))
    for i, w in enumerate((.70, .58, .66, .44)):
        y = .32 + i * .15
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_a(k.bar, alpha))
        p.drawEllipse(_up(s, .11, y + .035), 1.6, 1.6)
        _bar(p, _u(s, .16, y, w, .07), _a(k.bar, alpha))


def _box(p: QPainter, r: QRectF, k: _K, alpha: float = 1.0, sel: float = 0.0):
    """An occlusion mask; sel 0..1 blends in the selected look."""
    if r.width() < 1 or r.height() < 1:
        return
    fill = QColor(k.mask)
    if sel > 0:
        f = k.sel_fill
        fill = QColor(int(_lerp(fill.red(), f.red(), sel)),
                      int(_lerp(fill.green(), f.green(), sel)),
                      int(_lerp(fill.blue(), f.blue(), sel)),
                      int(_lerp(fill.alpha(), f.alpha(), sel)))
    _rrect(p, r, 2, _a(fill, alpha), _a(k.mask_line, alpha * (1 - sel)), 1)
    if sel > 0:
        _rrect(p, r, 2, None, _a(k.accent, alpha * sel), 1.5)


def _handles(p: QPainter, r: QRectF, k: _K, alpha: float):
    if alpha <= 0:
        return
    p.setPen(QPen(_a(k.accent, alpha), 1.2))
    p.setBrush(_a(QColor(255, 255, 255), alpha))
    for pt in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
        p.drawRect(QRectF(pt.x() - 2, pt.y() - 2, 4, 4))


def _cursor(p: QPainter, tip: QPointF, alpha: float = 1.0, down: float = 0.0):
    """A small arrow pointer; down 0..1 presses it in slightly."""
    if alpha <= 0:
        return
    s = 0.78 * (1 - 0.08 * down)
    pts = [(0, 0), (0, 14), (3.6, 10.6), (6.2, 16.2), (8.4, 15.2),
           (5.9, 9.8), (10.6, 9.8)]
    poly = QPolygonF([QPointF(tip.x() + x * s, tip.y() + y * s) for x, y in pts])
    p.setPen(QPen(_a(QColor(255, 255, 255), alpha), 1.3,
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    p.setBrush(_a(QColor(29, 29, 31), alpha))
    p.drawPolygon(poly)


def _pop(x: float) -> tuple:
    """(scale, alpha) of something popping into place."""
    if x <= 0:
        return 0.0, 0.0
    return _lerp(.55, 1.0, _back(x)), _clamp(x * 2.5)


# -------------------------------------------------------------- painters --
#
# Each takes (painter, art rect, t in 0..1, palette). The art rect is the
# stage-coloured window inside the card; painters may clip to it.

def _paint_draw(p, art, t, k):
    s = _slide_rect(art)
    _slide(p, s, k)
    _content(p, s, k)
    fade = 1 - _ph(t, .86, .98)
    box = _u(s, *_BOXES[1])
    start, end = box.topLeft(), box.bottomRight()
    home = _up(s, .30, 1.02)
    if t < .22:
        cur = _lerp_pt(home, start, _io(_ph(t, .02, .20)))
    else:
        cur = _lerp_pt(start, end, _io(_ph(t, .24, .58)))
        away = _io(_ph(t, .64, .80))
        cur = cur + QPointF(10 * away, 8 * away)
    if t >= .22:
        drag = _lerp_pt(start, end, _io(_ph(t, .24, .58)))
        r = QRectF(start, drag).normalized()
        _box(p, r, k, fade, sel=1.0)
        _handles(p, r, k, fade * _ph(t, .58, .66))
    down = _ph(t, .20, .24) * (1 - _ph(t, .58, .62))
    _cursor(p, cur, fade, down)


def _paint_select(p, art, t, k):
    s = _slide_rect(art)
    _slide(p, s, k)
    _content(p, s, k)
    boxes = [_u(s, *_BOXES[0]), _u(s, *_BOXES[1])]
    a, b = _up(s, .47, .14), _up(s, .97, .68)
    home = _up(s, .36, .02)
    drag = _io(_ph(t, .20, .56))
    band = QRectF(a, _lerp_pt(a, b, drag)).normalized()
    pressing = .18 <= t < .58
    unsel = _ph(t, .84, .96)
    for r in boxes:
        # selected once the band covers it; the band only grows, so it stays
        sel = 1.0 if t >= .18 and band.contains(r) else 0.0
        _box(p, r, k, 1.0, sel * (1 - unsel))
    if pressing:
        _rrect(p, band, 1.5, k.band, k.band_line, 1)
    if t < .18:
        cur = _lerp_pt(home, a, _io(_ph(t, .02, .16)))
    else:
        cur = _lerp_pt(a, b, drag)
        away = _io(_ph(t, .62, .78))
        cur = cur + QPointF(8 * away, 8 * away)
    down = _ph(t, .16, .20) * (1 - _ph(t, .56, .60))
    _cursor(p, cur, 1 - _ph(t, .84, .96), down)


def _typed(p, x0, y, width, x, col):
    """A line of text typing in a word at a time; returns where the caret is."""
    n = math.floor(x * 5) / 5 if x < 1 else 1.0
    _bar(p, QRectF(x0, y, width * n, 5), col)
    return QPointF(x0 + width * n + 2, y)


def _caret(p, at, k, alpha, t):
    if at is not None and alpha > 0 and (t * 5.2) % 1 < .6:
        p.setPen(QPen(_a(k.accent, alpha), 1.2))
        p.drawLine(at + QPointF(0, -1), at + QPointF(0, 6))


def _paint_text(p, art, t, k):
    """Two things the Text tool writes: a text box on the slide, then a
    label typed across an occlusion box."""
    s = _slide_rect(art)
    _slide(p, s, k)
    _content(p, s, k, labels=False)
    _bar(p, _u(s, *_LABELS[2]), k.bar)
    fade = 1 - _ph(t, .90, .99)
    mask = _u(s, *_BOXES[2])

    # 1) click an empty spot: a box opens and the words type in, the
    #    format bar riding above it while it is selected
    spot = _up(s, .58, .24)
    full = _u(s, .55, .20, .38, .17)
    grow = _out(_ph(t, .14, .22))
    typing = _ph(t, .22, .40)
    selected = grow * (1 - _ph(t, .44, .48))
    if grow > 0:
        r = QRectF(full.x(), full.y(), _lerp(8, full.width(), grow), full.height())
        _rrect(p, r, 2, _a(QColor(255, 255, 255), fade * grow),
               _a(k.hint, fade * selected), 1)
        at = _typed(p, full.x() + 6, full.y() + full.height() / 2 - 2.5,
                    .30 * s.width(), typing, _a(k.ink, fade))
        if selected > 0:
            bar = QRectF(full.center().x() - 22, full.y() - 13, 44, 9)
            _rrect(p, bar, 4.5, _a(k.btn, selected), _a(k.btn_line, selected), 1)
            for n, col in enumerate((k.ink, QColor(217, 48, 37), QColor(26, 115, 232), k.accent)):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_a(col, selected))
                p.drawEllipse(QPointF(bar.x() + 9 + n * 9, bar.center().y()), 2.2, 2.2)
            _caret(p, at, k, fade * selected, t)

    # 2) click an occlusion box: its label types across the mask, in white
    _box(p, mask, k, fade)
    on_box = _ph(t, .58, .62)
    label = _ph(t, .62, .80)
    if on_box > 0:
        _rrect(p, mask, 2, None, _a(k.accent, fade * on_box * (1 - _ph(t, .84, .88))), 1.5)
        w = .22 * s.width()
        at = _typed(p, mask.center().x() - w / 2, mask.center().y() - 2.5, w, label,
                    _a(QColor(255, 255, 255), fade * .95))
        _caret(p, at, k, fade * (1 - _ph(t, .82, .86)), t)

    # the pointer: in from below, click the slide, then click the box
    home = _up(s, .40, 1.02)
    box_pt = mask.center() + QPointF(-mask.width() * .25, 0)
    if t < .30:
        cur = _lerp_pt(home, spot, _io(_ph(t, .02, .13)))
        cur = cur + QPointF(12, 14) * _io(_ph(t, .20, .30))
    else:
        cur = _lerp_pt(spot + QPointF(12, 14), box_pt, _io(_ph(t, .44, .57)))
        cur = cur + QPointF(10, 12) * _io(_ph(t, .62, .72))
    down = max(_ph(t, .13, .15) * (1 - _ph(t, .16, .18)),
               _ph(t, .57, .59) * (1 - _ph(t, .60, .62)))
    _cursor(p, cur, fade * (1 - _ph(t, .76, .84)), down)


def _paint_detect(p, art, t, k):
    s = _slide_rect(art)
    _slide(p, s, k)
    _bar(p, _u(s, *_TITLE), k.title)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(k.blob)
    p.drawEllipse(_up(s, .25, .60), .25 * s.height() * 1.05, .25 * s.height())
    rows = [(.56, .27, .34), (.56, .42, .26), (.56, .57, .31), (.56, .72, .20)]
    for x, y, w in rows:
        _bar(p, _u(s, x, y, w, .07), k.bar)
    fade = 1 - _ph(t, .86, .98)
    a, b = _up(s, .50, .17), _up(s, .95, .86)
    drag = _io(_ph(t, .12, .44))
    home = _up(s, .42, 1.02)
    region = QRectF(a, _lerp_pt(a, b, drag)).normalized()
    if .10 <= t < .62:
        gone = _ph(t, .46, .60)
        # shade outside the region, then a dashed edge
        path = QPainterPath()
        path.addRect(s)
        inner = QPainterPath()
        inner.addRect(region)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, int(26 * _clamp(drag * 3) * (1 - gone))))
        p.drawPath(path.subtracted(inner))
        pen = QPen(_a(k.band_line, 1 - gone), 1.2, Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([3, 2.5])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(region)
    for i, (x, y, w) in enumerate(rows):
        sc, al = _pop(_ph(t, .46 + i * .07, .60 + i * .07))
        if al > 0:
            r = _u(s, x - .025, y - .02, w + .05, .11)
            _box(p, _scaled(r, sc), k, al * fade)
    if t < .12:
        cur = _lerp_pt(home, a, _io(_ph(t, .0, .11)))
    else:
        cur = _lerp_pt(a, b, drag) + QPointF(8, 6) * _io(_ph(t, .50, .64))
    down = _ph(t, .10, .13) * (1 - _ph(t, .44, .47))
    _cursor(p, cur, fade * (1 - _ph(t, .64, .72)), down)


def _paint_cloze(p, art, t, k):
    s = _slide_rect(art)
    _slide(p, s, k)
    _bar(p, _u(s, *_TITLE), k.title)
    words = [[.14, .22, .12, .18], [.20, .12, .26], [.16, .24]]
    target = (0, 1)
    m = _io(_ph(t, .22, .44)) * (1 - _io(_ph(t, .82, .96)))
    h = .085
    for row, ws in enumerate(words):
        x = .08
        y = .34 + row * .19
        for col, w in enumerate(ws):
            r = _u(s, x, y, w, h)
            if (row, col) == target:
                _bar(p, _scaled(r, 1 - .25 * m), _a(k.bar, 1 - m))
                if m > 0:
                    cy = r.center().y()
                    hh = r.height() * 1.9 / 2
                    spread = 2.5 * (1 - m)
                    pen = QPen(_a(k.accent, m), 1.4, Qt.PenStyle.SolidLine,
                               Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                    p.setPen(pen)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    lx, rx = r.left() + spread, r.right() - spread
                    p.drawPolyline(QPolygonF([QPointF(lx + 3, cy - hh), QPointF(lx, cy - hh),
                                              QPointF(lx, cy + hh), QPointF(lx + 3, cy + hh)]))
                    p.drawPolyline(QPolygonF([QPointF(rx - 3, cy - hh), QPointF(rx, cy - hh),
                                              QPointF(rx, cy + hh), QPointF(rx - 3, cy + hh)]))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(_a(k.accent, m))
                    for d in (-1, 0, 1):
                        dm = _ph(t, .36 + (d + 1) * .04, .44 + (d + 1) * .04)
                        p.setBrush(_a(k.accent, min(m, dm)))
                        p.drawEllipse(QPointF(r.center().x() + d * 4.2, cy), 1.25, 1.25)
            else:
                _bar(p, r, k.bar)
            x += w + .035


def _paint_auto(p, art, t, k):
    """Generate: the lecture on the left becomes, in turn, occlusion boxes,
    cloze cards and a practice quiz on the right."""
    src = QRectF(art.x() + 10, art.y() + 26, 62, 44)
    _slide(p, src, k)
    _content(p, src, k)
    # the three results, a third of the loop each
    out = QRectF(art.right() - 82, art.y() + 18, 72, 60)
    phase = min(2, int(t * 3))
    local = t * 3 - phase
    enter = _out(_ph(local, .0, .22))
    leave = _ph(local, .80, .98)
    al = _clamp(enter * (1 - leave))
    dx = 10 * (1 - enter) - 10 * leave
    # a small arrow from the lecture to the result, pulsing as each one arrives
    a0 = QPointF(src.right() + 6, src.center().y())
    a1 = QPointF(out.left() - 6 + 0 * dx, src.center().y())
    pulse = 0.5 + 0.5 * (1 - _ph(local, .0, .35))
    p.setPen(QPen(_a(k.accent, .55 + .35 * pulse), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(a0, a1)
    p.drawLine(a1, a1 + QPointF(-4, -3)); p.drawLine(a1, a1 + QPointF(-4, 3))
    p.save()
    p.translate(dx, 0)
    if phase == 0:          # image occlusion: the slide itself, boxed
        s = QRectF(out.x(), out.y() + 6, out.width(), out.height() - 12)
        _slide(p, s, k, al)
        _content(p, s, k, al)
        for n, b in enumerate(_BOXES):
            sc, a2 = _pop(_ph(local, .18 + n * .12, .34 + n * .12))
            if a2 > 0:
                _box(p, _scaled(_u(s, *b), sc), k, al * a2)
    elif phase == 1:        # cloze cards: a small stack, a sentence with one blank
        for n in (2, 1, 0):
            c = QRectF(out.x() + n * 4, out.y() + 10 - n * 4, out.width() - 10, out.height() - 22)
            _rrect(p, c, 3, _a(k.slide, al), _a(QColor(0, 0, 0, 40), al))
        c = QRectF(out.x(), out.y() + 10, out.width() - 10, out.height() - 22)
        _bar(p, QRectF(c.x() + 6, c.y() + 8, c.width() - 12, 3.5), _a(k.bar, al))
        _bar(p, QRectF(c.x() + 6, c.y() + 16, 10, 3.5), _a(k.bar, al))
        blank = _ph(local, .25, .45)
        _rrect(p, QRectF(c.x() + 19, c.y() + 14.5, 18, 6.5), 1.5, _a(k.accent, al * (.35 + .65 * blank)))
        _bar(p, QRectF(c.x() + 40, c.y() + 16, c.width() - 46, 3.5), _a(k.bar, al))
        _bar(p, QRectF(c.x() + 6, c.y() + 24, c.width() - 24, 3.5), _a(k.bar, al))
    else:                   # practice quiz: a question, five choices, one picked
        c = QRectF(out.x(), out.y() + 2, out.width() - 6, out.height() - 4)
        _rrect(p, c, 3, _a(k.slide, al), _a(QColor(0, 0, 0, 40), al))
        _bar(p, QRectF(c.x() + 6, c.y() + 6, c.width() - 12, 3), _a(k.title, al))
        _bar(p, QRectF(c.x() + 6, c.y() + 12, c.width() - 22, 3), _a(k.title, al))
        pick = _ph(local, .40, .55)
        for n in range(5):
            y = c.y() + 21 + n * 6.4
            on = n == 2 and pick > 0
            p.setPen(QPen(_a(k.accent if on else QColor(0, 0, 0, 90), al), 1))
            p.setBrush(_a(k.accent, al * pick) if on else Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(c.x() + 9, y + 1.5), 2.2, 2.2)
            _bar(p, QRectF(c.x() + 15, y, c.width() - 26 - (n % 2) * 8, 3), _a(k.bar, al))
    # the result's name under it: Boxes, Cards, Quiz
    f = QFont(p.font())
    f.setPixelSize(9)
    f.setBold(True)
    p.setFont(f)
    p.setPen(_a(k.sub, al))
    p.drawText(QRectF(out.x() - 4, art.bottom() - 15, out.width(), 12),
               int(Qt.AlignmentFlag.AlignCenter), ("Boxes", "Cards", "Quiz")[phase])
    p.restore()


def _inv_io(y: float) -> float:
    """Inverse of _io, by bisection (tiny, called a few times a frame)."""
    lo, hi = 0.0, 1.0
    for _ in range(18):
        mid = (lo + hi) / 2
        if _io(mid) < y:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _paint_create(p, art, t, k):
    s = QRectF(art.x() + 12, art.y() + 24, 86, 52)
    _slide(p, s, k)
    _content(p, s, k)
    fade = 1 - _ph(t, .86, .98)
    boxes = [_u(s, *b) for b in _BOXES]
    for r in boxes:
        _box(p, r, k)
    centre = QPointF(art.right() - 33, art.center().y() + 3)
    card_w, card_h = 38, 27
    angles = [-13, -2, 10]
    offs = [QPointF(-7, 2), QPointF(0, -1), QPointF(7, 2)]
    for i, r in enumerate(boxes):
        m = _io(_ph(t, .14 + i * .13, .46 + i * .13))
        if m <= 0:
            continue
        c = _lerp_pt(r.center(), centre + offs[i], m)
        w = _lerp(r.width(), card_w, m)
        h = _lerp(r.height(), card_h, m)
        p.save()
        p.translate(c)
        p.rotate(angles[i] * m)
        body = QRectF(-w / 2, -h / 2, w, h)
        # a card lifting off the box, with that box on it
        al = fade * _clamp(m * 3)
        _rrect(p, body, 3, _a(k.slide, al), _a(QColor(0, 0, 0, 40), al))
        _bar(p, QRectF(-w / 2 + 5, -h / 2 + 5, w * .45, 3), _a(k.title, al * m))
        _box(p, QRectF(-w / 2 + 5, -h / 2 + 12, w * .55, h * .3), k, al)
        p.restore()


def _paint_zoom(p, art, t, k):
    s = _slide_rect(art)
    z = _io(_ph(t, .10, .42)) - _io(_ph(t, .58, .90))
    sc = 1 + .2 * z
    anchor = s.center()
    p.save()
    clip = QPainterPath()
    clip.addRoundedRect(art, 7, 7)
    p.setClipPath(clip)
    p.translate(anchor)
    p.scale(sc, sc)
    p.translate(-anchor)
    _slide(p, s, k)
    _content(p, s, k)
    _box(p, _u(s, *_BOXES[1]), k)
    p.restore()


def _paint_open(p, art, t, k):
    p.save()
    clip = QPainterPath()
    clip.addRoundedRect(art, 7, 7)
    p.setClipPath(clip)
    fade = 1 - _ph(t, .86, .98)
    final = _slide_rect(art)
    dw, dh = 40, 52
    landed = QRectF(art.center().x() - dw / 2, art.center().y() - dh / 2, dw, dh)
    drop = _back(_ph(t, .04, .36), 1.1)
    y = _lerp(art.top() - dh - 4, landed.y(), drop)
    doc = QRectF(landed.x(), y, dw, dh)
    m = _io(_ph(t, .46, .68))
    r = _lerp_rect(doc, final, m)
    fold = 9 * (1 - m)
    path = QPainterPath()
    path.moveTo(r.left() + 3, r.top())
    path.lineTo(r.right() - fold - (3 if fold < 1 else 0), r.top())
    path.lineTo(r.right(), r.top() + fold + (3 if fold < 1 else 0))
    path.lineTo(r.right(), r.bottom() - 3)
    path.quadTo(r.right(), r.bottom(), r.right() - 3, r.bottom())
    path.lineTo(r.left() + 3, r.bottom())
    path.quadTo(r.left(), r.bottom(), r.left(), r.bottom() - 3)
    path.lineTo(r.left(), r.top() + 3)
    path.quadTo(r.left(), r.top(), r.left() + 3, r.top())
    p.setPen(QPen(_a(QColor(0, 0, 0, 40), fade), 1))
    p.setBrush(_a(k.slide, fade))
    p.drawPath(path)
    if fold > .5:
        p.setBrush(_a(QColor(0, 0, 0, 22), fade))
        p.drawPolygon(QPolygonF([QPointF(r.right() - fold, r.top()),
                                 QPointF(r.right() - fold, r.top() + fold),
                                 QPointF(r.right(), r.top() + fold)]))
    # a page of lines, turning into the slide
    for i, w in enumerate((.55, .70, .62, .70, .40)):
        _bar(p, QRectF(r.x() + 6, r.y() + 12 + i * 7, (r.width() - 12) * w, 3),
             _a(k.bar, fade * (1 - m)))
    if m > 0:
        _content(p, r, k, fade * m)
    p.restore()


def _paint_lecture(p, art, t, k):
    fade = 1 - _ph(t, .86, .98)
    a = _out(_ph(t, .03, .20))
    pop = QRectF(art.x() + 22, art.y() + 13 - 6 * (1 - a), art.width() - 44,
                 art.height() - 26)
    al = a * fade
    _rrect(p, pop.adjusted(.5, .5, -.5, -.5), 7, _a(k.card, al), _a(k.line, al))
    fx = pop.x() + 34
    fw = pop.right() - 10 - fx
    # row 1: the lecture's name types into its field
    y1 = pop.y() + 15
    _bar(p, QRectF(pop.x() + 10, y1 + 5, 16, 4), _a(k.sub, al * .55))
    field = QRectF(fx, y1, fw, 14)
    _rrect(p, field, 3, _a(k.btn, al), _a(k.btn_line, al))
    typed = _ph(t, .22, .46)
    typed = math.floor(typed * 6) / 6 if typed < 1 else 1.0
    tc = QColor(k.text)
    tc.setAlphaF(.75)
    _bar(p, QRectF(fx + 5, y1 + 5, (fw - 22) * typed, 4), _a(tc, al))
    # row 2: the deck, a stack that builds up
    y2 = pop.y() + 40
    _bar(p, QRectF(pop.x() + 10, y2 + 8, 16, 4), _a(k.sub, al * .55))
    for i in range(3):
        sc, pa = _pop(_ph(t, .48 + i * .08, .60 + i * .08))
        if pa <= 0:
            continue
        r = QRectF(fx + 4 + i * 4, y2 + 4 - i * 3, 24, 15)
        _rrect(p, _scaled(r, sc), 3, _a(k.slide, al * pa),
               _a(QColor(0, 0, 0, 50) if not k.night else QColor(0, 0, 0, 90),
                  al * pa))
    _bar(p, QRectF(fx + 42, y2 + 8, 30, 4),
         _a(tc, al * _ph(t, .70, .78)))


def _paint_slides(p, art, t, k):
    r = _io(_ph(t, .08, .32)) * (1 - _io(_ph(t, .58, .82)))
    rail_w = 32
    p.save()
    clip = QPainterPath()
    clip.addRoundedRect(art, 7, 7)
    p.setClipPath(clip)
    x0 = art.x() - rail_w + rail_w * r
    rail = QRectF(x0, art.y(), rail_w, art.height())
    if r > 0:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(k.card)
        p.drawRect(rail)
        p.setPen(QPen(k.line, 1))
        p.drawLine(QPointF(rail.right() - .5, rail.top()),
                   QPointF(rail.right() - .5, rail.bottom()))
        for i in range(4):
            th = QRectF(rail.x() + 6, art.y() + 8 + i * 22, rail_w - 12, 13)
            if i == 1:
                _rrect(p, th.adjusted(-3, -3, 3, 3), 3, k.rail_on)
            _slide(p, th, k)
            _bar(p, QRectF(th.x() + 2, th.y() + 2.5, th.width() * .45, 1.6), k.title)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(k.blob)
            p.drawEllipse(QPointF(th.x() + th.width() * .3, th.y() + th.height() * .62), 3, 2.6)
    room = art.adjusted(rail_w * r, 0, 0, 0)
    full = _slide_rect(art)
    aspect = full.height() / full.width()
    w = min(full.width(), room.width() - 24)
    h = w * aspect
    s = QRectF(room.center().x() - w / 2, room.center().y() - h / 2, w, h)
    _slide(p, s, k)
    _content(p, s, k)
    p.restore()


def _carousel(direction: int):
    def paint(p, art, t, k):
        s0 = _slide_rect(art)
        gap = s0.width() + 22
        pos = _io(_ph(t, .14, .42)) + _io(_ph(t, .58, .86))
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(art, 7, 7)
        p.setClipPath(clip)
        for i, draw in enumerate((_content, _text_slide, _content)):
            dx = (i - pos) * gap * direction
            if abs(dx) > art.width():
                continue
            s = s0.translated(dx, 0)
            _slide(p, s, k)
            draw(p, s, k)
        p.restore()
    return paint


def _mode(hide_all: bool):
    """The tested label moves down the slide; the others are covered too
    (hide all) or left showing (hide one)."""
    def paint(p, art, t, k):
        s = _slide_rect(art)
        _slide(p, s, k)
        _content(p, s, k)
        step = min(2, int(t * 3))
        local = t * 3 - step
        for i, b in enumerate(_BOXES):
            r = _u(s, *b)
            if i == step:
                sc, a = _pop(_ph(local, 0.0, .25))
                _box(p, _scaled(r, sc), k, a, sel=1.0)
            elif hide_all:
                _box(p, r, k, 1.0)
        # a small card edge flips in each step, so it reads as one card each
        flip = _ph(local, .0, .18) * (1 - _ph(local, .82, 1.0))
        if flip > 0:
            _rrect(p, s.adjusted(-1, -1, 1, 1), 4, None, _a(k.accent, .35 * flip), 1.2)
    return paint


PAINTERS = {
    "mode_ao": _mode(True),
    "mode_oa": _mode(False),
    "open": _paint_open,
    "lecture": _paint_lecture,
    "slides": _paint_slides,
    "prev": _carousel(-1),
    "next": _carousel(1),
    "draw": _paint_draw,
    "select": _paint_select,
    "text": _paint_text,
    "detect": _paint_detect,
    "cloze": _paint_cloze,
    "zoom": _paint_zoom,
    "auto": _paint_auto,
    "create": _paint_create,
}

# name, shortcut (portable text, shown in the platform's own notation)
LABELS = {
    "mode_ao": ("Hide All", ""),
    "mode_oa": ("Hide One", ""),
    "open": ("Open PDF", "Ctrl+O"),
    "lecture": ("Lecture", ""),
    "slides": ("Slides", ""),
    "prev": ("Previous", "Left"),
    "next": ("Next", "Right"),
    "draw": ("Draw", "D"),
    "select": ("Select", "V"),
    "text": ("Text", "T"),
    "detect": ("Detect", "F"),
    "cloze": ("Cloze Cards", "Ctrl+Shift+V"),
    "zoom": ("Zoom", "Ctrl+= Ctrl+-"),
    "auto": ("Generate", ""),
    "create": ("Create Cards", ""),
}

# Keys whose widget tooltip says something live (the card counts on Create),
# shown as a second, smaller line.
_LIVE_TIP = {"create"}

_PERIOD = {"auto": 4.5, "mode_ao": 3.6, "mode_oa": 3.6, "text": 3.8, "create": 3.0, "zoom": 2.8, "slides": 3.0,
           "prev": 3.0, "next": 3.0}


def _shortcut_text(keys: str) -> str:
    out = []
    for part in keys.split(" "):
        if not part:
            continue
        native = QKeySequence(part).toString(QKeySequence.SequenceFormat.NativeText)
        out.append({"Left": "←", "Right": "→"}.get(native, native))
    return "  ".join(out)


# ------------------------------------------------------------------ card --

class _Card(QWidget):
    M = 10            # room round the card for its shadow
    PAD = 10
    ART_W, ART_H = 164, 100
    DELAY_MS = 450
    FADE_S = 0.15

    def __init__(self, parent: QWidget):
        super().__init__(parent, Qt.WindowType.ToolTip
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        for attr in (Qt.WidgetAttribute.WA_ShowWithoutActivating,
                     Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                     Qt.WidgetAttribute.WA_TranslucentBackground):
            self.setAttribute(attr)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self.update)
        self._key = ""
        self._name = self._keys = self._extra = ""
        self._colors: Callable[[], dict] = lambda: {}
        self._t0 = self._shown = 0.0
        self.target: Optional[QWidget] = None

    def _fonts(self):
        base = QApplication.font()
        name = QFont(base)
        name.setPixelSize(12)
        name.setWeight(QFont.Weight.DemiBold)
        keys = QFont(base)
        keys.setPixelSize(12)
        extra = QFont(base)
        extra.setPixelSize(11)
        return name, keys, extra

    def present(self, widget: QWidget, key: str, colors_fn, extra: str = ""):
        self.target = widget
        self._key = key
        self._colors = colors_fn
        self._name, keys = LABELS.get(key, (key.title(), ""))
        self._keys = _shortcut_text(keys) if keys else ""
        self._extra = extra
        h = self.PAD + self.ART_H + 8 + 15 + (14 if extra else 0) + self.PAD - 2
        w = self.PAD * 2 + self.ART_W
        self.setFixedSize(w + 2 * self.M, h + 2 * self.M)
        self._place(widget)
        self._t0 = self._shown = time.monotonic()
        self.show()
        self.raise_()
        self._timer.start()
        self.update()

    def _place(self, widget: QWidget):
        W, H, M = self.width(), self.height(), self.M
        below = widget.mapToGlobal(QPoint(widget.width() // 2, widget.height()))
        x = below.x() - W // 2
        y = below.y() + 6 - M
        screen = widget.screen()
        if screen is not None:
            area = screen.availableGeometry()
            if y + H - M > area.bottom():
                y = widget.mapToGlobal(QPoint(0, 0)).y() - 6 - H + M
            x = max(area.left() + 4 - M, min(x, area.right() - 4 - W + M))
        self.move(x, y)

    def dismiss(self):
        self._timer.stop()
        self.hide()
        self.target = None

    def hideEvent(self, ev):
        self._timer.stop()
        super().hideEvent(ev)

    def paintEvent(self, ev):
        try:
            c = self._colors() or {}
            k = _K(c)
        except Exception:
            return
        now = time.monotonic()
        appear = _out((now - self._shown) / self.FADE_S)
        period = _PERIOD.get(self._key, 2.6)
        t = ((now - self._t0) / period) % 1.0

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(appear)
        p.translate(0, -5 * (1 - appear))
        M = self.M
        card = QRectF(self.rect()).adjusted(M, M, -M, -M)
        # a soft shadow, a few faint layers wide
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(M, 0, -2):
            p.setBrush(QColor(0, 0, 0, max(1, int(k.shadow * .22 * (1 - i / M) ** 2))))
            p.drawRoundedRect(card.adjusted(-i, -i + 3, i, i + 3), 10 + i, 10 + i)
        _rrect(p, card.adjusted(.5, .5, -.5, -.5), 10, k.card, k.line)

        art = QRectF(card.x() + self.PAD, card.y() + self.PAD, self.ART_W, self.ART_H)
        _rrect(p, art, 7, k.stage)
        paint = PAINTERS.get(self._key)
        if paint is not None:
            p.save()
            try:
                paint(p, art, t, k)
            except Exception:
                pass
            p.restore()

        name_f, keys_f, extra_f = self._fonts()
        fn, fk = QFontMetrics(name_f), QFontMetrics(keys_f)
        gap = 8 if self._keys else 0
        total = fn.horizontalAdvance(self._name) + gap + fk.horizontalAdvance(self._keys)
        x = card.center().x() - total / 2
        base = art.bottom() + 8 + fn.ascent()
        p.setFont(name_f)
        p.setPen(k.text)
        p.drawText(QPointF(x, base), self._name)
        if self._keys:
            p.setFont(keys_f)
            p.setPen(k.sub)
            p.drawText(QPointF(x + fn.horizontalAdvance(self._name) + gap, base),
                       self._keys)
        if self._extra:
            fe = QFontMetrics(extra_f)
            p.setFont(extra_f)
            p.setPen(k.sub)
            txt = fe.elidedText(self._extra, Qt.TextElideMode.ElideRight,
                                int(card.width() - 2 * self.PAD))
            p.drawText(QPointF(card.center().x() - fe.horizontalAdvance(txt) / 2,
                               base + fn.descent() + 3 + fe.ascent()), txt)
        p.end()


# ------------------------------------------------------------- installer --

class _Hover(QObject):
    """One per app: watches every explained control, keeps one card up."""

    WARM_S = 0.4      # moving straight to a neighbour shows it at once

    def __init__(self):
        super().__init__(QApplication.instance())
        self._keys: dict = {}          # id(widget) -> (key, colors_fn)
        self._card: Optional[_Card] = None
        self._pending: Optional[QWidget] = None
        self._last_hide = 0.0
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.timeout.connect(self._show_pending)

    def add(self, widget: QWidget, key: str, colors_fn):
        self._keys[id(widget)] = (key, colors_fn)
        widget.installEventFilter(self)
        widget.destroyed.connect(lambda *_, i=id(widget): self._keys.pop(i, None))

    def card(self) -> Optional[_Card]:
        return self._card

    def show_now(self, widget: QWidget):
        """Show the card for widget at once (also used by tests)."""
        self._delay.stop()
        self._pending = widget
        self._show_pending()

    def eventFilter(self, obj, ev):
        et = ev.type()
        if et == QEvent.Type.Enter:
            self._pending = obj
            warm = time.monotonic() - self._last_hide < self.WARM_S
            self._delay.start(40 if warm else _Card.DELAY_MS)
        elif et in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress,
                    QEvent.Type.MouseButtonDblClick, QEvent.Type.Hide,
                    QEvent.Type.Wheel, QEvent.Type.KeyPress):
            if self._pending is obj:
                self._delay.stop()
                self._pending = None
            if self._card is not None and self._card.target is obj:
                self.hide(warm=et == QEvent.Type.Leave)
        elif et == QEvent.Type.ToolTip:
            return True        # the card replaces the plain tooltip
        return False

    def hide(self, warm: bool = False):
        card = self._card
        if card is not None and _alive(card) and card.isVisible():
            card.dismiss()
            self._last_hide = time.monotonic() if warm else 0.0

    def _show_pending(self):
        w = self._pending
        self._pending = None
        if w is None or not _alive(w) or not w.isVisible():
            return
        entry = self._keys.get(id(w))
        if entry is None:
            return
        key, colors_fn = entry
        win = w.window()
        if self._card is None or not _alive(self._card) or self._card.parent() is not win:
            if self._card is not None and _alive(self._card):
                self._card.dismiss()
                self._card.deleteLater()
            self._card = _Card(win)
        extra = (w.toolTip() or "") if key in _LIVE_TIP else ""
        self._card.present(w, key, colors_fn, extra)
        _note_seen(key)


_SEEN: set = set()


def _note_seen(key: str) -> None:
    """Which controls people hover long enough to see explained: once per key
    per Anki session, so it shows what needs explaining without flooding."""
    if key in _SEEN:
        return
    _SEEN.add(key)
    try:
        from . import telemetry
        telemetry.track("hover_card", key=key)
    except Exception:   # noqa: BLE001 - never let counting break the window
        pass


def _alive(obj) -> bool:
    try:
        from PyQt6 import sip
    except ImportError:          # pragma: no cover
        import sip  # type: ignore
    try:
        return not sip.isdeleted(obj)
    except Exception:
        return False


_HOVER: Optional[_Hover] = None


def hover() -> _Hover:
    global _HOVER
    if _HOVER is None or not _alive(_HOVER):
        _HOVER = _Hover()
    return _HOVER


def install(widget: QWidget, key: str, colors_fn: Callable[[], dict]):
    """Explain widget with the animation for key when it is hovered."""
    if key in PAINTERS:
        hover().add(widget, key, colors_fn)


def install_all(owner, pairs, colors_fn: Callable[[], dict]):
    """install() for each (attribute name, key) on owner; missing ones skip."""
    for attr, key in pairs:
        w = getattr(owner, attr, None)
        if isinstance(w, QWidget):
            install(w, key, colors_fn)
