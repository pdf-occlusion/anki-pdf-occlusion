import os
import webbrowser
import time
import traceback
import uuid
from typing import Optional

from aqt import mw, gui_hooks
from aqt.theme import theme_manager
from aqt.qt import (
    QDialog, QWidget, QFrame, QVBoxLayout, QHBoxLayout, QFormLayout,
    QStackedWidget, QPushButton, QToolButton, QLabel, QLineEdit, QComboBox,
    QFileDialog, QScrollArea, QListWidget, QListWidgetItem,
    QStyledItemDelegate, QStyle, QAbstractItemView, QShortcut, QKeySequence,
    Qt, QRect, QRectF, QPoint, QPointF, QSize, QImage, QPixmap, QIcon, QColor, QPen,
    QFont, QFontMetrics, QPainter, QByteArray, QObject, QEvent,
    QProgressDialog, QMenu, QDesktopServices, QUrl, QTimer, QSlider,
    QWidgetAction,
)
from aqt.utils import askUser, showInfo, showWarning, tooltip

from . import auto_occlude
from . import hover_explain
from . import note_recovery
from . import pdf_hosting
from . import session_store
from .cloze_dialog import ClozeColumn, ClozeComposer, fly_to_chip
from .occlusion_canvas import (OcclusionCanvas, annotation_rect,
                               draw_annotation)
from .card_builder import (ensure_note_type, create_occlusion_notes,
                           create_cloze_notes, cloze_note_type,
                           cloze_card_count, pdf_path_in, SLIDES_PDF_FIELD,
                           NoteTypeConflict)
from . import ocr
from . import telemetry
from .pdf_renderer import (render_pdf, LazyPages, get_text_word_rects, get_page_text,
                           get_table_cell_rects, merge_word_rects, _render_page)


_ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]

# Room left round a fitted slide, so its shadow shows (logical px, each side).
_FIT_MARGIN = 24


# ------------------------------------------------------------------ look --
#
# Neutral greys everywhere; purple only where it means something: the one
# primary action (Create N cards) and, on the slide, the selected box. Every
# colour has a light and a night value, picked from Anki's own theme.

def _night() -> bool:
    try:
        return bool(theme_manager.night_mode)
    except Exception:
        return False


_LIGHT = {
    "window": "#fbfbfd", "line": "#e3e3e8", "stage": "#ececf0",
    "text": "#1d1d1f", "sub": "#86868b", "icon": "#3a3a3c", "icon_off": "#b8b8bd",
    "seg": "#e8e8ed", "seg_on": "#ffffff", "seg_on_line": "#d6d6dc",
    "hover": "rgba(0,0,0,0.06)",
    "btn": "#ffffff", "btn_line": "#d2d2d7", "btn_hover": "#f4f4f7",
    "btn_down": "#e8e8ed",
    "accent": "#6e5aa8", "accent_hover": "#634f9c", "accent_down": "#574590",
    "accent_tint": "#efebf8", "accent_text": "#5b4796", "accent_tint_hover": "#e5dff4", "accent_tint_down": "#d9d1ee",
    "off_bg": "#e8e8ed", "off_fg": "#a1a1a6",
    "rail_on": "#dcd6ee", "pill": QColor(0, 0, 0, 115),
    "placeholder": "#ffffff", "shadow": 26,
}
_DARK = {
    "window": "#2a2a2c", "line": "#3a3a3d", "stage": "#1e1e20",
    "text": "#f2f2f7", "sub": "#98989d", "icon": "#e5e5ea", "icon_off": "#636366",
    "seg": "#3a3a3d", "seg_on": "#636366", "seg_on_line": "#6e6e73",
    "hover": "rgba(255,255,255,0.08)",
    "btn": "#3a3a3d", "btn_line": "#4a4a4e", "btn_hover": "#434347",
    "btn_down": "#4d4d52",
    "accent": "#7461b0", "accent_hover": "#7f6cbb", "accent_down": "#6956a3",
    "accent_tint": "#2f2a40", "accent_text": "#c9bcf2", "accent_tint_hover": "#38314d", "accent_tint_down": "#41395a",
    "off_bg": "#3a3a3d", "off_fg": "#6e6e73",
    "rail_on": "#433b5e", "pill": QColor(0, 0, 0, 140),
    "placeholder": "#d8d8dc", "shadow": 90,
}


def _colors() -> dict:
    return _DARK if _night() else _LIGHT


def _primary_qss(c: Optional[dict] = None) -> str:
    """The one solid purple button. Disabled is a plain grey, never a pale
    purple, so it can't be mistaken for the live button."""
    c = c or _colors()
    return (
        f"QPushButton{{background:{c['accent']};color:white;font-weight:600;"
        f"padding:0 14px;min-height:30px;border-radius:7px;"
        f"border:1px solid {c['accent']};}}"
        f"QPushButton:hover{{background:{c['accent_hover']};"
        f"border-color:{c['accent_hover']};}}"
        f"QPushButton:pressed{{background:{c['accent_down']};"
        f"border-color:{c['accent_down']};}}"
        f"QPushButton:disabled{{background:{c['off_bg']};color:{c['off_fg']};"
        f"border-color:{c['off_bg']};}}"
    )


# Kept for anything that still imports it; the window itself uses
# _primary_qss(), which follows the theme.
_PRIMARY_BTN_QSS = _primary_qss(_LIGHT)


def _window_qss(c: dict) -> str:
    """Everything in the window, by object name. The bare QPushButton rule
    reaches child windows too (Auto, the cloze panel), so it stays quiet."""
    return f"""
QPushButton {{ padding:5px 14px; border-radius:6px;
    border:1px solid rgba(127,127,127,0.35); background:transparent; }}
QPushButton:hover {{ background:{c['hover']}; }}
QPushButton:pressed {{ background:rgba(127,127,127,0.20); }}
QPushButton:disabled {{ color:{c['off_fg']}; border-color:rgba(127,127,127,0.18); }}

QDialog#pdfOcclusion {{ background:{c['window']}; }}
QFrame#topBar {{ background:{c['window']}; border:none;
    border-bottom:1px solid {c['line']}; }}

QPushButton#titleBtn {{ border:1px solid {c['btn_line']}; background:{c['btn']}; color:{c['text']};
    font-size:14px; font-weight:600; padding:5px 10px; border-radius:7px;
    text-align:left; }}
QPushButton#titleBtn:hover {{ background:{c['btn_hover']}; }}
QPushButton#titleBtn:pressed {{ background:{c['btn_down']}; }}
QLabel#subLabel {{ color:{c['sub']}; font-size:14px; }}

QToolButton#barIcon {{ border:none; background:transparent; border-radius:6px;
    padding:4px; }}
QToolButton#barIcon:hover, QToolButton#barIcon:pressed {{ background:{c['hover']}; }}
QToolButton#barIcon::menu-indicator {{ image:none; width:0; }}

QFrame#seg {{ background:{c['seg']}; border:none; border-radius:8px; }}
QToolButton#segBtn {{ border:1px solid transparent; background:transparent;
    border-radius:6px; }}
QToolButton#segBtn:hover {{ background:{c['hover']}; }}
QToolButton#segBtn:checked {{ background:{c['seg_on']};
    border:1px solid {c['seg_on_line']}; }}

QToolButton#zoomBtn {{ border:none; background:transparent; color:{c['sub']};
    padding:4px 4px; border-radius:6px; }}
QToolButton#zoomBtn:hover {{ background:{c['hover']}; }}
QToolButton#zoomBtn:disabled {{ color:{c['off_fg']}; }}
QToolButton#zoomBtn::menu-indicator {{ image:none; width:0; }}

QPushButton#generate {{ background:{c['accent_tint']}; color:{c['accent_text']};
    font-weight:600; padding:0 14px; min-height:30px; border-radius:7px;
    border:1.5px solid {c['accent']}; }}
QPushButton#generate:hover {{ background:{c['accent_tint_hover']}; }}
QPushButton#generate:pressed {{ background:{c['accent_tint_down']}; }}
QPushButton#generate:disabled {{ background:{c['off_bg']}; color:{c['off_fg']};
    border-color:{c['off_bg']}; }}
QPushButton#secondary {{ background:{c['btn']}; color:{c['text']};
    font-weight:500; padding:0 13px; min-height:30px; border-radius:7px;
    border:1px solid {c['btn_line']}; }}
QPushButton#secondary:hover {{ background:{c['btn_hover']}; }}
QPushButton#secondary:pressed {{ background:{c['btn_down']}; }}
QPushButton#secondary:disabled {{ background:{c['off_bg']}; color:{c['off_fg']};
    border-color:{c['off_bg']}; }}

QSlider::groove:horizontal {{ height:4px; border-radius:2px; background:{c['line']}; }}
QSlider::sub-page:horizontal {{ height:4px; border-radius:2px; background:{c['accent']}; }}
QSlider::handle:horizontal {{ width:16px; height:16px; margin:-6px 0; border-radius:8px;
    background:#ffffff; border:1px solid {c['btn_line']}; }}

QListWidget#rail {{ background:{c['window']}; border:none;
    border-right:1px solid {c['line']}; outline:0; }}
QWidget#stage {{ background:{c['stage']}; }}
QScrollArea#stageScroll {{ background:{c['stage']}; border:none; }}

QLabel#formLabel {{ color:{c['sub']}; }}
QPushButton#notesBtn {{ text-align:left; padding:5px 26px 5px 10px; border-radius:6px;
    border:1px solid {c['btn_line']}; background:{c['btn']}; color:{c['text']}; min-height:20px; }}
QFrame#popover QLineEdit {{ padding:5px 10px; border-radius:6px; min-height:20px;
    border:1px solid {c['btn_line']}; background:{c['btn']}; color:{c['text']}; }}
QFrame#popover QComboBox {{ padding:5px 26px 5px 10px; border-radius:6px; min-height:20px;
    border:1px solid {c['btn_line']}; background:{c['btn']}; color:{c['text']}; }}
QFrame#popover QComboBox::drop-down {{ border:none; width:24px; }}
QFrame#popover QComboBox::down-arrow {{ image:url("{_asset('caret_muted.svg')}"); width:8px; height:5px; }}
QFrame#popover QComboBox QLineEdit {{ border:none; padding:0; background:transparent; min-height:0; }}
QToolButton#pagerBtn {{ border:none; background:transparent; border-radius:6px; padding:0; }}
QToolButton#pagerBtn:hover, QToolButton#pagerBtn:pressed {{ background:{c['hover']}; }}
QPushButton#notesBtn:hover {{ background:{c['btn_hover']}; }}
QPushButton#notesBtn::menu-indicator {{ image:url("{_asset('caret_muted.svg')}");
    subcontrol-origin:padding; subcontrol-position:right center; right:9px;
    width:8px; height:5px; }}

ClozeColumn QScrollArea, ClozeColumn QScrollArea > QWidget > QWidget {{
    background:transparent; }}
"""


# Line icons, 24-unit grid, stroked in the theme's icon colour.
_ICONS = {
    "draw": '<rect x="4" y="6" width="16" height="12" rx="1.5"/>',
    "select": '<path d="M6 4l11 7-5 1.2L9.5 18z"/>',
    "text": '<path d="M6 6h12M12 6v13"/>',
    "detect": '<path d="M4 9V5h4M16 5h4v4M20 15v4h-4M8 19H4v-4"/>'
              '<path d="M8 12h8"/>',
    "cloze": '<path d="M9 5c-2 0-3 1-3 3v2c0 1-1 2-2 2 1 0 2 1 2 2v2c0 2 1 3 3 3'
             'M15 5c2 0 3 1 3 3v2c0 1 1 2 2 2-1 0-2 1-2 2v2c0 2-1 3-3 3"/>',
    "auto": '<rect x="3.5" y="5" width="7" height="4" rx="1"/>'
            '<rect x="13.5" y="5" width="7" height="4" rx="1"/>'
            '<rect x="3.5" y="11" width="7" height="4" rx="1"/>'
            '<rect x="13.5" y="11" width="7" height="4" rx="1"/>'
            '<path d="M5 18.5h14"/>',
    "open": '<path d="M3.5 7a1.5 1.5 0 0 1 1.5-1.5h4.2l2 2.2H19a1.5 1.5 0 0 1 '
            '1.5 1.5V17a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 17z"/>',
    "caret": '<path d="M8 10l4 4 4-4"/>',
    "rail": '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M9 5v14"/>',
    "prev": '<path d="M14.5 6.5L9 12l5.5 5.5"/>',
    "next": '<path d="M9.5 6.5L15 12l-5.5 5.5"/>',
    "cards": '<rect x="7.5" y="4.5" width="12" height="9" rx="1.6"/>'
             '<path d="M4.5 9v7.9c0 .9.7 1.6 1.6 1.6H15"/>',
}


