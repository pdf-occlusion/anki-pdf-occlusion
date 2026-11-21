"""Whole-slide review: a slide at a time instead of a card at a time.

Every box on the slide starts covered. Click a box (or press Space for the next
one) to reveal it, then mark it right or wrong; each box is still its own Anki
card and is scheduled exactly as if it had been answered in the reviewer
(Again for wrong, Good for right). A slide's due boxes are outlined; the rest
stay covered so they cannot give answers away, and can be peeked at without
being graded.

The box layout comes from the cards themselves: every card's front mask holds
all the boxes on its slide, with its own region drawn in the highlight stroke.
"""
import os
import re
import time
from typing import Optional

from aqt import mw
from aqt.qt import (QColor, QDialog, QHBoxLayout, QLabel, QPainter, QPen, QPixmap,
                    QPushButton, QRectF, QShortcut, QKeySequence, Qt, QTimer,
                    QVBoxLayout, QWidget)

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
    """Cards grouped by slide: [{"image": fname, "W", "H", "regions": [{"cid", "rects"}], "others": [rects]}]."""
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
    out = []
    for s in by_slide.values():
        due = [r for reg in s["regions"] for r in reg["rects"]]
        # boxes on the slide that are not due today: everything in the masks that is not a due region
        s["others"] = [r for r in s["all"] if not any(abs(r[0] - d[0]) < 6 and abs(r[1] - d[1]) < 6 for d in due)]
        out.append(s)
    return out


class _SlideView(QWidget):
    """The slide with its boxes; reports clicks on a region."""

    def __init__(self, parent, on_click):
        super().__init__(parent)
        self.on_click = on_click
        self.pixmap: Optional[QPixmap] = None
        self.slide = None
        self.revealed: set = set()      # cids revealed
        self.graded: dict = {}          # cid -> ease
        self.peeked: set = set()        # indexes into slide["others"]
        self.current: Optional[int] = None
        self.setMinimumSize(640, 360)
        self.setMouseTracking(True)

    def _frame(self) -> QRectF:
        if not self.pixmap:
            return QRectF()
        pw, ph = self.pixmap.width() / self.pixmap.devicePixelRatio(), self.pixmap.height() / self.pixmap.devicePixelRatio()
        s = min(self.width() / pw, self.height() / ph)
        return QRectF((self.width() - pw * s) / 2, (self.height() - ph * s) / 2, pw * s, ph * s)

    def _to_screen(self, r) -> QRectF:
        f = self._frame()
        sx, sy = f.width() / self.slide["W"], f.height() / self.slide["H"]
        return QRectF(f.x() + r[0] * sx, f.y() + r[1] * sy, r[2] * sx, r[3] * sy)

    def paintEvent(self, _e):
        if not self.pixmap or not self.slide:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = self._frame()
        p.drawPixmap(f.toRect(), self.pixmap)
        mask = QColor(124, 124, 128)
        accent = QColor(110, 90, 168)
        for k, r in enumerate(self.slide["others"]):
            if k in self.peeked:
                continue
            p.fillRect(self._to_screen(r), QColor(124, 124, 128, 200))
        for i, reg in enumerate(self.slide["regions"]):
            for r in reg["rects"]:
                q = self._to_screen(r)
                if reg["cid"] in self.revealed:
                    ease = self.graded.get(reg["cid"])
                    if ease:
                        p.setPen(QPen(QColor(52, 168, 83) if ease > 1 else QColor(217, 48, 37), 2.5))
                        p.setBrush(Qt.BrushStyle.NoBrush)
                        p.drawRoundedRect(q.adjusted(-2, -2, 2, 2), 3, 3)
                    continue
                p.fillRect(q, mask)
                p.setPen(QPen(accent, 3 if i == self.current else 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(q)
        p.end()

    def mousePressEvent(self, e):
        if not self.slide:
            return
        pos = e.position() if hasattr(e, "position") else e.pos()
        for i, reg in enumerate(self.slide["regions"]):
            if any(self._to_screen(r).contains(pos) for r in reg["rects"]):
                self.on_click(i)
                return
        for k, r in enumerate(self.slide["others"]):
            if self._to_screen(r).contains(pos):
                self.peeked ^= {k}
                self.update()
                return


class SlideReview(QDialog):
    """Review due PDF Occlusion cards a slide at a time."""

    def __init__(self, parent, card_ids):
        super().__init__(parent)
        self.setWindowTitle("PDF Occlusion")
        self.col = mw.col
        self.slides = slides_for(self.col, card_ids)
        self.index = -1
        self.cards: dict = {}
        self.view = _SlideView(self, self._reveal)
        self.count = QLabel()
        self.count.setStyleSheet("color: palette(mid);")
        self.wrong = QPushButton("✗")
        self.right = QPushButton("✓")
        for b in (self.wrong, self.right):
            b.setFixedSize(56, 34)
            b.setEnabled(False)
        self.wrong.clicked.connect(lambda: self._grade(1))
        self.right.clicked.connect(lambda: self._grade(3))
        bar = QHBoxLayout()
        bar.addWidget(self.count)
        bar.addStretch(1)
        bar.addWidget(self.wrong)
        bar.addWidget(self.right)
        lay = QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addLayout(bar)
        QShortcut(QKeySequence("Space"), self, activated=self._space)
        QShortcut(QKeySequence("1"), self, activated=lambda: self._grade(1))
        QShortcut(QKeySequence("3"), self, activated=lambda: self._grade(3))
        self.resize(1200, 760)
        self._next_slide()

    # --- flow -----------------------------------------------------------
    def _next_slide(self):
        self.index += 1
        if self.index >= len(self.slides):
            self.accept()
            return
        s = self.slides[self.index]
        path = os.path.join(self.col.media.dir(), s["image"])
        self.view.pixmap = QPixmap(path)
        if not s["W"]:
            s["W"], s["H"] = self.view.pixmap.width(), self.view.pixmap.height()
        self.view.slide = s
        self.view.revealed, self.view.graded, self.view.peeked = set(), {}, set()
        self.view.current = 0 if s["regions"] else None
        self.cards = {}
        for reg in s["regions"]:
            c = self.col.get_card(reg["cid"])
            c.start_timer()
            self.cards[reg["cid"]] = c
        self.count.setText(f"{self.index + 1} / {len(self.slides)}")
        self._sync()

    def _reveal(self, i: int):
        reg = self.view.slide["regions"][i]
        self.view.current = i
        self.view.revealed.add(reg["cid"])
        self._sync()

    def _space(self):
        """Reveal the current box; a second press marks it right."""
        if self.view.current is None:
            return
        cid = self.view.slide["regions"][self.view.current]["cid"]
        if cid in self.view.revealed:
            self._grade(3)
        else:
            self._reveal(self.view.current)

    def _grade(self, ease: int):
        i = self.view.current
        if i is None:
            return
        reg = self.view.slide["regions"][i]
        cid = reg["cid"]
        if cid not in self.view.revealed or cid in self.view.graded:
            return
        answer_card(self.col, self.cards[cid], ease)
        self.view.graded[cid] = ease
        left = [k for k, r in enumerate(self.view.slide["regions"]) if r["cid"] not in self.view.graded]
        self.view.current = left[0] if left else None
        self._sync()
        if not left:
            QTimer.singleShot(450, self._next_slide)

    def _sync(self):
        i = self.view.current
        open_ = i is not None and self.view.slide["regions"][i]["cid"] in self.view.revealed
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