def _mode_icon(mode: str, c: dict) -> QIcon:
    """Three labels on a slide. Hide all: every one covered, the tested one
    outlined. Hide one: only the tested one covered."""
    dpr = max(2.0, (mw.app.devicePixelRatio() if mw else 1.0) or 1.0)
    icon = QIcon()
    for state, ink in ((QIcon.Mode.Normal, c["icon"]), (QIcon.Mode.Disabled, c["icon_off"])):
        pm = QPixmap(int(22 * dpr), int(22 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = QColor(ink)
        accent = QColor(c["accent"]) if state == QIcon.Mode.Normal else col
        for i, y in enumerate((3.5, 9, 14.5)):
            tested = i == 1
            if mode == "ao" or tested:
                # a covered label: a solid mask, the tested one ringed in accent
                fill = QColor(col)
                fill.setAlpha(170)
                p.setPen(QPen(accent, 1.6) if tested else Qt.PenStyle.NoPen)
                p.setBrush(fill)
                p.drawRoundedRect(QRectF(3, y, 16, 5), 1.5, 1.5)
            else:
                # a label left showing: a line of text
                p.setPen(QPen(col, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(4, y + 2.5), QPointF(16, y + 2.5))
        p.end()
        icon.addPixmap(pm, state)
    return icon


class _CountButton(QPushButton):
    """The primary button as an icon, with the number of cards it will make
    in a small badge at its bottom-right."""

    def __init__(self):
        super().__init__()
        self.count = 0

    def set_count(self, n: int):
        if n != self.count:
            self.count = n
            self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self.count or not self.isEnabled():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(self.font())
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        text = str(self.count) if self.count < 1000 else "999+"
        w = max(15, QFontMetrics(f).horizontalAdvance(text) + 8)
        r = QRectF(self.width() - w - 1, self.height() - 15, w, 14)
        c = _colors()
        p.setPen(QPen(QColor(c["accent"]), 1.2))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(r, 7, 7)
        p.setPen(QColor(c["accent"]))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)
        p.end()


def _svg_pixmap(name: str, color: str, size: int, dpr: float) -> QPixmap:
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
           f'fill="none" stroke="{color}" stroke-width="1.6" '
           'stroke-linecap="round" stroke-linejoin="round">'
           f'{_ICONS[name]}</svg>').encode()
    px = max(1, int(round(size * dpr)))
    try:
        from PyQt6.QtSvg import QSvgRenderer
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        QSvgRenderer(QByteArray(svg)).render(p)
        p.end()
    except Exception:       # no QtSvg module: the image plugin can still read it
        pm = QPixmap()
        pm.loadFromData(svg, "SVG")
        pm = pm.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    return pm


def _icon(name: str, c: dict, size: int = 20, color: Optional[str] = None) -> QIcon:
    dpr = max(2.0, (mw.app.devicePixelRatio() if mw else 1.0) or 1.0)
    ic = QIcon()
    ic.addPixmap(_svg_pixmap(name, color or c["icon"], size, dpr), QIcon.Mode.Normal)
    ic.addPixmap(_svg_pixmap(name, c["icon_off"], size, dpr), QIcon.Mode.Disabled)
    return ic


def _asset(name: str) -> str:
    return os.path.join(os.path.dirname(__file__), name).replace("\\", "/")


# ------------------------------------------------------------ slide rail --

class _ThumbDelegate(QStyledItemDelegate):
    def __init__(self, rail: "_SlideRail"):
        super().__init__(rail)
        self._rail = rail

    def sizeHint(self, option, index):
        return QSize(self._rail.WIDTH, self._rail.row_height(index.row()))

    def paint(self, painter, option, index):
        self._rail.paint_row(painter, option.rect, index.row(),
                             bool(option.state & QStyle.StateFlag.State_Selected))


class _SlideRail(QListWidget):
    """Slide thumbnails down the left edge.

    Thumbnails are rendered small, straight from pdfium, a few milliseconds'
    worth per idle tick and only for the rows on screen and just past them,
    then kept. Opening a PDF never waits on them, and the full-size pages
    (LazyPages) are never touched for them.
    """

    WIDTH = 150
    PAD = 8           # rail edge to the highlight
    INSET = 4         # highlight to the thumbnail
    GAP = 3           # above and below each highlight
    _BUDGET = 0.012   # seconds of rendering per tick
    _KEEP = 160       # thumbnails held at most

    def __init__(self, render, info, parent=None):
        """render(row, width_px) -> QImage or None.
        info(row) -> (label, boxes, marked, page_width_px)."""
        super().__init__(parent)
        self.setObjectName("rail")
        self.setFixedWidth(self.WIDTH)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setItemDelegate(_ThumbDelegate(self))
        self._render, self._info = render, info
        self._thumbs: dict = {}
        self._sizes: list = []        # page sizes in points, per row
        self._aspect = 0.75
        self._mask = QColor(120, 120, 120)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._tick)
        self.verticalScrollBar().valueChanged.connect(self.kick)

    def set_mask_color(self, rgb: tuple):
        self._mask = QColor(*[int(v) for v in rgb[:3]])

    def reset_pages(self, sizes: list):
        self._thumbs.clear()
        self._sizes = list(sizes)
        w, h = sizes[0] if sizes else (4, 3)
        self._aspect = (h / w) if w else 0.75
        self.blockSignals(True)
        self.clear()
        for _ in range(len(sizes)):
            self.addItem(QListWidgetItem())
        self.blockSignals(False)
        self.kick()

    def thumb_width(self) -> int:
        return self.WIDTH - 2 * (self.PAD + self.INSET)

    def row_height(self, row: int) -> int:
        w, h = self._sizes[row] if 0 <= row < len(self._sizes) else (1.0, self._aspect)
        aspect = min(1.6, (h / w) if w else self._aspect)
        return int(round(self.thumb_width() * aspect)) + 2 * (self.INSET + self.GAP)

    def set_current(self, row: int):
        if not 0 <= row < self.count():
            return
        self.blockSignals(True)
        self.setCurrentRow(row)
        self.blockSignals(False)
        self.scrollToItem(self.item(row),
                          QAbstractItemView.ScrollHint.EnsureVisible)
        self.kick()

    def refresh_row(self, row: int):
        item = self.item(row) if 0 <= row < self.count() else None
        if item is not None:
            self.viewport().update(self.visualItemRect(item))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.kick()

    # -- rendering, a little at a time ---------------------------------------

    def kick(self, *_):
        if self.count() and not self._timer.isActive():
            self._timer.start()

    def _rows_wanted(self) -> list:
        first = self.indexAt(QPoint(4, 2)).row()
        last = self.indexAt(QPoint(4, self.viewport().height() - 2)).row()
        n = self.count()
        first = 0 if first < 0 else first
        last = n - 1 if last < 0 else last
        rows = list(range(first, last + 1))
        for d in range(1, 4):                       # then just past the edges
            rows += [r for r in (last + d, first - d) if 0 <= r < n]
        return rows

    def _tick(self):
        dpr = self.devicePixelRatioF() or 1.0
        width = int(round(self.thumb_width() * dpr))
        t0 = time.perf_counter()
        rows = self._rows_wanted()
        for row in rows:
            if row in self._thumbs:
                continue
            try:
                img = self._render(row, width)
            except Exception:     # noqa: BLE001 - a page pdfium can't draw
                img = None
            pm = None
            if img is not None and not img.isNull():
                pm = QPixmap.fromImage(img)
                pm.setDevicePixelRatio(dpr)
            self._thumbs[row] = pm
            self.refresh_row(row)
            if time.perf_counter() - t0 > self._BUDGET:
                self._timer.start()
                return
        if len(self._thumbs) > self._KEEP:
            keep = set(rows)
            mid = rows[0] if rows else 0
            for r in sorted(self._thumbs, key=lambda r: -abs(r - mid)):
                if len(self._thumbs) <= self._KEEP:
                    break
                if r not in keep:
                    del self._thumbs[r]

    # -- painting ------------------------------------------------------------

    def paint_row(self, p: QPainter, rect: QRect, row: int, selected: bool):
        c = _colors()
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hi = QRectF(rect).adjusted(self.PAD, self.GAP, -self.PAD, -self.GAP)
        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["rail_on"]))
            p.drawRoundedRect(hi, 6, 6)
        area = hi.adjusted(self.INSET, self.INSET, -self.INSET, -self.INSET)
        w_pt, h_pt = (self._sizes[row] if row < len(self._sizes)
                      else (1.0, self._aspect))
        aspect = (h_pt / w_pt) if w_pt else self._aspect
        tw, th = area.width(), area.width() * aspect
        if th > area.height():
            th, tw = area.height(), area.height() / aspect
        tr = QRectF(area.center().x() - tw / 2, area.center().y() - th / 2, tw, th)

        # a hairline shadow, then the slide
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 34))
        p.drawRoundedRect(tr.adjusted(-0.5, 0, 0.5, 1.2), 3, 3)
        pm = self._thumbs.get(row)
        if pm is not None:
            p.drawPixmap(tr, pm, QRectF(pm.rect()))
        else:
            p.setBrush(QColor(c["placeholder"]))
            p.drawRect(tr)

        label, boxes, marked, page_w = self._info(row)
        if boxes and page_w:
            k = tr.width() / page_w
            p.setBrush(self._mask)
            for b in boxes:
                r = QRectF(tr.left() + b["x"] * k, tr.top() + b["y"] * k,
                           max(1.0, b["w"] * k), max(1.0, b["h"] * k))
                angle = b.get("angle") or 0
                if angle:
                    p.save()
                    p.translate(r.center())
                    p.rotate(angle)
                    p.translate(-r.center())
                if b.get("shape") == "ellipse":
                    p.drawEllipse(r)
                else:
                    p.drawRect(r)
                if angle:
                    p.restore()

        f = QFont(p.font())
        f.setPixelSize(10)
        f.setWeight(QFont.Weight.Medium)
        p.setFont(f)
        fm = QFontMetrics(f)
        pill = QRectF(tr.left() + 4, tr.bottom() - 4 - 14,
                      fm.horizontalAdvance(label) + 8, 14)
        p.setBrush(c["pill"])
        p.drawRoundedRect(pill, 4, 4)
        p.setPen(QColor(255, 255, 255))
        p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), label)

        if marked:
            centre = QPoint(int(tr.right() - 8), int(tr.bottom() - 8))
            p.setPen(QPen(QColor(c["rail_on"] if selected else c["window"]), 2))
            p.setBrush(QColor(c["accent"]))
            p.drawEllipse(centre, 4, 4)
        p.restore()


class _StageBackdrop(QObject):
    """Paints the area round the slide: a flat neutral ground and a soft
    shadow under the slide. It only repaints when the slide moves or the
    window changes size; the canvas paints itself opaquely on top."""

    def __init__(self, viewport: QWidget, canvas: QWidget):
        super().__init__(viewport)
        self._vp, self._canvas = viewport, canvas
        viewport.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is not self._vp or ev.type() != QEvent.Type.Paint:
            return False
        c = _colors()
        p = QPainter(self._vp)
        p.fillRect(self._vp.rect(), QColor(c["stage"]))
        if self._canvas.has_image():
            g = QRectF(self._canvas.geometry())
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(Qt.BrushStyle.NoBrush)
            steps, peak = 18, c["shadow"]
            soft = g.translated(0, 5)
            for i in range(1, steps + 1):
                a = peak * (1 - i / (steps + 1)) ** 2
                p.setPen(QPen(QColor(0, 0, 0, int(a)), 1))
                p.drawRoundedRect(soft.adjusted(-i + .5, -i + .5, i - .5, i - .5),
                                  2 + i, 2 + i)
            p.setPen(QPen(QColor(0, 0, 0, 22 if peak < 60 else 60), 1))
            p.drawRoundedRect(g.adjusted(-.5, -.5, .5, .5), 2, 2)
        p.end()
        return True


class _Popover(QFrame):
    """A small window under the lecture title for the things set once per
    lecture: its name, deck, notes PDF, and the current slide's mode."""

    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.Popup
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("popover")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.form = QFormLayout(self)
        self.form.setContentsMargins(16, 14, 16, 16)
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(10)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
        # macOS keeps form fields at their small default width; fill the row instead
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.setFixedWidth(440)

    def add_row(self, label: str, widget: QWidget):
        lab = QLabel(label)
        lab.setObjectName("formLabel")
        self.form.addRow(lab, widget)

    def paintEvent(self, ev):
        # rounded corners need the window itself to be see-through
        c = _colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(c["line"]), 1))
        p.setBrush(QColor(c["window"]))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 10, 10)
        p.end()

    def show_under(self, anchor: QWidget):
        for edit in self.findChildren(QLineEdit):
            edit.setCursorPosition(0)       # show a long name from its start
        self.adjustSize()
        self.move(anchor.mapToGlobal(QPoint(0, anchor.height() + 6)))
        self.show()


def _centre_inside(rect: tuple, region: tuple) -> bool:
    """Is a detected rect's centre within the scan region?

    Centres rather than full containment: a table cell whose border sits a
    pixel outside the rectangle you drew round it still belongs to it.
    """
    x, y, w, h = rect
    rx, ry, rw, rh = region
    cx, cy = x + w / 2.0, y + h / 2.0
    return rx <= cx <= rx + rw and ry <= cy <= ry + rh


def _cfg(key, default):
    cfg = mw.addonManager.getConfig(__name__) or {}
    return cfg.get(key, default)


def _set_cfg(key, value):
    cfg = mw.addonManager.getConfig(__name__) or {}
    if cfg.get(key) != value:
        cfg[key] = value
        mw.addonManager.writeConfig(__name__, cfg)


def _log_error(report: str) -> str:
    """Save a traceback to user_files/last_error.txt; returns where it went."""
    path = os.path.join(os.path.dirname(__file__), "user_files",
                        "last_error.txt")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
    except OSError:
        pass
    return path


def _push_published(hosting: dict, urls: list) -> None:
    """Commit and push the PDFs just published, off the main thread.

    Runs once the cards exist and doesn't hold the dialog open: a failed push
    costs nothing but the phone links working until a later one goes through.
    """
    def on_done(fut):
        try:
            pushed = fut.result()
        except pdf_hosting.PublishError as e:
            showWarning(
                "Your cards were made, but their PDFs couldn't be pushed to "
                f"the web, so the links won't open on your phone yet.\n\n{e}\n\n"
                f"The files are in {hosting['folder']}. Creating cards again "
                "tries the push again, or run git push there yourself.",
                title="PDF Occlusion",
            )
            return
        if pushed:
            tooltip("Slides / Notes PDFs pushed — your phone can open them "
                    "once the site updates (usually a minute or two).",
                    period=6000)

    mw.taskman.run_in_background(lambda: pdf_hosting.push(hosting, urls),
                                 on_done)


def _pages_with_notes(note_map) -> set:
    """Local page indexes that cards in this note map were made from."""
    pages = set()
    for key in (note_map or {}):
        try:
            pages.add(int(str(key).split(":", 1)[0]))
        except ValueError:
            pass
    return pages


class PDFOcclusionDialog(QDialog):
    def __init__(self, parent=None, editor=None, note_id=None):
        super().__init__(parent)
        self._editor = editor
        self.setWindowTitle("PDF Occlusion")
        self.setObjectName("pdfOcclusion")
        self.resize(1280, 860)

        # Documents: one entry per opened PDF. Pages are flattened into
        # self._pages; each doc records its slice via start/count.
        # {"path", "lecture", "start", "count", "note_map", "image_map"}
        self._docs: list[dict] = []
        self._pages: list[QImage] = []
        self._render_scale: float = 1.0
        self._page_index: int = 0
        self._boxes: dict[int, list[dict]] = {}
        self._page_modes: dict[int, str] = {}   # global page idx -> "ao"/"oa"
        # Text written onto a slide: part of the picture, not a card. It goes
        # onto the image the cards are cut from, so a box can occlude it like
        # anything the slide already had printed on it.
        self._annots: dict[int, list] = {}
        self._page_cache: dict[int, QImage] = {}   # slides with text drawn in
        # Cloze: the composer is a panel to the right of the slide, the
        # cards written from it are chips to the left, per slide. Like the
        # boxes, they are only records until Create All Cards runs —
        # _cloze_stale holds the notes of records deleted since.
        self._cloze_cards: dict[int, list] = {}
        self._cloze_stale: list[int] = []
        self._save_warned = False   # a failed session save is said once
        self._fly = None            # in-flight card animation, kept alive
        self._fitted = False        # is the current zoom a fit? (see _refit)

        self._build_ui()

        self._canvas.set_mask_color(tuple(_cfg("mask_color", [120, 120, 120])))
        self._rail.set_mask_color(tuple(_cfg("mask_color", [120, 120, 120])))
        gui_hooks.theme_did_change.append(self._on_theme_changed)
        self._canvas.set_editing_opacity(int(_cfg("editing_mask_opacity", 150)))
        self._apply_default_zoom()

        telemetry.watch_buttons(self, "main")

        # Hovering a top-bar control shows a small animated card of what it
        # does, in place of a plain tooltip (see hover_explain). Controls
        # that don't exist are skipped.
        hover_explain.install_all(self, (
            ("_open_btn", "open"), ("_title_btn", "lecture"),
            ("_rail_btn", "slides"), ("_prev_btn", "prev"),
            ("_next_btn", "next"), ("_draw_btn", "draw"),
            ("_select_btn", "select"), ("_text_btn", "text"),
            ("_detect_btn", "detect"), ("_cloze_btn", "cloze"),
            ("_zoom_label", "zoom"), ("_auto_btn", "auto"),
            ("_mode_ao_btn", "mode_ao"), ("_mode_oa_btn", "mode_oa"),
            ("_create_btn", "create"),
        ), _colors)

        # Opened from a PDF Occlusion card in Browse? Jump straight to the
        # slide and box that made it.
        if note_id:
            self._open_note(note_id)

    def _open_note(self, nid: int):
        found = session_store.find_note(nid)
        if not found:
            # No session for this card — it was cleared, made on another
            # machine, or predates the store. The cards themselves still
            # know enough to put one back.
            self._recover_note(nid)
            return
        path, region_key = found
        if not os.path.exists(path):
            showWarning(f"The PDF for this card can no longer be found:\n{path}")
            return
        self._open_paths([path], silent_resume=True)
        self._focus_region(region_key)

    def _recover_note(self, nid: int):
        """Rebuild this card's session from the cards made off the same PDF."""
        progress = QProgressDialog("Finding This Card's Slides", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)

        def on_progress(done: int, total: int):
            progress.setMaximum(total)
            progress.setValue(done)
            mw.app.processEvents()

        try:
            plan = note_recovery.recover(
                mw.col, nid, on_progress,
                hosting=pdf_hosting.settings(_cfg("pdf_hosting", None)))
        finally:
            progress.close()

        if plan is None:
            # Naming the file is the difference between "start over" and
            # "put that file back" — the boxes are still in the card.
            path = self._slides_pdf_of(nid)
            if path:
                showWarning(
                    "This card's boxes can be read back, but its PDF is no "
                    f"longer here:\n{path}\n\n"
                    "Put the file back at that path (or restore its name) "
                    "and open the card again."
                )
            else:
                showWarning(
                    "This card can't be opened for editing.\n\n"
                    "Its slides are not in a saved session and the card "
                    "doesn't record which PDF it came from.\n\n"
                    "Open the PDF with Open PDF and draw the boxes again."
                )
            return

        self._open_paths([plan["pdf_path"]], silent_resume=True)
        if not self._docs:
            return
        restored = self._apply_recovery(plan)
        self._show_page()
        if plan["focus"]:
            self._focus_region(plan["focus"])
        if restored:
            tooltip(f"Rebuilt {restored} slide(s) from your cards — "
                    "editing them updates those cards in place.", period=5000)

    def _slides_pdf_of(self, nid: int) -> str:
        try:
            note = mw.col.get_note(nid)
        except Exception:
            return ""
        if SLIDES_PDF_FIELD not in note:
            return ""
        return pdf_path_in(note[SLIDES_PDF_FIELD])

    def _apply_recovery(self, plan: dict) -> int:
        """Fill in slides the session doesn't already cover.

        Never overwrites boxes that are already loaded: a session that
        exists but has simply lost track of one card stays the truth.
        """
        doc = self._docs[0]
        if doc["path"] != plan["pdf_path"]:
            return 0
        if plan["lecture"]:
            doc["lecture"] = plan["lecture"]
        if plan["notes_pdf"] and not doc.get("notes_pdf"):
            doc["notes_pdf"] = plan["notes_pdf"]

        restored = 0
        for local, page in plan["pages"].items():
            if not 0 <= local < doc["count"]:
                continue
            idx = doc["start"] + local
            if self._boxes.get(idx):
                continue
            img = self._pages[idx]
            # The boxes are in the pixel space of whatever render scale the
            # cards were made at; these pages are rendered at the current one.
            fx = img.width() / page["w"] if page["w"] else 1.0
            fy = img.height() / page["h"] if page["h"] else 1.0
            for b in page["boxes"]:
                b["x"] = int(round(b["x"] * fx))
                b["y"] = int(round(b["y"] * fy))
                b["w"] = max(1, int(round(b["w"] * fx)))
                b["h"] = max(1, int(round(b["h"] * fy)))
            self._boxes[idx] = page["boxes"]
            if page["mode"] in ("ao", "oa"):
                self._page_modes[idx] = page["mode"]
            # The saved slide PNG is only the right one at the same scale.
            if page["image"] and fx == 1.0 and fy == 1.0:
                doc["image_map"][str(local)] = page["image"]
            doc["note_map"].update(page["note_map"])
            restored += 1

        if restored:
            self._persist_sessions()
        return restored

    def _focus_region(self, key: str):
        if not self._docs:
            return
        try:
            page_s, kind, ident = key.split(":", 2)
            local = int(page_s)
        except ValueError:
            return
        doc = self._docs[0]
        if not 0 <= local < doc["count"]:
            return
        self._save_current_boxes()
        self._page_index = doc["start"] + local
        self._show_page()
        if kind == "c":
            for card in self._cloze_cards.get(self._page_index, []):
                if card.get("uid") == ident:
                    self._on_cloze_activated(card)
                    return
        elif kind == "b":
            self._canvas.select_region(box_id=ident)
        else:
            self._canvas.select_region(group_uid=ident)

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar: open + lecture, tools in the middle, zoom + actions ──
        bar = QFrame()
        bar.setObjectName("topBar")
        bar.setFixedHeight(52)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 0, 16, 0)
        row.setSpacing(0)

        left = QHBoxLayout()
        left.setSpacing(4)
        self._open_btn = QToolButton()
        self._open_btn.setObjectName("barIcon")
        self._open_btn.setToolTip("Open PDF (Ctrl+O)")
        self._open_btn.setIconSize(QSize(20, 20))
        self._open_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._recent_menu = QMenu(self)
        self._recent_menu.aboutToShow.connect(self._fill_recent_menu)
        self._open_btn.setMenu(self._recent_menu)

        # The lecture's name is the title; clicking it opens the popover
        # with the name field, deck, notes PDF and this slide's mode.
        self._title_btn = QPushButton()
        self._title_btn.setObjectName("titleBtn")
        self._title_btn.setIconSize(QSize(16, 16))
        self._title_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._title_btn.setToolTip("Lecture, deck and notes")
        self._title_btn.clicked.connect(self._show_popover)
        self._sub_label = QLabel()
        self._sub_label.setObjectName("subLabel")
        def bar_btn(name, label, slot, checkable=False):
            b = QToolButton()
            b.setObjectName("barIcon")
            b.setAccessibleName(label)
            b.setIconSize(QSize(20, 20))
            b.setProperty("_icon", name)
            b.setCheckable(checkable)
            b.clicked.connect(slot)
            return b

        self._rail_btn = bar_btn("rail", "Slides", self._toggle_rail, checkable=True)
        self._rail_btn.setChecked(bool(_cfg("show_slides", True)))
        self._prev_btn = bar_btn("prev", "Previous Slide", self._prev_page)
        self._next_btn = bar_btn("next", "Next Slide", self._next_page)
        for b in (self._prev_btn, self._next_btn):
            b.setObjectName("pagerBtn")
            b.setFixedSize(26, 26)
            b.setIconSize(QSize(16, 16))
        self._sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # wide enough for "100 of 100" in its 14 px font, so the box never clips
        f = QFont(self._sub_label.font())
        f.setPixelSize(14)
        self._sub_label.setFixedWidth(QFontMetrics(f).horizontalAdvance("100 of 100") + 14)

        left.addWidget(self._open_btn)
        left.addWidget(self._rail_btn)
        left.addSpacing(4)
        left.addWidget(self._title_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        left.addStretch(1)

        seg = QFrame()
        seg.setObjectName("seg")
        seg_row = QHBoxLayout(seg)
        seg_row.setContentsMargins(3, 3, 3, 3)
        seg_row.setSpacing(2)

        def seg_btn(name, tip, slot):
            b = QToolButton()
            b.setObjectName("segBtn")
            b.setCheckable(True)
            b.setFixedSize(36, 28)
            b.setIconSize(QSize(20, 20))
            b.setToolTip(tip)
            b.setProperty("_icon", name)
            b.clicked.connect(slot)
            seg_row.addWidget(b)
            return b

        self._draw_btn = seg_btn("draw", "Draw (D)", lambda: self._set_tool("draw"))
        self._select_btn = seg_btn("select", "Select (V)",
                                   lambda: self._set_tool("select"))
        self._text_btn = seg_btn("text", "Text (T)", lambda: self._set_tool("text"))
        self._detect_btn = seg_btn("detect", "Detect (F)", self._arm_detect)
        self._cloze_btn = seg_btn("cloze", "Cloze (Ctrl+Shift+V)", self._open_cloze)

        right = QHBoxLayout()
        right.setSpacing(8)
        right.addStretch(1)
        self._zoom_label = QToolButton()
        self._zoom_label.setObjectName("zoomBtn")
        self._zoom_label.setText("100%")
        self._zoom_label.setMinimumWidth(48)
        self._zoom_label.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        zoom_menu = QMenu(self)
        # a slider for any zoom, then Fit; Ctrl+= and Ctrl+- still step
        holder = QWidget()
        hrow = QHBoxLayout(holder)
        hrow.setContentsMargins(14, 8, 14, 6)
        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setRange(25, 400)
        self._zoom_slider.setFixedWidth(200)
        self._zoom_slider.valueChanged.connect(self._on_zoom_slider)
        hrow.addWidget(self._zoom_slider)
        slider_act = QWidgetAction(zoom_menu)
        slider_act.setDefaultWidget(holder)
        zoom_menu.addAction(slider_act)
        zoom_menu.addSeparator()
        self._fit_act = zoom_menu.addAction("Fit", self._zoom_fit)
        self._fit_act.setShortcut(QKeySequence("Ctrl+0"))
        # the window-level QShortcut does the work; this only shows it
        self._fit_act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        zoom_menu.aboutToShow.connect(self._sync_zoom_slider)
        self._zoom_label.setMenu(zoom_menu)

        self._auto_btn = QPushButton("Generate")
        self._auto_btn.setObjectName("generate")
        self._auto_btn.setIconSize(QSize(18, 18))
        self._auto_btn.clicked.connect(self._open_auto_occlude)

        self._create_btn = _CountButton()
        self._create_btn.setAccessibleName("Create Cards")
        self._create_btn.setFixedSize(48, 34)
        self._create_btn.setIconSize(QSize(20, 20))
        self._create_btn.setObjectName("primary")
        self._create_btn.setDefault(True)
        self._create_btn.clicked.connect(self._create_cards)

        right.addWidget(self._zoom_label)
        right.addSpacing(2)
        right.addWidget(self._auto_btn)
        right.addWidget(self._create_btn)

        # equal stretch either side keeps the tools in the true centre
        seg.setFixedHeight(34)
        row.addLayout(left, 1)
        row.addSpacing(12)
        row.addWidget(seg, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(12)
        row.addLayout(right, 1)
        root.addWidget(bar)

        # ── Lecture popover ───────────────────────────────────────────────
        self._popover = _Popover(self)

        self._lecture_edit = QLineEdit()
        self._lecture_edit.setPlaceholderText("Lecture")
        self._lecture_edit.textEdited.connect(self._on_lecture_edited)

        self._deck_combo = QComboBox()
        self._deck_combo.setEditable(True)
        self._populate_decks()

        # a pop-up button like the combos round it: Choose, Open, Remove
        # a plain button that opens its menu itself: Qt drops the text padding of
        # buttons that own a menu; the caret is a small label at the right edge
        self._notes_pdf_btn = QPushButton()
        self._notes_pdf_btn.setObjectName("notesBtn")
        self._notes_pdf_menu = QMenu(self)
        self._notes_pdf_menu.aboutToShow.connect(self._fill_notes_pdf_menu)
        self._notes_pdf_btn.clicked.connect(lambda: self._notes_pdf_menu.exec(
            self._notes_pdf_btn.mapToGlobal(QPoint(0, self._notes_pdf_btn.height() + 2))))
        caret_row = QHBoxLayout(self._notes_pdf_btn)
        caret_row.setContentsMargins(0, 0, 10, 0)
        caret_row.addStretch(1)
        caret = QLabel()
        caret.setPixmap(QIcon(_asset("caret_muted.svg")).pixmap(QSize(8, 5)))
        caret.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        caret_row.addWidget(caret)


        self._popover.add_row("Lecture", self._lecture_edit)
        self._popover.add_row("Deck", self._deck_combo)
        self._popover.add_row("Notes PDF", self._notes_pdf_btn)

        # ── Rail + stage ──────────────────────────────────────────────────
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._rail = _SlideRail(self._render_thumb, self._thumb_info)
        self._rail.currentRowChanged.connect(self._on_rail_row)
        self._rail.setVisible(False)
        body.addWidget(self._rail)

        self._stage = QWidget()
        self._stage.setObjectName("stage")
        self._stage.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        stage_box = QVBoxLayout(self._stage)
        stage_box.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        stage_box.addWidget(self._stack)

        # this slide's mode, on the slide: hide all and show one, or hide one
        self._mode_seg = QFrame(self._stage)
        self._mode_seg.setObjectName("seg")
        mrow = QHBoxLayout(self._mode_seg)
        mrow.setContentsMargins(3, 3, 3, 3)
        mrow.setSpacing(2)
        self._mode_btns = {}
        for mode, label in (("ao", "Hide All"), ("oa", "Hide One")):
            b = QToolButton()
            b.setObjectName("segBtn")
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setFixedSize(36, 28)
            b.setIconSize(QSize(22, 22))
            b.setAccessibleName(label)
            b.clicked.connect(lambda _=False, m=mode: self._set_page_mode(m))
            mrow.addWidget(b)
            self._mode_btns[mode] = b
        self._mode_ao_btn, self._mode_oa_btn = self._mode_btns["ao"], self._mode_btns["oa"]
        self._mode_seg.adjustSize()
        self._mode_seg.hide()

        # previous, "3 of 9", next: bottom right of the stage
        self._pager = QFrame(self._stage)
        self._pager.setObjectName("seg")
        prow = QHBoxLayout(self._pager)
        prow.setContentsMargins(3, 3, 3, 3)
        prow.setSpacing(0)
        prow.addWidget(self._prev_btn)
        prow.addWidget(self._sub_label)
        prow.addWidget(self._next_btn)
        self._pager.setFixedSize(3 + 26 + self._sub_label.width() + 26 + 3, 34)
        self._pager.hide()
        self._stage.installEventFilter(self)

        # nothing open yet: one button in the middle
        empty = QWidget()
        empty_box = QVBoxLayout(empty)
        empty_box.addStretch(1)
        empty_row = QHBoxLayout()
        empty_row.addStretch(1)
        self._empty_open_btn = QPushButton("Open PDF")
        self._empty_open_btn.setObjectName("primary")
        self._empty_open_btn.clicked.connect(self._open_pdf)
        empty_row.addWidget(self._empty_open_btn)
        empty_row.addStretch(1)
        empty_box.addLayout(empty_row)
        empty_box.addStretch(1)
        self._stack.addWidget(empty)

        self._canvas = OcclusionCanvas()
        self._canvas.boxes_changed.connect(self._on_boxes_changed)
        self._canvas.slide_nav.connect(self._on_slide_nav)
        self._canvas.zoom_gesture.connect(self._on_zoom_gesture)
        self._canvas.scan_region.connect(self._on_scan_region)
        self._canvas.scan_cancelled.connect(self._on_scan_cancelled)
        self._canvas.annotations_changed.connect(self._on_annotations_changed)
        self._canvas.label_activated.connect(self._on_label_activated)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("stageScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidget(self._canvas)
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._backdrop = _StageBackdrop(self._scroll.viewport(), self._canvas)

        # Cloze lives in the window, not over it: cards already made on the
        # left, the composer on the right, the slide keeping the middle.
        # Both start hidden and cost nothing until used.
        self._cloze_column = ClozeColumn()
        self._cloze_column.setVisible(False)
        self._cloze = ClozeComposer(self)
        # The panel is capped at half the slide's height and pinned to the
        # top of its side, so the Add button can't end up next to Create
        # cards — two buttons that do very different things.
        self._cloze_side = QWidget()
        side = QVBoxLayout(self._cloze_side)
        side.setContentsMargins(0, 12, 12, 0)
        side.addWidget(self._cloze)
        side.addStretch()
        self._cloze_side.setVisible(False)
        self._cloze.card_created.connect(self._on_cloze_created)
        self._cloze.card_updated.connect(self._on_cloze_updated)
        self._cloze_column.card_activated.connect(self._on_cloze_activated)
        self._cloze_column.card_delete_requested.connect(self._on_cloze_delete)

        work = QWidget()
        mid = QHBoxLayout(work)
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        # margins round the column that go away with it
        self._column_wrap = QWidget()
        wrap_box = QVBoxLayout(self._column_wrap)
        wrap_box.setContentsMargins(14, 14, 0, 14)
        wrap_box.addWidget(self._cloze_column)
        self._column_wrap.setVisible(False)
        self._cloze_column.installEventFilter(self)
        mid.addWidget(self._column_wrap)
        mid.addWidget(self._scroll, stretch=1)
        mid.addWidget(self._cloze_side)
        self._stack.addWidget(work)

        body.addWidget(self._stage, stretch=1)
        root.addLayout(body, stretch=1)

        # Re-fit a fitted slide when the window is resized, once it settles.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(80)
        self._resize_timer.timeout.connect(self._refit)

        self._apply_theme()

        # ── Keep keyboard focus on the canvas ─────────────────────────────
        # The canvas only receives key events while it holds focus, and it is
        # given focus as soon as a PDF loads. A button that takes focus when
        # clicked would silently kill every canvas key — arrows, Delete,
        # G/U/N, Esc — until you clicked the slide again. Every one of these
        # has a keyboard shortcut (D, V, T, F, Ctrl+Shift+V, Ctrl+0, Ctrl+±),
        # so dropping them from the Tab chain puts nothing out of reach.
        #
        # Deliberately NOT in this list: the title (opens the popover), the
        # lecture field, the two combos and the notes-PDF button, which need
        # focus to be usable from the keyboard.
        for w in (self._draw_btn, self._select_btn, self._text_btn,
                  self._detect_btn, self._cloze_btn, self._auto_btn,
                  self._zoom_label, self._open_btn, self._create_btn):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Clicking the grey margin around the slide targets the scroll area,
        # not the canvas; without this it would take focus and cause the same
        # dead-keys problem.
        self._scroll.setFocusProxy(self._canvas)

        # ── Shortcuts ─────────────────────────────────────────────────────
        # Left/Right nudge the selection and otherwise flip slides. They're
        # bound here as well as on the canvas because the canvas only sees key
        # events while it holds focus — and flipping slides is exactly what you
        # do before ever clicking on one. PgUp/PgDn always flip slides.
        # None of these fire while a text field has focus: QLineEdit claims the
        # arrows (and Ctrl+Z, and plain letters) for editing before Qt looks at
        # window shortcuts, so typing a lecture name is unaffected.
        for keys, key in (("Left", Qt.Key.Key_Left), ("Right", Qt.Key.Key_Right),
                          ("Shift+Left", Qt.Key.Key_Left),
                          ("Shift+Right", Qt.Key.Key_Right)):
            QShortcut(QKeySequence(keys), self,
                      lambda k=key, s=keys.startswith("Shift"): self._arrow(k, s))
        QShortcut(QKeySequence(Qt.Key.Key_PageUp), self, self._prev_page)
        QShortcut(QKeySequence(Qt.Key.Key_PageDown), self, self._next_page)
        QShortcut(QKeySequence("D"), self, lambda: self._set_tool("draw"))
        QShortcut(QKeySequence("V"), self, lambda: self._set_tool("select"))
        QShortcut(QKeySequence("T"), self, lambda: self._set_tool("text"))
        QShortcut(QKeySequence("F"), self, self._arm_detect)
        QShortcut(QKeySequence("P"), self, self._toggle_peek)
        QShortcut(QKeySequence("Ctrl+O"), self, self._open_pdf)
        # V for Vasu, who asked for the cloze composer. Plain V is already
        # the Select tool, so it takes the modifiers.
        QShortcut(QKeySequence("Ctrl+Shift+V"), self, self._open_cloze)
        QShortcut(QKeySequence("Ctrl+="), self, self._zoom_in)
        QShortcut(QKeySequence("Ctrl++"), self, self._zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self, self._zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self, self._zoom_fit)
        # Window-level so they work when a button has focus. QLineEdit
        # overrides these while it has focus, so text editing is unaffected.
        QShortcut(QKeySequence("Ctrl+Z"), self, self._canvas.undo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, self._canvas.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self, self._canvas.redo)
        QShortcut(QKeySequence("Ctrl+C"), self, self._canvas.copy_selected)
        QShortcut(QKeySequence("Ctrl+V"), self, self._canvas.paste)

        self._update_controls()

    def eventFilter(self, obj, ev):
        if obj is self._stage and ev.type() == QEvent.Type.Resize:
            self._place_mode_seg()
        if obj is self._cloze_column and ev.type() in (
                QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self._column_wrap.setVisible(ev.type() == QEvent.Type.ShowToParent)
        return super().eventFilter(obj, ev)

    def _apply_theme(self):
        """Colours and icons for Anki's current theme. Runs again when the
        theme changes with the window open."""
        c = _colors()
        self.setStyleSheet(_window_qss(c))
        self._create_btn.setStyleSheet(_primary_qss(c) + "QPushButton{padding:0;}")
        self._create_btn.setIcon(_icon("cards", c, 20, color="#ffffff"))
        for b in (self._rail_btn, self._prev_btn, self._next_btn):
            b.setIcon(_icon(b.property("_icon"), c))
        for mode, b in self._mode_btns.items():
            b.setIcon(_mode_icon(mode, c))
        self._empty_open_btn.setStyleSheet(_primary_qss(c))
        for b in (self._draw_btn, self._select_btn, self._text_btn,
                  self._detect_btn, self._cloze_btn):
            b.setIcon(_icon(b.property("_icon"), c))
        self._open_btn.setIcon(_icon("open", c))
        self._auto_btn.setIcon(_icon("auto", c, 18, color=c["accent_text"]))
        self._title_btn.setIcon(_icon("caret", c, 16, color=c["text"]))
        self._rail.viewport().update()
        self._scroll.viewport().update()

    def _show_popover(self):
        if self._docs:
            self._popover.show_under(self._title_btn)

    def _on_theme_changed(self):
        try:
            self._apply_theme()
        except RuntimeError:        # window already deleted
            pass

    def _populate_decks(self):
        current = mw.col.decks.name(mw.col.decks.selected())
        default = _cfg("default_deck", "") or current
        names = sorted(
            d.name for d in mw.col.decks.all_names_and_ids(include_filtered=False)
        )
        self._deck_combo.addItems(names)
        idx = self._deck_combo.findText(default)
        if idx >= 0:
            self._deck_combo.setCurrentIndex(idx)
        else:
            self._deck_combo.setEditText(default)

    def _set_tool(self, tool: str):
        if self._canvas.scan_armed():
            self._canvas.arm_scan(False)
        self._canvas.set_tool(tool)
        self._sync_tools()

    def _sync_tools(self):
        """One raised segment for the tool in use (Detect while it waits for
        its region), plus Cloze while its panel is open."""
        armed = self._canvas.scan_armed()
        tool = self._canvas.tool()
        self._draw_btn.setChecked(not armed and tool == "draw")
        self._select_btn.setChecked(not armed and tool == "select")
        self._text_btn.setChecked(not armed and tool == "text")
        self._detect_btn.setChecked(armed)
        self._cloze_btn.setChecked(self._cloze_side.isVisible())

    def _default_mode(self) -> str:
        """PDF-wide occlusion mode — the config value; slides override it."""
        mode = _cfg("occlusion_mode", "ao")
        return mode if mode in ("ao", "oa") else "ao"

    # ---------------------------------------------------------------- docs --

    def _doc_for_page(self, idx: int) -> Optional[dict]:
        for doc in self._docs:
            if doc["start"] <= idx < doc["start"] + doc["count"]:
                return doc
        return None

    def _on_lecture_edited(self, text: str):
        doc = self._doc_for_page(self._page_index)
        if doc:
            doc["lecture"] = text.strip()
            self._sync_title()

    # ---------------------------------------------------------- notes PDF --
    #
    # A separate lecture-notes PDF, attached per document. Its path (and the
    # slide deck's) is written onto every card this document makes, so the
    # Notes / Slides buttons on the back of the card can open them. Only the
    # path is stored — the PDF is never copied into the collection.

    def _notes_pdf(self) -> str:
        doc = self._doc_for_page(self._page_index)
        return doc.get("notes_pdf", "") if doc else ""

    def _choose_notes_pdf(self):
        doc = self._doc_for_page(self._page_index)
        if not doc:
            return
        start_dir = os.path.dirname(doc.get("notes_pdf") or doc["path"])
        self._popover.hide()
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose Lecture Notes PDF", start_dir, "PDF Files (*.pdf)"
        )
        if path:
            doc["notes_pdf"] = path
            self._sync_notes_pdf_btn()
        self._popover.show_under(self._title_btn)

    def _remove_notes_pdf(self):
        doc = self._doc_for_page(self._page_index)
        if doc:
            doc["notes_pdf"] = ""
            self._sync_notes_pdf_btn()

    def _fill_notes_pdf_menu(self):
        self._notes_pdf_menu.clear()
        path = self._notes_pdf()
        self._notes_pdf_menu.addAction(
            "Choose" if not path else "Replace", self._choose_notes_pdf)
        act = self._notes_pdf_menu.addAction(
            "Open", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        act.setEnabled(bool(path) and os.path.exists(path))
        act = self._notes_pdf_menu.addAction("Remove", self._remove_notes_pdf)
        act.setEnabled(bool(path))

    def _sync_notes_pdf_btn(self):
        """Reflect the current document's attachment on the button."""
        self._notes_pdf_btn.setEnabled(bool(self._docs))
        path = self._notes_pdf()
        if not path:
            self._notes_pdf_btn.setText("Choose")
            self._notes_pdf_btn.setToolTip("")
            return
        name = os.path.basename(path)
        if len(name) > 28:
            name = name[:27] + "…"
        missing = not os.path.exists(path)
        self._notes_pdf_btn.setText(name + (" (missing)" if missing else ""))
        self._notes_pdf_btn.setToolTip(
            path + ("\n\n(this file can no longer be found)" if missing else "")
        )

    # ---------------------------------------------------------------- PDF --

    def _open_pdf(self):
        # start where the last PDF was picked from, if that folder still exists
        start = _cfg("last_open_dir", "")
        if not (start and os.path.isdir(start)):
            start = ""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open PDF", start, "PDF Files (*.pdf)"
        )
        if paths:
            _set_cfg("last_open_dir", os.path.dirname(paths[0]))
        self._open_paths(paths)

    def _fill_recent_menu(self):
        self._recent_menu.clear()
        act = self._recent_menu.addAction("Open PDF", self._open_pdf)
        act.setShortcut(QKeySequence("Ctrl+O"))
        act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        sessions = session_store.list_sessions(8)
        if not sessions:
            return
        self._recent_menu.addSeparator()
        for s in sessions:
            path = s["pdf_path"]
            name = s.get("lecture") or os.path.splitext(os.path.basename(path))[0]
            n_boxes = sum(len(v) for v in s.get("boxes", {}).values())
            label = f"{name}  ({n_boxes} box{'es' if n_boxes != 1 else ''})"
            self._recent_menu.addAction(
                label, lambda p=path: self._open_recent(p))

    def _open_recent(self, path: str):
        if not os.path.exists(path):
            if askUser(
                f"The PDF can no longer be found:\n{path}\n\n"
                "Forget this session?"
            ):
                session_store.delete(path)
            return
        self._open_paths([path])

    def _render_paths(self, paths: list[str]) -> Optional[list[tuple[str, list[QImage]]]]:
        """Render every PDF, one shared progress dialog. None if cancelled."""
        progress = QProgressDialog("Rendering PDF", "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)

        rendered: list[tuple[str, list[QImage]]] = []
        for path in paths:
            progress.setLabelText(f"Rendering {os.path.basename(path)}")

            def on_progress(done: int, total: int):
                progress.setMaximum(total)
                progress.setValue(done)
                mw.app.processEvents()
                return not progress.wasCanceled()

            try:
                pages = render_pdf(path, scale=self._render_scale,
                                   on_progress=on_progress)
            except Exception as exc:
                progress.close()
                telemetry.track("error", where="open_pdf", message=str(exc)[:200])
                showWarning(f"Could not open this PDF:\n{path}\n\n{exc}")
                return None
            if progress.wasCanceled():
                progress.close()
                return None
            if pages:
                rendered.append((path, pages))
        # read the cancel state BEFORE close() — closing a QProgressDialog
        # emits canceled(), which flips wasCanceled() to True
        canceled = progress.wasCanceled()
        progress.close()
        if canceled or not rendered:
            return None
        return rendered

    def _open_paths(self, paths: list[str], silent_resume: bool = False):
        paths = [p for p in paths if p]
        if not paths:
            return

        # keep whatever was in progress before switching documents
        self._persist_sessions()

        self._render_scale = float(_cfg("render_dpi_scale", 2.0))
        # pages are rendered when shown, not all up front (see LazyPages)
        try:
            all_pages = LazyPages(paths, self._render_scale)
        except Exception as exc:     # noqa: BLE001 - a damaged or locked PDF
            telemetry.track("error", where="open_pdf", message=str(exc)[:200])
            showWarning(f"Could not open this PDF:\n{exc}")
            return
        if not all_pages:
            all_pages.close()
            return

        docs: list[dict] = []
        start = 0
        for path, count in zip(paths, all_pages.counts):
            if count:
                docs.append({
                    "path": path,
                    "lecture": os.path.splitext(os.path.basename(path))[0],
                    "start": start,
                    "count": count,
                    "notes_pdf": "",
                    "note_map": {},
                    "image_map": {},
                })
            start += count

        if isinstance(self._pages, LazyPages):
            self._pages.close()
        self._docs = docs
        self._pages = all_pages
        self._page_index = 0
        self._boxes.clear()
        self._page_modes.clear()
        self._annots.clear()
        self._page_cache.clear()
        # keyed by page index too, so they would land on the new PDF's slides
        self._cloze_cards = {}
        self._cloze_stale = []

        # the first recognition on a machine loads macOS's models and takes
        # ~30s; get that out of the way now rather than under a Detect click
        ocr.warm_up()

        telemetry.track("pdf_open", pages=len(all_pages), files=len(docs), reopened=silent_resume)
        self._maybe_resume_sessions(silent=silent_resume)

        self._rail.reset_pages(self._page_sizes())
        self._rail.setVisible(self._rail_btn.isChecked())
        self._stack.setCurrentIndex(1)
        self._show_page()
        self._apply_default_zoom()
        # so the arrows, Delete, G/U etc. reach the canvas without a click first
        self._canvas.setFocus()

    # ------------------------------------------------------------- sessions --

    def _maybe_resume_sessions(self, silent: bool = False):
        sessions = {i: session_store.load(doc["path"])
                    for i, doc in enumerate(self._docs)}
        resumable = {
            i: s for i, s in sessions.items()
            if s and (s.get("boxes") or s.get("note_map")
                      or s.get("notes_pdf") or s.get("cloze_cards"))
        }
        if not resumable:
            return

        if not silent:
            names = ", ".join(
                os.path.basename(self._docs[i]["path"]) for i in resumable)
            if not askUser(
                f"Resume your saved session for {names}?",
                title="PDF Occlusion",
            ):
                for i in resumable:
                    session_store.delete(self._docs[i]["path"])
                return

        applied_settings = False
        for i, s in resumable.items():
            doc = self._docs[i]
            saved_scale = float(s.get("render_scale") or self._render_scale)
            factor = self._render_scale / saved_scale if saved_scale else 1.0

            for k, blist in (s.get("boxes") or {}).items():
                try:
                    local = int(k)
                except ValueError:
                    continue
                if not 0 <= local < doc["count"]:
                    continue
                if factor != 1.0:
                    for b in blist:
                        b["x"] = int(b["x"] * factor)
                        b["y"] = int(b["y"] * factor)
                        b["w"] = int(b["w"] * factor)
                        b["h"] = int(b["h"] * factor)
                self._boxes[doc["start"] + local] = blist

            self._cloze_stale += [int(n) for n in (s.get("cloze_stale") or [])]

            for k, cards in (s.get("cloze_cards") or {}).items():
                try:
                    local = int(k)
                except ValueError:
                    continue
                if cards and 0 <= local < doc["count"]:
                    page = doc["start"] + local
                    restored = []
                    for card in cards:
                        entry = dict(card)
                        # Sessions written before records had their own id
                        # get one now — without it every such record keys on
                        # None, and they collide with each other.
                        entry.setdefault("uid", uuid.uuid4().hex)
                        entry.setdefault("nid", None)
                        entry["page"] = page
                        restored.append(entry)
                    self._cloze_cards[page] = restored

            for k, a in (s.get("annotations") or {}).items():
                try:
                    local = int(k)
                except ValueError:
                    continue
                if 0 <= local < doc["count"] and a:
                    for entry in a:
                        entry.setdefault("id", uuid.uuid4().hex)
                        # same pixel space as the boxes, so the same rescale
                        if factor != 1.0:
                            for f in ("x", "y", "w", "h"):
                                entry[f] = int(entry[f] * factor)
                    self._annots[doc["start"] + local] = a

            for k, m in (s.get("page_modes") or {}).items():
                try:
                    local = int(k)
                except ValueError:
                    continue
                if m in ("ao", "oa") and 0 <= local < doc["count"]:
                    self._page_modes[doc["start"] + local] = m

            doc["lecture"] = s.get("lecture") or doc["lecture"]
            doc["notes_pdf"] = s.get("notes_pdf") or ""
            doc["note_map"] = s.get("note_map") or {}
            # the slide PNGs only match if the render scale is unchanged
            doc["image_map"] = (s.get("image_map") or {}) if factor == 1.0 else {}

            if not applied_settings:
                applied_settings = True
                if s.get("deck"):
                    self._deck_combo.setEditText(s["deck"])

    def _persist_sessions(self):
        if not self._docs or not self._pages:
            return
        self._save_current_boxes()
        for doc in self._docs:
            start, count = doc["start"], doc["count"]
            boxes = {
                str(i - start): self._boxes[i]
                for i in range(start, start + count)
                if self._boxes.get(i)
            }
            cloze_cards = {
                str(i - start): self._cloze_cards[i]
                for i in range(start, start + count)
                if self._cloze_cards.get(i)
            }
            annots = {
                str(i - start): self._annots[i]
                for i in range(start, start + count)
                if self._annots.get(i)
            }
            if (not boxes and not cloze_cards and not annots
                    and not self._cloze_stale
                    and not doc.get("note_map") and not doc.get("notes_pdf")):
                session_store.delete(doc["path"])
                continue
            saved = session_store.save(doc["path"], {
                "lecture": doc["lecture"],
                "notes_pdf": doc.get("notes_pdf", ""),
                "boxes": boxes,
                "page_modes": {
                    str(i - start): m for i, m in self._page_modes.items()
                    if start <= i < start + count},
                "annotations": annots,
                "note_map": doc.get("note_map", {}),
                "image_map": doc.get("image_map", {}),
                "cloze_cards": cloze_cards,
                "cloze_stale": [n for n in self._cloze_stale],
                "render_scale": self._render_scale,
                "page_count": count,
                "deck": self._deck_combo.currentText().strip(),
            })
            if not saved and not self._save_warned:
                self._save_warned = True
                showWarning(
                    f"Couldn't save your work on "
                    f"{os.path.basename(doc['path'])}.\n\n"
                    "Until it saves, creating cards again may make "
                    "duplicate cards. Another program (antivirus, OneDrive) "
                    f"may be locking this folder:\n{session_store.folder()}"
                )

    def done(self, result: int):
        # runs on accept, reject (Esc) and window close — work is never lost
        self._persist_sessions()
        try:
            gui_hooks.theme_did_change.remove(self._on_theme_changed)
        except ValueError:
            pass
        if isinstance(self._pages, LazyPages):
            self._pages.close()
            self._pages = []
        super().done(result)

    # ---------------------------------------------------------------- zoom --

    def _apply_default_zoom(self):
        z = _cfg("default_zoom", "fit")
        if z == "fit":
            self._zoom_fit()
        else:
            try:
                self._set_zoom(float(z))
            except (TypeError, ValueError):
                self._zoom_fit()

    def _refit(self):
        """Re-fit after the side panels change how much room the slide has —
        but only if the slide was fitted to begin with; a zoom the user set
        by hand is theirs to keep. Deferred by a tick so the new layout is
        settled before the viewport is measured."""
        if self._pages and self._fitted:
            QTimer.singleShot(0, self._zoom_fit)

    def _set_zoom(self, z: float):
        self._fitted = False        # _zoom_fit sets it back after this call
        self._canvas.set_zoom(z)
        self._zoom_label.setText(f"{int(self._canvas.zoom() * 100)}%")

    def _sync_zoom_slider(self):
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(int(round(self._canvas.zoom() * 100)))
        self._zoom_slider.blockSignals(False)

    def _on_zoom_slider(self, value: int):
        """Zoom about the middle of what is on screen, so the slider never
        throws you to the corner of the slide."""
        if not self._pages:
            return
        old = self._canvas.zoom()
        vp = self._scroll.viewport()
        centre = QPoint(self._scroll.horizontalScrollBar().value() + vp.width() // 2,
                        self._scroll.verticalScrollBar().value() + vp.height() // 2)
        self._on_zoom_gesture(value / 100 / old - 1, centre)

    def _zoom_in(self):
        z = self._canvas.zoom()
        bigger = [s for s in _ZOOM_STEPS if s > z + 0.01]
        if bigger:
            self._set_zoom(bigger[0])

    def _zoom_out(self):
        z = self._canvas.zoom()
        smaller = [s for s in _ZOOM_STEPS if s < z - 0.01]
        if smaller:
            self._set_zoom(smaller[-1])

    def _on_zoom_gesture(self, delta: float, pos):
        """Pinch / Ctrl+scroll: smooth zoom anchored at the cursor."""
        if not self._pages:
            return
        old = self._canvas.zoom()
        new = max(0.1, min(4.0, old * (1.0 + delta)))
        if abs(new - old) < 1e-4:
            return
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        # keep the image point under the cursor stationary: its canvas
        # coordinate scales by (new/old), so shift the viewport by the same
        ratio = new / old
        hv, vv = hbar.value(), vbar.value()
        self._set_zoom(new)
        hbar.setValue(int(pos.x() * ratio - (pos.x() - hv)))
        vbar.setValue(int(pos.y() * ratio - (pos.y() - vv)))

    def _zoom_fit(self):
        """Zoom so the entire slide is visible in the scroll area."""
        if not self._pages:
            return
        img = self._pages[self._page_index]
        natural_w = img.width() / self._render_scale
        natural_h = img.height() / self._render_scale
        if natural_w <= 0 or natural_h <= 0:
            return
        vp = self._scroll.viewport()
        m = 2 * _FIT_MARGIN
        self._set_zoom(min((vp.width() - m) / natural_w,
                           (vp.height() - m) / natural_h))
        self._fitted = True

    # --------------------------------------------------------------- pages --

    def _save_current_boxes(self):
        # while _page_index is still this slide, so the text lands on it
        self._canvas.commit_edit()
        if self._canvas.has_image():
            self._boxes[self._page_index] = self._canvas.get_boxes()

    def _show_page(self):
        if not self._pages:
            return
        self._canvas.set_image(
            self._pages[self._page_index],
            self._boxes.get(self._page_index, []),
            render_scale=self._render_scale,
        )
        self._canvas.set_annotations(self._annots.get(self._page_index, []))
        doc = self._doc_for_page(self._page_index)
        if doc and self._lecture_edit.text().strip() != doc["lecture"]:
            self._lecture_edit.blockSignals(True)
            self._lecture_edit.setText(doc["lecture"])
            self._lecture_edit.blockSignals(False)
        self._rail.set_current(self._page_index)
        self._sync_tools()
        self._sync_page_mode_combo()
        self._update_controls()
        self._refresh_count()
        if self._cloze.isVisible():
            self._cloze.refresh_slide()
        self._refresh_cloze_column()
        self._schedule_prefetch()

    def _schedule_prefetch(self):
        """Render the slides next to this one while the window is idle, one per
        tick, so moving to them is instant; a newer call cancels an older one."""
        if not isinstance(self._pages, LazyPages):
            return
        self._prefetch_gen = getattr(self, "_prefetch_gen", 0) + 1
        gen = self._prefetch_gen

        def step():
            if gen != self._prefetch_gen or not isinstance(self._pages, LazyPages):
                return
            if self._pages.prefetch(self._page_index) is not None:
                QTimer.singleShot(10, step)
        QTimer.singleShot(50, step)

    def _sync_page_mode_combo(self):
        """Show the slide's effective mode: its override, else the config default."""
        mode = self._page_modes.get(self._page_index) or self._default_mode()
        self._mode_btns[mode].setChecked(True)
        self._mode_seg.setVisible(bool(self._pages))
        self._pager.setVisible(bool(self._pages))
        self._place_mode_seg()

    def _place_mode_seg(self):
        """The slide's mode in the stage's bottom-left corner, the slide
        arrows in its bottom-right corner."""
        seg, pager = self._mode_seg, self._pager
        seg.adjustSize()
        h = self._stage.height()
        seg.move(14, h - seg.height() - 14)
        pager.move(self._stage.width() - pager.width() - 14, h - pager.height() - 14)
        seg.raise_()
        pager.raise_()

    def _set_page_mode(self, mode: str):
        if not self._pages:
            return
        # picking the default again = no override
        if mode == self._default_mode():
            self._page_modes.pop(self._page_index, None)
        else:
            self._page_modes[self._page_index] = mode

    def _arrow(self, key, shift: bool):
        if self._pages:
            self._canvas.handle_arrow(key, shift)

    def _toggle_rail(self):
        on = self._rail_btn.isChecked()
        _set_cfg("show_slides", on)
        if self._pages:
            self._rail.setVisible(on)
            if on:
                self._rail.set_current(self._page_index)
            self._refit()

    def _sync_nav(self):
        has = bool(self._pages)
        self._rail_btn.setEnabled(has)
        self._prev_btn.setVisible(has)
        self._next_btn.setVisible(has)
        self._prev_btn.setEnabled(has and self._page_index > 0)
        self._next_btn.setEnabled(has and self._page_index < len(self._pages) - 1)

    def _on_slide_nav(self, direction: int):
        self._next_page() if direction > 0 else self._prev_page()

    def _prev_page(self):
        if self._pages and self._page_index > 0:
            self._save_current_boxes()
            self._page_index -= 1
            self._show_page()

    def _next_page(self):
        if self._pages and self._page_index < len(self._pages) - 1:
            self._save_current_boxes()
            self._page_index += 1
            self._show_page()

    def _go_to(self, idx: int):
        if self._pages and 0 <= idx < len(self._pages) and idx != self._page_index:
            self._save_current_boxes()
            self._page_index = idx
            self._show_page()

    def _on_rail_row(self, row: int):
        self._go_to(row)
        self._canvas.setFocus()

    def _on_boxes_changed(self):
        self._refresh_count()

    # ------------------------------------------------------------ slide text --
    #
    # Text written onto the slide itself. Not a card and not a note: it joins
    # the picture, so it lands on every card cut from this slide and a box
    # can occlude it exactly like something the lecturer had printed there.

    def _on_annotations_changed(self):
        """The canvas edits text in place and hands back the result — this
        only has to remember it and drop the composited page."""
        here = [a for a in self._canvas.annotations() if (a.get("text") or "").strip()]
        if here:
            self._annots[self._page_index] = here
        else:
            self._annots.pop(self._page_index, None)
        self._page_cache.pop(self._page_index, None)
        self._refresh_count()

    def _on_label_activated(self, box_id: str):
        """Only reached for a box with nowhere legible to put a label — the
        canvas edits the rest in place."""
        self._say_detect("Box too small for a label", transient=True)

    def _page_image(self, idx: int) -> QImage:
        """The slide as it actually looks — its own pixels plus any text
        written onto it. This, not the raw render, is what gets occluded,
        recognised and written onto cards."""
        annots = self._annots.get(idx)
        if not annots:
            return self._pages[idx]
        cached = self._page_cache.get(idx)
        if cached is not None:
            return cached
        img = QImage(self._pages[idx])
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        for a in annots:
            draw_annotation(p, a, annotation_rect(a))
        p.end()
        self._page_cache[idx] = img
        return img

    # ------------------------------------------------------------ detection --
    #
    # Detect never runs on the whole slide. Click it and it arms the canvas;
    # the next drag says which part to scan, and only that part is scanned.
    # A slide usually has one table worth boxing and a title, a footer and a
    # page number that are not — so "everything on the slide" was almost
    # never the right answer, and undoing it was busywork.

    def _arm_detect(self):
        """Click 1 of 2: wait for the region. Clicking again stands down."""
        if not self._pages:
            self._sync_tools()
            return
        self._canvas.arm_scan(not self._canvas.scan_armed())
        # No prompt: the raised segment, the crosshair and the dimming that
        # follows the drag already say what is going on.
        self._sync_tools()

    def _toggle_peek(self):
        """P — flip the masks between the configured opacity and see-through.

        Keyboard only: worth reaching for just when editing_mask_opacity is
        high enough to hide the slide, which is not the default.
        """
        self._canvas.set_peeking(not self._canvas.peeking())

    def _on_scan_cancelled(self):
        self._sync_tools()

    def _on_scan_region(self, x: float, y: float, w: float, h: float):
        """Click 2 of 2: box whatever is inside the region just dragged out.

        Cells if a ruled table is in there, lines of text if not — which is
        what the slide itself decides, so there is nothing for the button to
        ask about.
        """
        self._sync_tools()
        doc = self._doc_for_page(self._page_index)
        if not doc:
            return
        local = self._page_index - doc["start"]
        region = (x, y, w, h)

        try:
            rects = [r for r in get_table_cell_rects(
                doc["path"], local, self._render_scale)
                if _centre_inside(r, region)]
            if len(rects) < 2:
                # the region goes in, rather than filtering afterwards: a
                # drag over one word of a title must give that word, not the
                # run it happens to sit in
                rects = get_text_word_rects(
                    doc["path"], local, self._render_scale, region=region)
        except Exception as exc:
            telemetry.track("error", where="detect", message=str(exc)[:200])
            showWarning(f"Detection failed:\n{exc}")
            return

        # Nothing in the PDF's own data? Then the slide is a picture of one,
        # and the only way in is to read it off the pixels.
        if not rects and ocr.available():
            try:
                rects = self._ocr_region(region)
            except ocr.OcrError as exc:
                telemetry.track("error", where="ocr", message=str(exc)[:200])
                showWarning(f"Text recognition failed:\n{exc}")
                return

        found = len(rects)
        rects = [r for r in rects if not self._already_boxed(r)]
        if not rects:
            # Both of these are a passing tooltip rather than a dialog.
            # Neither is an error and neither needs a decision, and a modal
            # you have to dismiss to try the next region is the wrong weight
            # for "that one came up empty".
            self._say_detect("Already boxed" if found else "Nothing found",
                             transient=True)
            return

        self._canvas.add_boxes([
            {"x": int(rx), "y": int(ry), "w": int(rw), "h": int(rh),
             "group": None, "shape": "rect", "id": uuid.uuid4().hex}
            for rx, ry, rw, rh in rects])

    def _ocr_region(self, region: tuple) -> list:
        """Read the words out of the pixels of one region of the slide.

        Only the region is handed over, not the whole slide: it is less for
        the recogniser to chew on, and it cannot drag in a heading from the
        far side of the page that the drag deliberately left out.
        """
        x, y, w, h = (int(v) for v in region)
        page = self._page_image(self._page_index)
        crop = page.copy(QRect(max(0, x), max(0, y),
                               min(int(w), page.width()), min(int(h), page.height())))
        # merged the same way the PDF's own text is, so a phrase read off
        # the pixels comes out as one box too
        found = merge_word_rects([(rx, ry, rw, rh)
                                  for rx, ry, rw, rh, _t in ocr.recognize(crop)])
        return [(rx + max(0, x), ry + max(0, y), rw, rh)
                for rx, ry, rw, rh in found]

    def _already_boxed(self, rect: tuple, slack: int = 3) -> bool:
        """Is this rect already on the slide? Scanning the same region twice
        should not leave two boxes stacked on every line of it."""
        x, y, w, h = rect
        for b in self._canvas.get_boxes():
            if (abs(b["x"] - x) <= slack and abs(b["y"] - y) <= slack
                    and abs(b["w"] - w) <= slack and abs(b["h"] - h) <= slack):
                return True
        return False

    def _say_detect(self, msg: str, transient: bool = False):
        """Say what Detect just did, briefly, near the slide."""
        if msg:
            tooltip(msg, period=2500, parent=self)

    # -------------------------------------------------------------- counter --

    def _expected_cards(self) -> int:
        """Cards that Create All Cards would make right now, across all slides."""
        total = 0
        for i in range(len(self._pages)):
            if i == self._page_index and self._canvas.has_image():
                boxes = self._canvas.get_boxes()
            else:
                boxes = self._boxes.get(i, [])
            gids = {b["group"] for b in boxes if b.get("group") is not None}
            total += len(gids) + sum(1 for b in boxes if b.get("group") is None)
        return total

    def _cloze_cards_total(self) -> int:
        """Cards the cloze notes made — one per c-number, every slide."""
        return sum(cloze_card_count(c["text"])
                   for cards in self._cloze_cards.values() for c in cards)

    def _refresh_count(self):
        """The primary button says how many cards it would make, boxes and
        cloze together. It stays live at zero when there are cards out there
        to update or delete (all boxes on a slide removed, say)."""
        if not self._pages:
            self._create_btn.set_count(0)
            self._create_btn.setToolTip("")
            self._create_btn.setEnabled(False)
            self._sync_nav()
            return
        occlusion = self._expected_cards()
        cloze = self._cloze_cards_total()
        n = occlusion + cloze
        self._create_btn.set_count(n)
        self._create_btn.setToolTip(
            f"{occlusion} occlusion, {cloze} cloze" if cloze else "")
        pending = bool(self._cloze_stale) or any(d.get("note_map") for d in self._docs)
        self._create_btn.setEnabled(bool(n) or pending)
        self._sync_nav()
        self._rail.refresh_row(self._page_index)

    def _update_controls(self):
        has = bool(self._pages)
        for w in (self._draw_btn, self._select_btn, self._text_btn,
                  self._detect_btn, self._cloze_btn, self._zoom_label,
                  self._mode_seg):
            w.setEnabled(has)
        self._zoom_label.setVisible(has)
        self._auto_btn.setEnabled(has and not getattr(self, "_auto_run", None))
        self._sync_notes_pdf_btn()
        self._sync_title()
        self._refresh_count()

    def _sync_title(self):
        """Lecture name, then where in it you are, e.g. 12 of 54."""
        doc = self._doc_for_page(self._page_index) if self._pages else None
        self._title_btn.setVisible(doc is not None)
        self._sub_label.setVisible(doc is not None)
        if doc is None:
            return
        name = doc["lecture"] or os.path.splitext(os.path.basename(doc["path"]))[0]
        fm = QFontMetrics(self._title_btn.font())
        self._title_btn.setText(fm.elidedText(name, Qt.TextElideMode.ElideRight, 420))
        local = self._page_index - doc["start"]
        self._sub_label.setText(f"{local + 1} of {doc['count']}")
        self._place_mode_seg()      # the count's width can change the corner's

    # -- thumbnails (see _SlideRail) -----------------------------------------

    def _page_sizes(self) -> list:
        """Every page's size in points, without loading any page."""
        pages = self._pages
        if not isinstance(pages, LazyPages):
            return [(img.width(), img.height()) for img in pages]
        sizes = []
        for di, n in enumerate(pages.counts):
            pdf = pages._docs[di]
            for li in range(n):
                try:
                    sizes.append(tuple(pdf.get_page_size(li)))
                except Exception:       # noqa: BLE001
                    sizes.append(sizes[-1] if sizes else (612.0, 792.0))
        return sizes

    def _render_thumb(self, idx: int, width_px: int) -> Optional[QImage]:
        pages = self._pages
        if not isinstance(pages, LazyPages) or not 0 <= idx < len(pages):
            return None
        if pages.cached(idx):             # already rendered full size: shrink it
            return pages[idx].scaledToWidth(
                width_px, Qt.TransformationMode.SmoothTransformation)
        di, li = pages._where[idx]
        page = pages._docs[di][li]
        try:
            w = page.get_width()
            return _render_page(page, width_px / w if w else 0.3)
        finally:
            page.close()

    def _thumb_info(self, idx: int):
        doc = self._doc_for_page(idx)
        label = str(idx - doc["start"] + 1) if doc else str(idx + 1)
        if idx == self._page_index and self._canvas.has_image():
            boxes = self._canvas.get_boxes()
        else:
            boxes = self._boxes.get(idx) or []
        marked = bool(boxes) or bool(self._cloze_cards.get(idx))
        sizes = self._rail._sizes
        page_w = sizes[idx][0] * self._render_scale if idx < len(sizes) else 0
        return label, boxes, marked, page_w

    # -------------------------------------------------------------- cloze --
    #
    # Slides that are too text-heavy to occlude usefully are better as cloze
    # cards with the slide kept as the extra. The composer is a separate
    # non-modal panel (cloze_dialog.py); everything it needs about the
    # current slide comes through the three methods below.

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_panel_height()
        if self._pages and self._fitted:
            self._resize_timer.start()

    def _sync_panel_height(self):
        """Keep the composer to half the height the slide has."""
        panel = getattr(self, "_cloze", None)
        if panel is None:
            return
        h = self._scroll.height()
        if h > 0:
            panel.setMaximumHeight(max(300, h // 2))

    # -------------------------------------------------------- auto-occlude --
    #
    # One lecture (the document the current slide belongs to) per run, which
    # is also one lecture credit. Boxes land the same way Detect's do: as
    # ordinary boxes, one undo step on the slide in view, nothing saved
    # until Create All Cards.

    def _auto_scope_pages(self, doc: dict, scope: str) -> list:
        start, count = doc["start"], doc["count"]
        if scope == "current":
            return [self._page_index]
        if scope == "all":
            return list(range(start, start + count))
        return [i for i in range(start, start + count) if not self._boxes.get(i)]

    def _auto_notes_text(self, doc: dict) -> str:
        path = doc.get("notes_pdf") or ""
        if not path or not os.path.exists(path):
            return ""
        parts, total = [], 0
        try:
            from .pdf_renderer import _import_pdfium
            n = len(_import_pdfium().PdfDocument(path))
        except Exception:
            return ""
        for i in range(n):
            try:
                t = get_page_text(path, i).strip()
            except Exception:
                continue
            if t:
                parts.append(t)
                total += len(t)
            if total > 12000:
                break
        return "\n\n".join(parts)[:12000]

    def _open_auto_occlude(self):
        if not self._pages:
            return
        self._save_current_boxes()
        doc = self._doc_for_page(self._page_index)
        if not doc:
            return
        counts = {k: len(self._auto_scope_pages(doc, k)) for k, _ in auto_occlude.SCOPES}
        telemetry.track("auto_occlude_open", pages=doc["count"],
                        signed_in=bool(auto_occlude.account().get("token")))
        win = auto_occlude.AutoOccludeDialog(
            self, bool(doc.get("notes_pdf")), counts, _primary_qss())
        if not win.exec() or not win.chosen:
            telemetry.track("auto_occlude_closed")
            return
        s = win.chosen
        idxs = self._auto_scope_pages(doc, s["scope"])
        telemetry.track("auto_occlude_start", pages=len(idxs), custom_chars=len(s.get("custom") or ""),
                        **{k: v for k, v in s.items() if k not in ("custom", "show_more")})
        if len(idxs) > 150:
            showWarning("Auto-occlude takes up to 150 slides at a time. "
                        "Split the PDF, or run it on the slides without boxes.",
                        parent=self, title="PDF Occlusion")
            return
        # each slide is rendered when the run reaches it, not all before it starts
        pages = [(i, doc["path"], i - doc["start"], (lambda i=i: self._page_image(i))) for i in idxs]
        notes = self._auto_notes_text(doc) if s.get("use_notes") else ""
        self._auto_replace = s["scope"] in ("all", "current")
        self._auto_btn.setEnabled(False)
        self._auto_run = auto_occlude.Run(self, pages, s, notes,
                                          self._auto_page_boxes, self._auto_finished,
                                          on_page_cards=self._auto_page_cards)
        self._auto_run.start()

    def _auto_page_cards(self, idx, cards, run):
        """Atomic mode: the card writer's cloze cards for one slide, filed like hand-written
        ones (nothing reaches Anki until Create All Cards)."""
        import uuid as _uuid
        add = [{"uid": _uuid.uuid4().hex, "nid": None, "text": c["text"], "extra": "",
                "slide": True, "page": idx} for c in cards if c.get("text")]
        if add:
            self._cloze_cards.setdefault(idx, []).extend(add)
            run.made += len(add)
            self._refresh_cloze_column()
            self._refresh_count()
            self._rail.refresh_row(idx)

    def _auto_page_boxes(self, idx, units, image, scale, plan, run):
        old = self._boxes.get(idx, []) if idx != self._page_index else self._canvas.get_boxes()
        keep = [] if self._auto_replace else old
        next_group = max([b["group"] for b in keep if b.get("group") is not None] + [0]) + 1
        from . import vision
        boxes, made, dropped = vision.boxes_for_page(plan, units, image, scale, next_group)
        run.made += made
        run.dropped += dropped
        if idx == self._page_index:
            remove = {b["id"] for b in old} if self._auto_replace else set()
            self._canvas.replace_boxes(remove, boxes)
            self._boxes[idx] = self._canvas.get_boxes()
        elif boxes or self._auto_replace:
            self._boxes[idx] = keep + boxes
        self._refresh_count()
        self._rail.refresh_row(idx)

    def _auto_finished(self, made, dropped, slides, complete):
        telemetry.track("auto_occlude_done", cards=made, dropped=dropped, slides=slides, complete=complete)
        run = self._auto_run
        self._auto_btn.setEnabled(bool(self._pages))
        self._auto_run = None
        if complete and run is not None and run.quiz and run.quiz_url:
            webbrowser.open(run.quiz_url)
            tooltip(f"Quiz ready: {made} questions, opened in your browser", parent=self)
            return
        # atomic cards first, then the slides they said are better tested by location go
        # straight through occlusion in the same sitting: no second trip to Auto-occlude
        if complete and run is not None and run.atomic and run.occlude:
            doc = self._doc_for_page(run.occlude[0])
            pages = [(i, doc["path"], i - doc["start"], (lambda i=i: self._page_image(i)))
                     for i in run.occlude if self._doc_for_page(i) is doc]
            s = dict(run.s, make="occlusion", _followup=True)
            self._auto_replace = False
            self._auto_btn.setEnabled(False)
            self._auto_run = auto_occlude.Run(self, pages, s, run.notes, self._auto_page_boxes,
                                              self._auto_finished, on_page_cards=self._auto_page_cards)
            QTimer.singleShot(0, self._auto_run.start)
            return
        self._update_controls()
        self._refresh_count()
        if slides:
            note = f", {dropped} skipped" if dropped else ""
            tooltip(f"Auto-occlude made {made} card{'s' if made != 1 else ''} "
                    f"on {slides} slide{'s' if slides != 1 else ''}{note}", parent=self)

    def _open_cloze(self):
        """Ctrl+Shift+V — show or hide the composer panel."""
        if not self._pages:
            return
        showing = not self._cloze.isVisible()
        self._sync_panel_height()
        self._cloze_side.setVisible(showing)
        self._sync_tools()
        if showing:
            self._cloze.refresh_slide()
            self._cloze.focus_editor()
        else:
            self._canvas.setFocus()
        self._refit()

    def _refresh_cloze_column(self):
        """Show this slide's cards. The column appears and disappears with
        them, so the slide's room changes — hence the re-fit."""
        self._cloze_column.set_cards(self._cloze_cards.get(self._page_index, []))
        self._refit()

    def _find_cloze(self, uid: Optional[str]):
        """(page, index) of a stored card, or (None, None)."""
        for page, cards in self._cloze_cards.items():
            for i, card in enumerate(cards):
                if card.get("uid") == uid:
                    return page, i
        return None, None

    def _on_cloze_created(self, card: dict):
        """File the new card against its slide and send it to the column."""
        page = card.get("page", self._page_index)
        entry = dict(card)
        self._cloze_cards.setdefault(page, []).append(entry)
        self._refresh_count()
        if page != self._page_index:
            return
        chip = self._cloze_column.add_card(entry)
        self._refit()               # the column may have just appeared
        start = self._cloze.source_rect()

        def fly():
            self._cloze_column.scroll_to_end()
            self._fly = fly_to_chip(self, card["text"], start, chip)

        QTimer.singleShot(0, fly)   # let the column lay the chip out first

    def _on_cloze_activated(self, entry: dict):
        """A chip was clicked — put that card back in the panel to edit."""
        if not self._cloze.isVisible():
            self._open_cloze()
        self._cloze.load_card(entry)

    def _on_cloze_updated(self, card: dict):
        page, i = self._find_cloze(card.get("uid"))
        if page is None:
            return
        entry = dict(card)
        entry["page"] = page
        self._cloze_cards[page][i] = entry
        self._refresh_cloze_column()
        self._refresh_count()

    def _on_cloze_delete(self, entry: dict):
        n = cloze_card_count(entry.get("text", ""))
        made = bool(entry.get("nid"))
        if not askUser(
            f"Remove this cloze card ({n} card{'s' if n != 1 else ''})?"
            + ("\n\nIts note is deleted the next time you create cards."
               if made else ""),
            title="PDF Occlusion",
        ):
            return
        page, i = self._find_cloze(entry.get("uid"))
        if page is not None:
            del self._cloze_cards[page][i]
            if not self._cloze_cards[page]:
                del self._cloze_cards[page]
        if made:
            self._cloze_stale.append(int(entry["nid"]))
        if self._cloze.editing_uid() == entry.get("uid"):
            self._cloze.reset_editing()
        self._refresh_cloze_column()
        self._refresh_count()

    def cloze_deck_name(self) -> str:
        return self._deck_combo.currentText().strip()

    def cloze_slide(self, page: Optional[int] = None) -> Optional[dict]:
        """Which slide a card belongs to — the one on screen unless asked
        for another (a card being edited keeps its own)."""
        if not self._pages:
            return None
        idx = self._page_index if page is None else page
        if not 0 <= idx < len(self._pages):
            idx = self._page_index
        return {"page": idx, "label": self._slide_label_for(idx)}

    def cloze_slide_text(self, page: Optional[int] = None) -> str:
        """The text printed on a slide, for the composer's Slide Text button.

        Empty string when the slide has none to give (a scanned image, or a
        PDF pdfium cannot read text out of) — the composer says so."""
        if not self._pages:
            return ""
        idx = self._page_index if page is None else page
        if not 0 <= idx < len(self._pages):
            idx = self._page_index
        doc = self._doc_for_page(idx)
        if not doc:
            return ""
        try:
            return get_page_text(doc["path"], idx - doc["start"])
        except Exception:
            return ""

    def _slide_label_for(self, idx: int) -> str:
        doc = self._doc_for_page(idx)
        local = idx - doc["start"] if doc else idx
        total = doc["count"] if doc else len(self._pages)
        return f"Slide {local + 1}/{total}"

    # -------------------------------------------------------- card creation --

    def _create_cards(self):
        if not self._pages:
            return

        self._save_current_boxes()

        per_doc = []
        for doc in self._docs:
            start, count = doc["start"], doc["count"]
            to_create = [
                (i - start, self._page_image(i), self._boxes[i])
                for i in range(start, start + count)
                if self._boxes.get(i)
            ]
            # Slides whose boxes were all removed but still have cards go in
            # empty, so their cards come up for deletion like any other
            # removed box. Left out, they would stay on forever.
            to_create += [
                (local, self._pages[start + local], [])
                for local in sorted(_pages_with_notes(doc.get("note_map")))
                if 0 <= local < count and not self._boxes.get(start + local)
            ]
            cloze = []
            for i in range(start, start + count):
                for card in self._cloze_cards.get(i, []):
                    entry = dict(card)
                    entry["page"] = i - start
                    cloze.append(entry)
            if to_create or cloze:
                per_doc.append((doc, to_create, cloze))

        if not per_doc:
            showWarning(
                "Nothing to create yet.\n\n"
                "Draw a box on a slide, or write a cloze card "
                "(Ctrl+Shift+V)."
            )
            return

        mode = self._default_mode()

        deck_name = self._deck_combo.currentText().strip()
        deck_id = mw.col.decks.id(deck_name) if deck_name else mw.col.decks.selected()

        note_type_name = _cfg("note_type_name", "PDF Occlusion")
        mask_color = tuple(_cfg("mask_color", [120, 120, 120]))
        mask_opacity = int(_cfg("mask_opacity", 255))
        highlight_color = tuple(_cfg("highlight_color", [131, 110, 170]))

        # Everything that can ask a question happens before the progress
        # dialog opens: a prompt raised under it (e.g. Anki's "this needs a
        # full sync" when the note type gains fields) sits hidden behind the
        # modal progress window and the whole thing looks frozen.

        # Only when there are boxes: a cloze-only session shouldn't plant
        # the occlusion note type in a collection that has never used it.
        try:
            note_type = (ensure_note_type(mw.col, note_type_name)
                         if any(tc for _, tc, _c in per_doc) else None)
        except NoteTypeConflict as e:
            showWarning(str(e), parent=self, title="PDF Occlusion")
            return

        cloze_type = cloze_note_type(mw.col)
        if cloze_type is None and any(cloze for _, _, cloze in per_doc):
            showWarning(
                "No cloze note type found in this collection, so the cloze "
                "cards were skipped.\n\nAdd Anki's stock “Cloze” note type "
                "(Tools → Manage Note Types → Add) and create again."
            )

        # With pdf_hosting set, the cards get each PDF's public URL instead of
        # its path, so the Notes / Slides buttons open on a phone too. A PDF
        # that can't be copied keeps its path — the cards still open it on
        # this computer — and is reported with the rest of the summary.
        hosting = pdf_hosting.settings(_cfg("pdf_hosting", None))
        links: dict = {}            # location in the session -> on the cards
        unpublished: list = []
        if hosting:
            for doc, to_create, _cloze in per_doc:
                for loc in (doc["path"], doc.get("notes_pdf", "")):
                    if not to_create or not loc or loc in links:
                        continue
                    try:
                        links[loc] = pdf_hosting.publish(loc, hosting)
                    except OSError as e:
                        links[loc] = loc
                        unpublished.append(
                            f"{os.path.basename(loc)}: {e.strerror or e}")

        grand_total = sum(len(tc) for _, tc, _c in per_doc)
        progress = QProgressDialog("Creating Cards", "Cancel", 0, grand_total,
                                   self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)

        created = updated = unchanged = cloze_made = 0
        stale_nids: list[int] = list(self._cloze_stale)
        canceled = False
        error = ""
        try:
            created, updated, unchanged, cloze_made, canceled = (
                self._create_notes(per_doc, progress, deck_id, note_type,
                                   cloze_type, links, mode, mask_color,
                                   mask_opacity, highlight_color, stale_nids))
        except Exception:
            error = traceback.format_exc()
        finally:
            # without this an error left the modal progress window up for good
            progress.close()

        # The records now know their note ids. Get that to disk before
        # anything else can go wrong — losing it would mean creating every
        # one of these notes a second time on the next run.
        self._persist_sessions()

        if error:
            telemetry.track("error", where="create_cards", message=error.strip().splitlines()[-1][:200])
            log = _log_error(error)
            mw.reset()
            showWarning(
                "Creating cards stopped with an error. Cards made before it "
                "are kept.\n\n"
                f"{error.strip().splitlines()[-1]}\n\n"
                f"The full report is in {log}. Please include it if you "
                "report this.",
                parent=self, title="PDF Occlusion",
            )
            return

        telemetry.track("cards_created", cards=created + updated, created=created, updated=updated,
                        unchanged=unchanged, cloze=cloze_made, canceled=canceled,
                        published=bool(hosting), stale=len(stale_nids))
        deleted = 0
        if stale_nids and not canceled and askUser(
            f"Delete {len(stale_nids)} note(s) whose boxes or cloze cards "
            "were removed?",
            parent=self, title="PDF Occlusion",
        ):
            mw.col.remove_notes(stale_nids)
            deleted = len(stale_nids)
        if not canceled:
            self._cloze_stale = []

        mw.col.reset()
        mw.reset()
        self._persist_sessions()

        bits = []
        if created:
            bits.append(f"{created} created")
        if updated:
            bits.append(f"{updated} updated")
        if deleted:
            bits.append(f"{deleted} deleted")
        if not bits:
            msg = (f"All {unchanged} card{'s' if unchanged != 1 else ''} "
                   "already up to date.")
        else:
            if unchanged:
                bits.append(f"{unchanged} unchanged")
            msg = "Cards: " + ", ".join(bits) + "."
        if canceled:
            msg = ("Stopped early. Create cards again to finish the "
                   "rest.\n\n" + msg)
        if cloze_made:
            msg += f"\n\n{cloze_made} of them cloze."
        if unpublished:
            msg += ("\n\nCouldn't publish these, so their buttons only work "
                    "on this computer:\n" + "\n".join(unpublished))

        # Started before the summary so it runs while that's being read.
        if hosting and hosting["git_push"]:
            _push_published(hosting, [u for u in links.values()
                                      if pdf_hosting.is_url(u)])
        showInfo(msg, parent=self)
        if _cfg("close_after_creating", True) and not canceled:
            self.accept()

    def _create_notes(self, per_doc, progress, deck_id, note_type, cloze_type,
                      links, mode, mask_color, mask_opacity, highlight_color,
                      stale_nids):
        """The writing half of _create_cards. Appends to stale_nids and
        returns (created, updated, unchanged, cloze_made, canceled)."""
        created = updated = unchanged = cloze_made = 0
        canceled = False
        offset = 0
        for doc, to_create, cloze in per_doc:
            if canceled:
                break
            start, count = doc["start"], doc["count"]
            local_modes = {
                str(i - start): m for i, m in self._page_modes.items()
                if start <= i < start + count
            }

            def on_progress(done: int, total: int, _off=offset):
                nonlocal canceled
                mw.app.processEvents()
                canceled = canceled or progress.wasCanceled()
                if not canceled:
                    progress.setValue(_off + done)
                return not canceled

            result = {"created": 0, "updated": 0, "unchanged": 0,
                      "note_map": doc.get("note_map", {}),
                      "image_map": doc.get("image_map", {}),
                      "stale_nids": []}
            if to_create:
                try:
                    result = create_occlusion_notes(
                        mw.col, deck_id, note_type, to_create,
                        mask_color=mask_color,
                        mask_opacity=mask_opacity,
                        highlight_color=highlight_color,
                        lecture_name=doc["lecture"],
                        total_slides=count,
                        slides_pdf=links.get(doc["path"], doc["path"]),
                        notes_pdf=links.get(doc.get("notes_pdf", ""),
                                            doc.get("notes_pdf", "")),
                        default_mode=mode,
                        page_modes=local_modes,
                        note_map=doc.get("note_map"),
                        image_map=doc.get("image_map"),
                        on_progress=on_progress,
                    )
                except Exception as e:
                    if hasattr(e, "partial_maps"):
                        doc["note_map"], doc["image_map"] = e.partial_maps
                    raise
            doc["note_map"] = result["note_map"]
            doc["image_map"] = result["image_map"]
            created += result["created"]
            updated += result["updated"]
            unchanged += result["unchanged"]
            stale_nids += result["stale_nids"]
            offset += len(to_create)

            if cloze and cloze_type is not None and not canceled:
                def caption_for(local, _doc=doc, _count=count):
                    label = f"Slide {local + 1}/{_count}"
                    lecture = (_doc.get("lecture") or "").strip()
                    return f"{lecture} · {label}" if lecture else label

                cres = create_cloze_notes(
                    mw.col, deck_id, cloze_type, cloze,
                    page_images={c["page"]: self._page_image(start + c["page"])
                                 for c in cloze},
                    image_map=doc.get("image_map"),
                    caption_for=caption_for,
                )
                doc["image_map"] = cres["image_map"]
                # hand the new note ids back to the records they came from
                by_uid = {c["uid"]: c["nid"] for c in cres["cards"]
                          if c.get("uid")}
                for i in range(start, start + count):
                    for card in self._cloze_cards.get(i, []):
                        uid = card.get("uid")
                        if uid and uid in by_uid:
                            card["nid"] = by_uid[uid]
                created += cres["created"]
                updated += cres["updated"]
                unchanged += cres["unchanged"]
                cloze_made += cres["created"] + cres["updated"]

        return created, updated, unchanged, cloze_made, canceled
