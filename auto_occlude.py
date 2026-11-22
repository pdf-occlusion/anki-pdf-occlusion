"""Auto-occlude: a first draft of a lecture's boxes, placed by the server.

The add-on does the slide work here: it reads each slide's words (the PDF's
text layer, plus text recognition on the picture for figure labels), sends
the slide images and their words to the PDF Occlusion server in small
batches, and turns the plan that comes back into ordinary boxes on the
canvas. Nothing is saved until the student presses Create All Cards.

The server holds the model key and counts lecture credits; this file only
ever holds the student's own sign-in key, in user_files/account.json.
"""
import base64
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import webbrowser
from typing import Optional

from aqt import mw
from aqt.qt import (
    QApplication, QBuffer, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QGridLayout, QHBoxLayout, QIODevice, QImage, QLabel,
    QButtonGroup, QPlainTextEdit, QProgressDialog, QPushButton, QTimer, QVBoxLayout,
    QWidget, Qt,
)
from aqt.utils import showWarning, tooltip

from . import ocr
from . import telemetry
from .version import VERSION

DEFAULT_SERVER = "https://pdf-occlusion-api.pdf-occlusion.workers.dev"
SITE = "https://pdf-occlusion.github.io"
MAX_PIXELS = 1_150_000
_ACCOUNT = os.path.join(os.path.dirname(__file__), "user_files", "account.json")

# The few choices that apply to any document; the server works out the rest
# (what kind of document it is, what matters on each page) by reading it.
# (setting, label, [(value, label), ...]); the first choice is the default
DENSITY = ("density", "Cards", [
    ("fewer", "Fewer"),
    ("balanced", "Balanced"),
    ("more", "More"),
])
MORE = [
    ("labels", "Figure Labels", [
        ("auto", "Decide for Each Document"),
        ("key", "Only the Ones That Matter"),
        ("all", "Every Label"),
    ]),
    ("text", "Bullet Text", [
        ("key_phrase", "Hide the Key Phrase"),
        ("whole_point", "Hide the Whole Point"),
    ]),
    ("tables", "Tables", [
        ("per_cell", "One Card per Cell"),
        ("per_row", "One Card per Row"),
    ]),
]
CHOICES = MORE
SCOPES = [
    ("empty", "Only Slides I Haven't Boxed"),
    ("current", "Only This Slide"),
    ("all", "All Slides (Clears My Boxes)"),
]
DEFAULTS = {
    "density": "balanced",
    **{key: opts[0][0] for key, _, opts in MORE},
    "skip_admin": True, "custom": "", "use_notes": True, "scope": "empty",
    "make": "occlusion", "quiz_style": "step1", "quiz_count": 20, "card_style": "complete",
    "show_more": False,
}
# what the server gets with every batch
SENT = ["density", "labels", "text", "tables", "skip_admin", "custom"]


# ------------------------------------------------------------------ config

def _cfg() -> dict:
    return mw.addonManager.getConfig(__name__.split(".")[0]) or {}


def settings() -> dict:
    s = dict(DEFAULTS)
    saved = _cfg().get("auto_occlude") or {}
    if "skip_admin" not in saved:
        # saved by a version with the old, longer list of choices: keep only
        # what still means the same thing
        saved = {k: v for k, v in saved.items() if k in ("custom", "use_notes", "scope")}
    s.update({k: v for k, v in saved.items() if k in DEFAULTS})
    return s


def save_settings(s: dict) -> None:
    pkg = __name__.split(".")[0]
    cfg = mw.addonManager.getConfig(pkg) or {}
    cfg["auto_occlude"] = {k: s[k] for k in DEFAULTS}
    mw.addonManager.writeConfig(pkg, cfg)


def server() -> str:
    return (_cfg().get("auto_occlude_server") or DEFAULT_SERVER).rstrip("/")


def account() -> dict:
    try:
        with open(_ACCOUNT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_account(a: dict) -> None:
    os.makedirs(os.path.dirname(_ACCOUNT), exist_ok=True)
    tmp = _ACCOUNT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(a, f)
    os.replace(tmp, _ACCOUNT)


def sign_out() -> None:
    try:
        os.remove(_ACCOUNT)
    except OSError:
        pass


# ------------------------------------------------------------------ server

class ServerError(Exception):
    def __init__(self, status: int, message: str, data: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


@functools.lru_cache(maxsize=1)
def device_id() -> str:
    """A one-way hash of this computer's hardware id. The server counts free
    pages per computer with it; the id itself never leaves this machine."""
    raw = ""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r'"IOPlatformUUID" = "([^"]+)"', out)
            raw = m.group(1) if m else ""
        elif sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                raw = str(winreg.QueryValueEx(k, "MachineGuid")[0])
        else:
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    with open(p) as f:
                        raw = f.read().strip()
                except OSError:
                    continue
                if raw:
                    break
    except Exception:       # noqa: BLE001 - fall back to the network card below
        raw = ""
    if not raw:
        raw = f"node:{uuid.getnode():012x}"
    return hashlib.sha256(f"pdf-occlusion-device:{raw}".encode()).hexdigest()


def call(path: str, body: Optional[dict] = None, token: Optional[str] = None,
         timeout: float = 120.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server() + path, data=data, method="POST" if data is not None else "GET")
    req.add_header("content-type", "application/json")
    req.add_header("user-agent", "pdf-occlusion-addon")
    req.add_header("x-device", device_id())
    req.add_header("x-addon-version", VERSION)
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        raise ServerError(e.code, payload.get("error") or f"Server error {e.code}", payload)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ServerError(0, f"Could not reach the PDF Occlusion server ({e}).")


def in_background(fn, done):
    """Run fn() off the main thread; done(result, error) back on it."""
    def run():
        try:
            out, err = fn(), None
        except Exception as e:      # noqa: BLE001 - handed to the caller
            out, err = None, e
        mw.taskman.run_on_main(lambda: done(out, err))
    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------ slide words

def _page_words(path: str, page_index: int, scale: float) -> list:
    """The PDF's own words on one page: [(text, x0, y0, x1, y1)] in image px."""
    from .pdf_renderer import _import_pdfium
    pdfium = _import_pdfium()
    words, doc = [], pdfium.PdfDocument(path)
    try:
        page = doc[page_index]
        _, page_h = page.get_size()
        tp = page.get_textpage()
        try:
            text = tp.get_text_range()
            cur = None
            for i in range(min(tp.count_chars(), len(text))):
                ch = text[i]
                if ch.isspace():
                    if cur:
                        words.append(cur)
                        cur = None
                    continue
                try:
                    left, bottom, right, top = tp.get_charbox(i)
                except Exception:
                    continue
                if right <= left or top <= bottom:
                    continue
                if cur is None:
                    cur = [ch, left, bottom, right, top]
                else:
                    cur = [cur[0] + ch, min(cur[1], left), min(cur[2], bottom),
                           max(cur[3], right), max(cur[4], top)]
            if cur:
                words.append(cur)
        finally:
            tp.close()
            page.close()
    finally:
        doc.close()
    return [(t, l * scale, (page_h - tp_) * scale, r * scale, (page_h - b) * scale)
            for t, l, b, r, tp_ in words if r - l > 0.5 and tp_ - b > 0.5]


def _group_lines(words: list) -> list:
    """Words in reading order -> lines: a word joins the previous one's line
    when its middle falls inside that word's height and it starts to its right."""
    lines = []
    for w in words:
        mid = (w[2] + w[4]) / 2
        last = lines[-1][-1] if lines else None
        if last and last[2] <= mid <= last[4] and w[1] >= last[1] and w[1] - last[3] < 3 * (last[4] - last[2]):
            lines[-1].append(w)
        else:
            lines.append([w])
    return lines


def read_picture_text(image: QImage) -> list:
    """Text recognition for one slide, safe to run on a worker thread (the recogniser
    is a separate process; the QImage is the caller's own copy). [] if unavailable."""
    if not ocr.available():
        return []
    try:
        return ocr.recognize(image)
    except ocr.OcrError:
        return []


def slide_lines(path: str, page_index: int, scale: float, image: QImage, read=None) -> list:
    """Every line of text on a slide, as [{"id", "src", "words": [...]}].
    `read` is the slide's text recognition when it was already done elsewhere."""
    found = _group_lines(_page_words(path, page_index, scale))
    out, wid = [], 0
    for ln in found:
        out.append({"id": f"L{len(out)}", "src": "text", "words": [
            {"t": t, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0} for t, x0, y0, x1, y1 in ln]})
    if read is not None or ocr.available():
        known = [w for ln in out for w in ln["words"]]
        if read is None:
            read = read_picture_text(image)
        extra = []
        for x, y, w, h, t in read:
            cx, cy = x + w / 2, y + h / 2
            if any(k["x"] - 2 <= cx <= k["x"] + k["w"] + 2 and k["y"] - 2 <= cy <= k["y"] + k["h"] + 2
                   for k in known):
                continue    # the text layer already has this word
            extra.append((t, x, y, x + w, y + h))
        for ln in _group_lines(sorted(extra, key=lambda r: (round(r[2] / 10), r[1]))):
            out.append({"id": f"L{len(out)}", "src": "ocr", "words": [
                {"t": t, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0} for t, x0, y0, x1, y1 in ln]})
    for ln in out:
        for w in ln["words"]:
            w["id"] = f"w{wid}"
            wid += 1
    return out


def slide_payload(n: int, image: QImage, lines: list) -> dict:
    img = image
    if img.width() * img.height() > MAX_PIXELS:
        f = (MAX_PIXELS / (img.width() * img.height())) ** 0.5
        img = img.scaled(int(img.width() * f), int(img.height() * f),
                         Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.convertToFormat(QImage.Format.Format_RGB32).save(buf, "JPEG", 85)
    return {
        "n": n, "w": image.width(), "h": image.height(),
        "media_type": "image/jpeg",
        "image": base64.b64encode(bytes(buf.data())).decode(),
        "lines": [{"id": ln["id"], "x": ln["words"][0]["x"], "y": ln["words"][0]["y"],
                   "ocr": ln["src"] == "ocr", "text": " ".join(w["t"] for w in ln["words"])}
                  for ln in lines],
    }


def _page_text(path: str, page_index: int, scale: float) -> str:
    try:
        return "\n".join(" ".join(w[0] for w in ln) for ln in _group_lines(_page_words(path, page_index, scale)))
    except Exception:       # noqa: BLE001 - a page that can't be read is sent empty
        return ""


def _thumb(image: QImage, width: int = 900) -> str:
    img = image.scaledToWidth(min(width, image.width()), Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.convertToFormat(QImage.Format.Format_RGB32).save(buf, "JPEG", 70)
    return base64.b64encode(bytes(buf.data())).decode()


def atomic_payload(n: int, image: QImage, lines: list) -> dict:
    """One slide for the atomic card writer: the plain slide and its text."""
    img = image
    if img.width() * img.height() > MAX_PIXELS:
        f = (MAX_PIXELS / (img.width() * img.height())) ** 0.5
        img = img.scaled(int(img.width() * f), int(img.height() * f),
                         Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.convertToFormat(QImage.Format.Format_RGB32).save(buf, "JPEG", 85)
    return {"n": n, "media_type": "image/jpeg", "image": base64.b64encode(bytes(buf.data())).decode(),
            "text": "\n".join(" ".join(w["t"] for w in ln["words"]) for ln in lines), "_scale": 1.0}


def document_payload(dlg, doc: dict, prepared: list, notes: str) -> dict:
    """The whole PDF, for the server's first read: every page's text (the
    text layer; text recognition only where it already ran, on the slides
    being occluded), a few page images, and the notes."""
    start, count = doc["start"], doc["count"]
    read = {gidx - start: "\n".join(" ".join(w["t"] for w in ln["words"]) for ln in lines)
            for gidx, lines in ((p[0], p[3]) for p in prepared)}
    pages = []
    for local in range(min(count, 400)):
        text = _page_text(doc["path"], local, dlg._render_scale) or read.get(local, "")
        pages.append({"n": local + 1, "text": text[:3000]})
    step = max(1, count // 6)
    thumbs = [{"n": i + 1, "media_type": "image/jpeg", "image": _thumb(dlg._page_image(start + i))}
              for i in range(0, count, step)][:6]
    return {"pages": pages, "thumbs": thumbs, "notes": (notes or "")[:12000]}


# ------------------------------------------------------------------ plan -> boxes
# The same targets ankify's plans use: a phrase, whole lines, a phrase as one
# block, lines one box each, or a hand-placed rect.

def _norm(tok: str) -> str:
    tok = unicodedata.normalize("NFKC", tok).lower()
    return re.sub(r"^\W+|\W+$", "", tok)


def _union(rects):
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return [x0, y0, x1 - x0, y1 - y0]


def _stacks(rects):
    """A range of lines -> one box per run of lines that sit on top of each
    other. Labels read off a figure are numbered row by row, so "L20-L23"
    can be two labels on opposite sides of the drawing; one box around all
    of them would cover the figure between."""
    out = []
    for r in sorted(rects, key=lambda r: r[1]):
        for c in out:
            gap = r[1] - (c[1] + c[3])
            overlap = min(r[0] + r[2], c[0] + c[2]) - max(r[0], c[0])
            if gap < 1.2 * r[3] and overlap > 0:
                c[:] = _union([c, r])
                break
        else:
            out.append(list(r))
    return out


def _pad(r, W, H, ocr=False):
    pad = max(3.0, 0.15 * r[3]) if r[3] < 120 else 6.0
    # text recognition places words less exactly than a PDF's own text
    # layer, mostly at their ends, so its boxes get more room sideways
    side = max(pad, 0.25 * min(r[3], 60)) if ocr else pad
    x0, y0 = max(0, r[0] - side), max(0, r[1] - pad)
    x1, y1 = min(W, r[0] + r[2] + side), min(H, r[1] + r[3] + pad)
    return [int(round(x0)), int(round(y0)), int(round(x1 - x0)), int(round(y1 - y0))]


class _Page:
    def __init__(self, lines, W, H):
        self.W, self.H = W, H
        self.lines = {ln["id"]: ln for ln in lines}
        self.seq = [(w, ln["id"]) for ln in lines for w in ln["words"] if _norm(w["t"])]
        self.used = set()

    def _line_ids(self, spec):
        ids = []
        for part in spec.split(","):
            m = re.fullmatch(r"L(\d+)(?:\s*-\s*L?(\d+))?", part.strip())
            if not m:
                raise ValueError(f"bad line range {spec!r}")
            ids += [f"L{k}" for k in range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1)]
        missing = [k for k in ids if k not in self.lines]
        if missing:
            raise ValueError(f"no line {missing[0]}")
        return ids

    def lines_rects(self, spec, each=False):
        rects = [_union([[w["x"], w["y"], w["w"], w["h"]] for w in self.lines[i]["words"]])
                 for i in self._line_ids(spec)]
        return rects if each else _stacks(rects)

    def phrase(self, text, line=None, occurrence=None):
        toks = [t for t in (_norm(t) for t in str(text).split()) if t]
        if not toks:
            raise ValueError("empty phrase")
        hits = [s for s in range(len(self.seq) - len(toks) + 1)
                if all(_norm(self.seq[s + k][0]["t"]) == toks[k] for k in range(len(toks)))
                and (line is None or self.seq[s][1] == line)]
        if not hits:
            raise ValueError(f"not on the slide: {text!r}")
        if occurrence and int(occurrence) <= len(hits):
            start = hits[int(occurrence) - 1]
        else:
            start = next((h for h in hits if h not in self.used), hits[0])
        self.used.update(range(start, start + len(toks)))
        by_line = {}
        for w, lid in self.seq[start:start + len(toks)]:
            by_line.setdefault(lid, []).append([w["x"], w["y"], w["w"], w["h"]])
        return [_union(r) for r in by_line.values()]

    def uses_ocr(self, t):
        """Does a target land on recognised text (as opposed to the text layer)?"""
        try:
            if isinstance(t, str) and re.fullmatch(r"L\d+(\s*[-,]\s*L?\d+)*", t.strip()):
                return any(self.lines[i].get("src") == "ocr" for i in self._line_ids(t))
            if isinstance(t, dict) and "lines" in t:
                return any(self.lines[i].get("src") == "ocr" for i in self._line_ids(t["lines"]))
        except (ValueError, KeyError):
            return False
        return any(ln.get("src") == "ocr" for ln in self.lines.values()) and not any(
            ln.get("src") == "text" for ln in self.lines.values())

    def resolve(self, t):
        """One target -> (rects, already padded)."""
        if isinstance(t, str):
            if re.fullmatch(r"L\d+(\s*[-,]\s*L?\d+)*", t.strip()):
                return self.lines_rects(t), False
            return self.phrase(t), False
        if isinstance(t, dict):
            if "rect" in t:
                x, y, w, h = (float(v) for v in t["rect"])
                return [[int(x), int(y), int(w), int(h)]], True
            if "lines" in t:
                return self.lines_rects(t["lines"], bool(t.get("each"))), False
            if "text" in t:
                rects = self.phrase(t["text"], t.get("line"), t.get("occurrence"))
                return ([_union(rects)] if t.get("block") else rects), False
        raise ValueError(f"unreadable target {t!r}")


def _whole_lines(rects, lines):
    """Text lines (not figure labels) that these rects cover completely."""
    full = []
    for i, ln in enumerate(lines):
        if ln.get("src") == "ocr" or not ln["words"]:
            continue
        inside = [any(r[0] <= w["x"] + w["w"] / 2 <= r[0] + r[2] and r[1] <= w["y"] + w["h"] / 2 <= r[1] + r[3]
                      for r in rects) for w in ln["words"]]
        if all(inside):
            full.append(i)
    return full


def _merge_bare_siblings(cards, lines):
    """With every box covered at once, two cards that each hide a whole line of
    a list look identical. Neighbouring cards like that become one card, which
    asks for the list under its heading."""
    bare = []
    for k, (rects, _) in enumerate(cards):
        full = _whole_lines(rects, lines)
        if full and len(full) <= 2:
            bare.append((k, full))
    groups, cur = [], []
    for k, full in bare:
        if cur and full[0] - cur[-1][1][-1] <= 1 and abs(lines[full[0]]["words"][0]["x"] - lines[cur[-1][1][0]]["words"][0]["x"]) < 60:
            cur.append((k, full))
        else:
            if len(cur) > 1:
                groups.append(cur)
            cur = [(k, full)]
    if len(cur) > 1:
        groups.append(cur)
    drop = set()
    for g in groups:
        head = g[0][0]
        for k, _ in g[1:]:
            cards[head][0].extend(cards[k][0])
            drop.add(k)
    return [c for k, c in enumerate(cards) if k not in drop]


def _separate(cards):
    """Padding can make the boxes of two neighbouring labels overlap, and an
    overlap clips the edge of the other card's words. Split each such overlap
    down the middle, across whichever axis it is thinner on."""
    boxes = [(k, r) for k, (rects, _q) in enumerate(cards) for r in rects]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (ki, a), (kj, b) = boxes[i], boxes[j]
            if ki == kj:
                continue
            ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
            oy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
            if ox <= 0 or oy <= 0:
                continue
            # only padding-sized overlaps: a real overlap is a choice
            if ox > 0.5 * min(a[2], b[2]) and oy > 0.5 * min(a[3], b[3]):
                continue
            if oy <= ox:
                top, bot = (a, b) if a[1] < b[1] else (b, a)
                mid = (top[1] + top[3] + bot[1]) / 2
                cut = top[1] + top[3] - mid
                top[3] = int(round(top[3] - cut))
                bot[3] = int(round(bot[3] - (mid - bot[1])))
                bot[1] = int(round(mid))
            else:
                left, right = (a, b) if a[0] < b[0] else (b, a)
                mid = (left[0] + left[2] + right[0]) / 2
                cut = left[0] + left[2] - mid
                left[2] = int(round(left[2] - cut))
                right[2] = int(round(right[2] - (mid - right[0])))
                right[0] = int(round(mid))


def plan_to_boxes(cards: list, lines: list, W: int, H: int, next_group: int):
    """One slide's cards -> (boxes, cards made, cards dropped)."""
    page, resolved, dropped = _Page(lines, W, H), [], 0
    for card in cards or []:
        if isinstance(card, (str, list)) or not isinstance(card, dict) or "hide" not in card:
            card = {"hide": card}
        targets = card.get("hide")
        if isinstance(targets, (str, dict)):
            targets = [targets]
        rects, raw, ocr = [], [], False
        try:
            for t in targets or []:
                got, padded = page.resolve(t)
                if padded:
                    rects += got
                else:
                    raw += got
                    ocr = ocr or page.uses_ocr(t)
        except (ValueError, KeyError, TypeError):
            dropped += 1
            continue
        # the lines of one multi-line label, however they were named: one
        # box, not one per line
        rects += [_pad(r, W, H, ocr) for r in (_stacks(raw) if len(raw) > 1 else raw)]
        rects = [r for r in rects if r[2] > 2 and r[3] > 2]
        if not rects:
            dropped += 1
            continue
        resolved.append((rects, card.get("q", "")))
    resolved = _merge_bare_siblings(resolved, lines)
    _separate(resolved)
    out = []
    for rects, _q in resolved:
        grouped = len(rects) > 1
        guid = uuid.uuid4().hex if grouped else None
        for r in rects:
            out.append({"x": r[0], "y": r[1], "w": r[2], "h": r[3],
                        "group": next_group if grouped else None, "group_uid": guid,
                        "shape": "rect", "mode": None, "note": "", "id": uuid.uuid4().hex})
        next_group += grouped
    return out, len(resolved), dropped

# ------------------------------------------------------------------ sign in

class SignIn:
    """Link this Anki to an account: the site approves a code, we collect a key."""

    def __init__(self, parent, on_done):
        self.parent, self.on_done = parent, on_done
        self.timer, self.started = None, 0.0

    def start(self):
        telemetry.track("sign_in_start")

        def go():
            label = f"Anki on {os.uname().nodename}" if hasattr(os, "uname") else "Anki on Windows"
            return call("/v1/link/start", {"label": label[:80]})
        in_background(go, self._started)

    def _started(self, res, err):
        if err:
            telemetry.track("error", where="sign_in", message=str(err)[:200])
            showWarning(str(err), parent=self.parent, title="PDF Occlusion")
            return
        self.code, self.secret = res["code"], res["secret"]
        webbrowser.open(res["url"])
        self.box = QProgressDialog(
            f"Your browser opened a page showing code {self.code}.\n"
            "Sign in with Google there, then click “link this anki”.",
            "Cancel", 0, 0, self.parent)
        self.box.setWindowTitle("Sign In")
        # in front of the Auto-occlude window until signed in or cancelled
        self.box.setWindowModality(Qt.WindowModality.WindowModal)
        self.box.setMinimumDuration(0)
        self.box.canceled.connect(self._cancel)
        self.box.rejected.connect(self._cancel)  # Esc hides the box without canceled
        self.box.show()
        self.started = time.time()
        self.timer = QTimer(self.parent)
        self.timer.timeout.connect(self._poll)
        self.timer.start(2000)

    def _cancel(self):
        # close() also emits canceled, but by then a finished sign-in has stopped the timer
        if self.timer:
            telemetry.track("sign_in_cancel", seconds=round(time.time() - self.started))
        self._stop()

    def _stop(self):
        if self.timer:
            self.timer.stop()
            self.timer = None

    def _poll(self):
        if time.time() - self.started > 600:
            self._stop()
            self.box.close()
            showWarning("The sign-in code expired. Try again.", parent=self.parent, title="PDF Occlusion")
            return
        self.timer.stop()

        def go():
            return call("/v1/link/poll", {"code": self.code, "secret": self.secret}, timeout=20)

        def done(res, err):
            if not self.timer and self.box.wasCanceled():
                return
            if err or res.get("status") != "linked":
                if self.timer:
                    self.timer.start(2000)
                return
            self._stop()
            self.box.close()
            _save_account({"token": res["token"], "email": res.get("email")})
            telemetry.track("sign_in_done")
            tooltip(f"Signed in as {res.get('email') or 'your account'}", parent=self.parent)
            self.on_done()
        in_background(go, done)


# ------------------------------------------------------------------ the window

MAKES = [("occlusion", "Boxes"), ("atomic", "Cards"), ("quiz", "Quiz")]
# Cloze card styles: how deep the cards go. The text is added to the
# student's own instructions when the cards are written.
CARD_STYLES = [
    ("complete", "Complete", ""),
    ("essentials", "Essentials", "Only what exams test most; skip anything secondary."),
    ("scratch", "From Scratch", "I am new to this topic: start with what each term means and build up; plain words."),
]
# Quiz styles, named for the kind of question rather than one exam; the
# caption says which exams each one is for. Values are the server's.
QUIZ_STYLES = [
    ("step1", "Basic Science", "Cases that ask why something happens. Suits Step 1, COMLEX, the MCAT, dental, pharmacy and nursing science, and course exams."),
    ("shelf", "Clinical", "Cases that ask what to do next. Suits Step 2 CK, shelf exams, NCLEX, PANCE and other clinical exams."),
    ("recall", "Quick Recall", "Short questions on one fact each, for any course or exam."),
    ("custom", "Custom", "Your own style. Describe it in Instructions below."),
]
QUIZ_COUNT = (5, 40)       # the server's limits


def _preview(kind: str, size: int = 56, white: bool = False):
    """A small drawing of each choice: what it makes, or how its questions look.
    white: drawn in white for the purple start button."""
    from aqt.qt import QIcon, QPainter, QPixmap, QColor, QPen, QRectF, QPointF
    from aqt.theme import theme_manager
    night = theme_manager.night_mode
    ink = QColor(220, 220, 225) if night else QColor(58, 58, 60)
    soft = QColor(ink); soft.setAlpha(110)
    mask = QColor(124, 124, 128)
    accent = QColor(150, 128, 210) if night else QColor(110, 90, 168)
    if white:
        ink = QColor(255, 255, 255)
        soft = QColor(255, 255, 255, 170)
        mask = QColor(255, 255, 255, 150)
        accent = QColor(255, 255, 255)
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(ink, 1.6))
    p.setBrush(QColor(0, 0, 0, 0))
    s = size

    def lines(x, y, widths, gap=6, col=soft, w=1.6):
        p.setPen(QPen(col, w))
        for k, ww in enumerate(widths):
            p.drawLine(int(x), int(y + k * gap), int(x + ww), int(y + k * gap))

    if kind == "occlusion":        # a slide: a figure with its labels boxed
        p.drawRoundedRect(QRectF(5, 11, s - 10, s - 22), 3, 3)
        p.setPen(QPen(soft, 1.4)); p.drawEllipse(QRectF(11, 20, 15, 15))
        p.fillRect(QRectF(30, 18, 18, 5), mask)
        p.fillRect(QRectF(30, 26, 14, 5), accent)
        p.fillRect(QRectF(30, 34, 17, 5), mask)
    elif kind == "atomic":         # a flashcard: a sentence with one blank, a second card behind
        p.setPen(QPen(soft, 1.4))
        p.drawRoundedRect(QRectF(12, 9, s - 20, s - 24), 4, 4)
        p.setPen(QPen(ink, 1.6))
        p.setBrush(QColor(0, 0, 0, 0))
        p.drawRoundedRect(QRectF(6, 15, s - 20, s - 24), 4, 4)
        lines(12, 25, [s - 32])
        lines(12, 33, [6])
        p.setPen(QPen(accent, 1.4))
        p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 60))
        p.drawRoundedRect(QRectF(21, 29.5, 16, 7), 2, 2)
        p.setPen(QPen(accent, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for dx in (25.5, 29, 32.5):
            p.drawPoint(QPointF(dx, 33))
        p.setBrush(QColor(0, 0, 0, 0))
        lines(40, 33, [s - 54])
        lines(12, 41, [s - 38])
    elif kind == "quiz":           # a question with five choices, one picked
        lines(9, 11, [s - 18, s - 26], gap=5, col=ink)
        for k in range(4):
            y = 25 + k * 7.5
            p.setPen(QPen(ink, 1.2))
            p.setBrush(accent if k == 2 else QColor(0, 0, 0, 0))
            p.drawEllipse(QRectF(9, y - 2.5, 5, 5))
            p.setBrush(QColor(0, 0, 0, 0))
            lines(18, y, [s - 32 - (k % 2) * 6])
    elif kind == "step1":          # a vignette with a lab table, asking why
        lines(8, 10, [s - 16, s - 16, s - 22, s - 28])
        p.setPen(QPen(soft, 1.0))
        p.drawRect(QRectF(8, 33, s - 16, 11)); p.drawLine(int(s / 2), 33, int(s / 2), 44)
        lines(8, 50, [s - 30], col=accent, w=2.0)
    elif kind == "shelf":          # a vignette with vitals, asking what next
        lines(8, 10, [s - 16, s - 20, s - 16, s - 26])
        p.setPen(QPen(soft, 1.2))
        for k in range(3):
            p.drawRoundedRect(QRectF(8 + k * 14, 33, 11, 8), 2, 2)
        lines(8, 50, [s - 24], col=accent, w=2.0)
    else:                          # recall: one short line
        lines(8, 24, [s - 20])
        lines(8, 34, [s - 34], col=accent, w=2.0)
    p.end()
    return QIcon(pm)


def _credits_left(b: dict):
    """The one balance; a server from before credits says pages_left."""
    if not b:
        return None
    v = b.get("credits_left", b.get("pages_left"))
    return int(v) if v is not None else None


class _CreditMeter(QWidget):
    """What a run costs, drawn: the number of credits in an accent pill, a bar of
    the balance with this run's share filled, and what is left after."""

    def __init__(self, c: dict):
        super().__init__()
        self.c = c
        self.need, self.left, self.free = 0, None, None
        self.suffix = ""
        self.setMinimumHeight(30)
        self.setMinimumWidth(220)

    def set(self, need: int, left, free=None, suffix: str = ""):
        self.need, self.left, self.free, self.suffix = need, left, free, suffix
        self.update()

    def text(self) -> str:      # for tests and logs
        if self.free is not None:
            return f"{self.free} Free Credits"
        if self.left is None:
            return f"{self.need} credits"
        return f"{self.need} of {self.left} credits{self.suffix}"

    def paintEvent(self, _e):
        from aqt.qt import QPainter, QColor, QRectF, QFont, QFontMetrics, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self.c
        accent, sub, line = QColor(c["accent"]), QColor(c["sub"]), QColor(c["line"])
        red = QColor("#d93025")
        f = QFont(self.font())
        f.setPixelSize(12)
        f.setBold(True)
        cy = self.height() / 2
        if self.free is not None:          # signed out: what a new account gets
            p.setFont(f)
            p.setPen(accent)
            p.drawText(QRectF(0, 0, self.width(), self.height()),
                       int(Qt.AlignmentFlag.AlignVCenter), f"{self.free} Free Credits")
            p.end()
            return
        short = self.left is not None and self.need > self.left
        col = red if short else accent
        # the pill: how many this run uses
        label = f"{self.need:,} Credit{'s' if self.need != 1 else ''}"
        w = QFontMetrics(f).horizontalAdvance(label) + 18
        pill = QRectF(0, cy - 11, w, 22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(col.red(), col.green(), col.blue(), 38))
        p.drawRoundedRect(pill, 11, 11)
        p.setFont(f)
        p.setPen(col)
        p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), label)
        p.end()


def _go_icon(size: int = 20):
    """A plain white arrow: go."""
    from aqt.qt import QIcon, QPainter, QPixmap, QColor, QPen, QPointF
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(255, 255, 255), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    s = size
    p.drawLine(QPointF(s * .22, s * .5), QPointF(s * .76, s * .5))
    p.drawLine(QPointF(s * .52, s * .26), QPointF(s * .78, s * .5))
    p.drawLine(QPointF(s * .52, s * .74), QPointF(s * .78, s * .5))
    p.end()
    return QIcon(pm)


def _initial_icon(email: str, c: dict, size: int = 26, credits=None):
    from aqt.qt import QIcon, QPainter, QPixmap, QColor, QRectF, QFont
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(0, 0, 0, 0))
    p.setBrush(QColor(c["accent"]))
    p.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    f = QFont(); f.setPixelSize(12); f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(0, 0, size, size), int(Qt.AlignmentFlag.AlignCenter), (email or "?")[:1].upper())
    p.end()
    return QIcon(pm)


class AutoOccludeDialog(QDialog):
    """What to make from the lecture, and how. Returns the chosen settings via .chosen."""

    LABEL_W = 92

    def __init__(self, parent, has_notes: bool, slide_counts: dict, primary_qss: str):
        super().__init__(parent)
        from aqt.qt import QToolButton, QSize, QFrame, QSlider, QMenu
        from . import pdf_occlusion_dialog as D
        self._D = D
        self.c = D._colors()
        self.setWindowTitle("Generate")
        self.setObjectName("generate")
        self.setStyleSheet(D._window_qss(self.c) + self._qss(self.c))
        self.slide_counts = slide_counts
        self.chosen = None
        s = settings()
        root = QVBoxLayout(self)
        from aqt.qt import QLayout
        # the window always fits what the chosen output shows, no stretched gaps
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)

        # what to make: three tiles, each a picture of the result; the account sits apart, top right
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addSpacing(84)          # balances the account corner, so the tiles sit in the true centre
        head.addStretch(1)
        self.make_group = QButtonGroup(self)
        self.make_btns = {}
        for value, label in MAKES:
            b = self._tile(label, _preview(value), 128, 96, 52)
            b.setChecked(value == (s.get("make") or "occlusion"))
            self.make_group.addButton(b)
            self.make_btns[value] = b
            b.toggled.connect(lambda on, v=value: on and self._set_make(v))
            head.addWidget(b)
        head.addStretch(1)
        self.acct_btn = QToolButton()
        self.acct_btn.setObjectName("acct")
        self.acct_btn.setIconSize(QSize(26, 26))
        self.acct_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.acct_menu = QMenu(self)
        self.acct_menu.aboutToShow.connect(self._fill_account_menu)
        self.acct_btn.setMenu(self.acct_menu)
        self.signin_btn = QPushButton("Sign In")
        self.signin_btn.setObjectName("secondary")
        self.signin_btn.clicked.connect(self._sign)
        # the account: its initial, with the credit balance in a badge under it
        self.credit_badge = QLabel()
        self.credit_badge.setObjectName("creditBadge")
        self.credit_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        who = QWidget()
        wl = QVBoxLayout(who)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(3)
        wl.addWidget(self.acct_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        wl.addWidget(self.credit_badge, 0, Qt.AlignmentFlag.AlignHCenter)
        self._who = who
        corner = QWidget()
        corner.setFixedWidth(84)
        cl = QHBoxLayout(corner)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addStretch(1)
        cl.addWidget(who, 0, Qt.AlignmentFlag.AlignTop)
        cl.addWidget(self.signin_btn, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(corner, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        line = QFrame(); line.setObjectName("rule"); line.setFixedHeight(1)
        root.addWidget(line)

        # the settings, one aligned column of labels
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(12)
        self.grid.setColumnMinimumWidth(0, self.LABEL_W)
        self.grid.setColumnStretch(1, 1)
        self._rows = {}

        self.scope_seg, self.scope_btns = self._segmented([])
        self._add_row("scope", "Slides", self.scope_seg)

        self.density_seg, self.density_btns = self._segmented(DENSITY[2], s["density"])
        self._add_row("density", "Amount", self.density_seg)

        self.card_seg, self.card_btns = self._segmented(
            [(v, n) for v, n, _ in CARD_STYLES],
            s.get("card_style") if s.get("card_style") in [v for v, _, _ in CARD_STYLES] else "complete")
        self._add_row("card_style", "Style", self.card_seg)

        # quiz styles: the same segmented control as the card styles; the exams
        # each one suits are the note beside it
        self.quiz_seg, self.quiz_btns = self._segmented(
            [(v, n) for v, n, _ in QUIZ_STYLES],
            s.get("quiz_style") if s.get("quiz_style") in [v for v, _, _ in QUIZ_STYLES] else "step1",
            below=True)
        self._add_row("quiz_style", "Style", self.quiz_seg, top=True)

        count_row = QHBoxLayout()
        count_row.setContentsMargins(0, 0, 0, 0)
        from aqt.qt import QSpinBox
        self.quiz_count = QSpinBox()
        self.quiz_count.setObjectName("count")
        self.quiz_count.setRange(*QUIZ_COUNT)
        self.quiz_count.setValue(int(s.get("quiz_count") or 20))
        self.quiz_count.setFixedWidth(72)
        self.quiz_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_label = QLabel()
        self.count_label.setObjectName("note")
        self.quiz_count.valueChanged.connect(lambda _v: self._refresh())
        count_row.setSpacing(12)
        count_row.addWidget(self.quiz_count)
        count_row.addWidget(self.count_label, 1)
        cw = QWidget(); cw.setLayout(count_row)
        self._add_row("quiz_count", "Questions", cw)

        self.notes = QCheckBox("Use the Notes PDF")
        self.notes.setChecked(bool(s["use_notes"]) and has_notes)
        self._add_row("notes", "", self.notes)
        self._has_notes = has_notes

        self.custom = QPlainTextEdit()
        self.custom.setObjectName("instructions")
        self.custom.setPlainText(s["custom"])
        self.custom.setFixedHeight(58)
        self.custom.textChanged.connect(self._refresh)
        self.custom.setFixedWidth(474)
        self._add_row("custom", "Instructions", self.custom, top=True)
        root.addLayout(self.grid)

        # image occlusion's finer settings, folded away
        self.more_toggle = QPushButton()
        self.more_toggle.setObjectName("link")
        self.more_toggle.setFlat(True)
        self.more_toggle.clicked.connect(lambda: self._show_more(not self.more.isVisible()))
        self.more = QWidget()
        form = QGridLayout(self.more)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setColumnMinimumWidth(0, self.LABEL_W)
        form.setColumnStretch(1, 1)
        self.combos = {}
        for r, item in enumerate(MORE):
            form.addWidget(self._label(item[1]), r, 0)
            form.addWidget(self._combo(item, s), r, 1)
        self.skip_admin = QCheckBox("Skip Title, Objective and Reference Slides")
        self.skip_admin.setChecked(bool(s["skip_admin"]))
        form.addWidget(self.skip_admin, len(MORE), 1)
        mrow = QHBoxLayout()
        mrow.addSpacing(self.LABEL_W + 14)
        mrow.addWidget(self.more_toggle)
        mrow.addStretch(1)
        root.addLayout(mrow)
        root.addWidget(self.more)

        # what it uses, and go
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.usage = _CreditMeter(self.c)
        self.topup_btn = QPushButton("Buy Credits")
        self.topup_btn.setObjectName("secondary")
        self.topup_btn.clicked.connect(lambda: webbrowser.open(f"{SITE}/pricing/"))
        # the start button is a picture of what it makes, like Create Cards in the main window
        self.start_btn = QPushButton()
        self.start_btn.setStyleSheet(primary_qss + "QPushButton{padding:0;}")
        self.start_btn.setFixedSize(52, 36)
        self.start_btn.setIconSize(QSize(20, 20))
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        foot.addWidget(self.usage, 1)
        foot.addWidget(self.topup_btn)
        foot.addSpacing(6)
        foot.addWidget(self.start_btn)
        root.addLayout(foot)

        self._show_more(bool(s.get("show_more")))
        self.balance = None
        self.free_pages = 60
        self._set_make(s.get("make") if s.get("make") in dict(MAKES) else "occlusion",
                       scope=s.get("scope"))
        self._refresh()
        self._load_balance()
        telemetry.watch_buttons(self, "auto_occlude")
        # A modal window that slips behind its parent (after a trip to the browser for
        # sign-in or pricing, say) still blocks every click while being invisible. Bring
        # it back to the front whenever Anki becomes the active app again.
        QApplication.instance().applicationStateChanged.connect(self._keep_in_front)

    # --- building blocks --------------------------------------------------
    @staticmethod
    def _qss(c: dict) -> str:
        return (
            f"QDialog#generate {{ background:{c['window']}; }}"
            f"QFrame#rule {{ background:{c['line']}; border:none; }}"
            f"QLabel {{ color:{c['text']}; }}"
            f"QLabel#rowLabel {{ color:{c['sub']}; }}"
            f"QLabel#usage {{ color:{c['sub']}; }}"
            f"QLabel#usage[short=\"true\"] {{ color:#d93025; }}"
            f"QLabel#value {{ color:{c['text']}; }}"
            f"QLabel#note {{ color:{c['sub']}; }}"
            f"QLabel#creditBadge {{ color:{c['accent']}; background:{c['btn']}; border:1px solid {c['accent']};"
            f" border-radius:8px; padding:0 6px; font-size:10px; font-weight:600; min-height:15px; }}"
            f"QSpinBox#count {{ background:{c['btn']}; color:{c['text']}; border:1px solid {c['btn_line']};"
            f" border-radius:7px; padding:3px 4px; min-height:24px; }}"
            f"QToolButton#tile {{ background:{c['btn']}; color:{c['text']};"
            f" border:1px solid {c['btn_line']}; border-radius:10px; padding:6px 4px 8px 4px; }}"
            f"QToolButton#tile:hover {{ background:{c['btn_hover']}; }}"
            f"QToolButton#tile:checked {{ border:2px solid {c['accent']}; padding:5px 3px 7px 3px; }}"
            f"QToolButton#acct {{ border:none; background:transparent; padding:0; }}"
            f"QToolButton#acct::menu-indicator {{ image:none; width:0; }}"
            f"QPushButton#link {{ border:none; background:transparent; color:{c['accent']};"
            f" padding:0; text-align:left; }}"
            f"QPlainTextEdit#instructions {{ background:{c['btn']}; color:{c['text']};"
            f" border:1px solid {c['btn_line']}; border-radius:7px; padding:4px 6px; }}"
            f"QFrame#seg QToolButton#segBtn {{ padding:0 12px; color:{c['text']}; }}"
        )

    def _tile(self, name, icon, w, h, icon_px, caption=""):
        from aqt.qt import QToolButton, QSize
        b = QToolButton()
        b.setObjectName("tile")
        b.setText(name + (f"\n{caption}" if caption else ""))
        b.setIcon(icon)
        b.setIconSize(QSize(icon_px, icon_px))
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        b.setCheckable(True)
        b.setFixedSize(w, h + (16 if caption else 0))
        if caption:
            f = b.font(); f.setPointSizeF(max(8.5, f.pointSizeF() - 1.5)); b.setFont(f)
        return b

    def _segmented(self, options, current=None, below=False):
        from aqt.qt import QFrame, QToolButton
        from aqt.qt import QSizePolicy
        seg = QFrame()
        seg.setObjectName("seg")
        seg.setFixedHeight(34)
        seg.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(seg)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(2)
        group = QButtonGroup(seg)
        btns = {}
        seg._row, seg._group = row, group
        for value, label in options:
            self._seg_add(seg, btns, value, label, value == current)
        wrap = QWidget()
        note = QLabel()
        note.setObjectName("note")
        if below:
            # a longer description: one line under the buttons, across the column
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setSpacing(6)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(seg)
            row.addStretch(1)
            wl.addLayout(row)
            note.setWordWrap(True)
            wl.addWidget(note)
        else:
            wl = QHBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setSpacing(12)
            wl.addWidget(seg)
            note.setMaximumWidth(210)
            wl.addWidget(note)
            wl.addStretch(1)
        wrap._seg = seg
        wrap._note = note
        return wrap, btns

    def _seg_add(self, seg, btns, value, label, checked):
        from aqt.qt import QToolButton
        b = QToolButton()
        b.setObjectName("segBtn")
        b.setText(label)
        b.setCheckable(True)
        b.setFixedHeight(28)
        b.setChecked(checked)
        b.toggled.connect(lambda on: on and self._refresh())
        seg._group.addButton(b)
        seg._row.addWidget(b)
        btns[value] = b

    def _label(self, text):
        lab = QLabel(text)
        lab.setObjectName("rowLabel")
        lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lab.setFixedWidth(self.LABEL_W)
        return lab

    def _add_row(self, key, label, widget, top=False):
        r = self.grid.rowCount()
        lab = self._label(label)
        if top:
            lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            lab.setContentsMargins(0, 6, 0, 0)
        self.grid.addWidget(lab, r, 0)
        self.grid.addWidget(widget, r, 1)
        self._rows[key] = (lab, widget)

    def _show_row(self, key, on):
        for w in self._rows[key]:
            w.setVisible(on)

    @staticmethod
    def _picked(btns, default):
        return next((k for k, b in btns.items() if b.isChecked()), default)

    # --- window behaviour -------------------------------------------------
    def _keep_in_front(self, state=None):
        if self.isVisible() and (state is None or state == Qt.ApplicationState.ApplicationActive):
            self.raise_()
            self.activateWindow()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._keep_in_front)

    def done(self, r):
        try:
            QApplication.instance().applicationStateChanged.disconnect(self._keep_in_front)
        except (TypeError, RuntimeError):
            pass
        super().done(r)

    def _set_make(self, make: str, scope: Optional[str] = None):
        """Show only what applies to the chosen output."""
        self.make = make
        boxes, cards, quiz = make == "occlusion", make == "atomic", make == "quiz"
        keep = scope or self._picked(self.scope_btns, None)
        options = ([("current", "This Slide"), ("empty", "Unboxed"), ("all", "All")] if boxes
                   else [("current", "This Slide"), ("all", "All")])
        seg = self.scope_seg._seg
        for b in list(self.scope_btns.values()):
            seg._group.removeButton(b)
            seg._row.removeWidget(b)
            b.hide()
            b.setParent(None)
            b.deleteLater()
        self.scope_btns.clear()
        values = [v for v, _ in options]
        pick = keep if keep in values else ("empty" if boxes else "all")
        for value, label in options:
            self._seg_add(seg, self.scope_btns, value, label, value == pick)
        self._show_row("scope", not quiz)     # a quiz is always made from the whole lecture
        self._show_row("density", boxes)
        self._show_row("card_style", cards)
        self._show_row("quiz_style", quiz)
        self._show_row("quiz_count", quiz)
        self._show_row("notes", self._has_notes and not quiz)
        self.more_toggle.setVisible(boxes)
        if not boxes:
            self.more.setVisible(False)
        if quiz and not getattr(self, "_count_touched", False):
            # a starting point that fits the lecture: about one question per two slides
            n = self.slide_counts.get("all", 0)
            if n and int(settings().get("quiz_count") or 0) == DEFAULTS["quiz_count"]:
                self.quiz_count.setValue(max(10, min(QUIZ_COUNT[1], round(n / 2))))
        self._refresh()
        self.adjustSize()

    def _show_more(self, on: bool):
        self.more.setVisible(on)
        self.more_toggle.setText("Fewer Settings" if on else "More Settings")
        self.adjustSize()

    def _combo(self, item, s) -> QComboBox:
        key, _, opts = item
        c = QComboBox()
        for value, text in opts:
            c.addItem(text, value)
        values = [v for v, _ in opts]
        c.setCurrentIndex(values.index(s[key]) if s[key] in values else 0)
        self.combos[key] = c
        return c

    # --- account ----------------------------------------------------------
    def _load_balance(self):
        a = account()
        if not a.get("token"):
            # how many pages a new account gets is the server's to say
            in_background(lambda: call("/v1/health", timeout=10), self._got_health)
            return
        in_background(lambda: call("/v1/me", token=a["token"], timeout=20), self._got_balance)

    def _got_health(self, res, err):
        if not err and isinstance(res, dict) and (res.get("free_credits") or res.get("free_pages")):
            self.free_pages = int(res.get("free_credits") or res["free_pages"])
            self._refresh()

    def _got_balance(self, me, err):
        if err:
            if isinstance(err, ServerError) and err.status == 401:
                sign_out()
            self.balance = None
        else:
            self.balance = me
        self._refresh()

    def _fill_account_menu(self):
        m = self.acct_menu
        m.clear()
        a = account()
        b = self.balance or {}
        m.addAction(a.get("email") or "Signed In").setEnabled(False)
        parts = []
        if b.get("plan") in ("monthly", "yearly"):
            parts.append(f"{b['plan'].capitalize()} Plan")
        left = _credits_left(b)
        if left is not None:
            parts.append(f"{left:,} Credits")
        if parts:
            m.addAction(", ".join(parts)).setEnabled(False)
        m.addSeparator()
        m.addAction("Buy Credits", lambda: webbrowser.open(f"{SITE}/pricing/"))
        m.addAction("Account", lambda: webbrowser.open(f"{SITE}/account/"))
        m.addSeparator()
        m.addAction("Sign Out", self._sign)

    def _sign(self):
        if account().get("token"):
            telemetry.track("sign_out")
            sign_out()
            self.balance = None
            self._refresh()
        else:
            # kept on the window: Qt only holds a weak reference to the
            # timer's slot, so an unreferenced SignIn would never poll
            self._signin = SignIn(self, lambda: (self._refresh(), self._load_balance()))
            self._signin.start()

    # --- state ------------------------------------------------------------
    def _refresh(self):
        if not hasattr(self, "start_btn"):
            return
        a = account()
        signed = bool(a.get("token"))
        b = self.balance or {}
        self._who.setVisible(signed)
        self.signin_btn.setVisible(not signed)
        bal = _credits_left(b) if signed else None
        self.credit_badge.setVisible(bal is not None)
        if bal is not None:
            self.credit_badge.setText(f"{bal:,}")
            self.credit_badge.setToolTip(f"{bal:,} credits")
        if signed:
            self.acct_btn.setIcon(_initial_icon(a.get("email") or "", self.c))
        make = getattr(self, "make", "occlusion")
        scope = "all" if make == "quiz" else self._picked(self.scope_btns, "all")
        n = self.slide_counts.get(scope, 0)
        q = self.quiz_count.value()
        costs = b.get("credit_costs") or {}
        # one balance: a credit per slide for boxes or cards, two per quiz question
        need = q * int(costs.get("question", 2)) if make == "quiz" else n * int(costs.get("slide", 1))
        left = _credits_left(b) if signed else None
        short = signed and left is not None and need > left
        replaces = make == "occlusion" and scope == "all" and self.slide_counts.get("empty", n) < n
        if not signed:
            self.usage.set(need, None, free=self.free_pages)
        else:
            self.usage.set(need, left, suffix=", replaces your boxes" if replaces else "")
        self.count_label.setText(f"{need:,} credits" if make == "quiz" else "")
        self._captions(make, scope)
        self.topup_btn.setVisible(short)
        names = {"occlusion": "Make Boxes", "atomic": "Make Cards", "quiz": "Make Quiz"}
        self.start_btn.setIcon(_go_icon(20))
        self.start_btn.setAccessibleName(names[make])
        self.start_btn.setToolTip(names[make])
        custom_missing = (make == "quiz" and self._picked(self.quiz_btns, "step1") == "custom"
                          and not self.custom.toPlainText().strip())
        self.start_btn.setEnabled(signed and n > 0 and not short and not custom_missing)

    def _captions(self, make: str, scope: str):
        """A few words beside each choice saying what the chosen option does."""
        n_all = self.slide_counts.get("all", 0)
        n_empty = self.slide_counts.get("empty", 0)
        slides = {
            "current": "This slide only",
            "empty": f"{n_empty} slide{'s' if n_empty != 1 else ''} without boxes",
            "all": (f"All {n_all} slides, replaces boxes" if make == "occlusion"
                    else f"All {n_all} slides"),
        }
        amount = {
            "fewer": "Main labels only",
            "balanced": "Recommended",
            "more": "Every label and fact",
        }
        styles = {
            "complete": "All key facts",
            "essentials": "High-yield only",
            "scratch": "Starts with definitions",
        }
        self.scope_seg._note.setText(slides.get(scope, ""))
        self.density_seg._note.setText(amount.get(self._picked(self.density_btns, "balanced"), ""))
        self.card_seg._note.setText(styles.get(self._picked(self.card_btns, "complete"), ""))
        exams = {v: e for v, _n, e in QUIZ_STYLES}
        self.quiz_seg._note.setText(exams.get(self._picked(self.quiz_btns, "step1"), ""))

    def _start(self):
        if not account().get("token"):
            self._sign()
            return
        self._accept()

    def _gather(self) -> dict:
        s = {k: c.currentData() for k, c in self.combos.items()}
        s["density"] = self._picked(self.density_btns, "balanced")
        s["skip_admin"] = self.skip_admin.isChecked()
        s["custom"] = self.custom.toPlainText().strip()[:1000]
        s["use_notes"] = self.notes.isChecked()
        s["scope"] = "all" if getattr(self, "make", "") == "quiz" else self._picked(self.scope_btns, "all")
        s["show_more"] = self.more.isVisible()
        s["make"] = getattr(self, "make", "occlusion")
        s["card_style"] = self._picked(self.card_btns, "complete")
        s["quiz_style"] = self._picked(self.quiz_btns, "step1")
        s["quiz_count"] = self.quiz_count.value()
        return s

    def _accept(self):
        self.chosen = self._gather()
        save_settings({**settings(), **self.chosen})
        self.accept()

    def reject(self):
        save_settings({**settings(), **self._gather()})
        super().reject()


# ------------------------------------------------------------------ the run

class Run:
    """Prepare slides, then send them in batches; apply each batch's boxes."""

    def __init__(self, dlg, pages: list, s: dict, notes_text: str, on_page_boxes, on_finish, on_page_cards=None):
        self.dlg, self.pages, self.s = dlg, pages, s
        # "atomic" makes text cloze cards (paid) instead of occlusion boxes; slides the
        # card writer says are better tested by location come back in self.occlude
        self.atomic = s.get("make") == "atomic"
        if self.atomic:
            # the card style is a few words of instruction ahead of the student's own
            style = dict((v, t) for v, _n, t in CARD_STYLES).get(s.get("card_style"), "")
            if style:
                s = {**s, "custom": (style + "\n" + (s.get("custom") or "")).strip()[:1000]}
                self.s = s
        self.quiz = s.get("make") == "quiz"
        self.quiz_url = None
        self.on_page_cards = on_page_cards
        self.occlude: list = []
        self.notes = notes_text
        self.on_page_boxes, self.on_finish = on_page_boxes, on_finish
        self.token = account().get("token")
        self.cancelled = False
        self.made = self.dropped = self.done_slides = 0
        self.prepared = []

    def start(self):
        total = len(self.pages)
        self.progress = QProgressDialog("Reading the slides", "Cancel", 0, total * 2, self.dlg)
        self.progress.setWindowTitle("Generate")
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.setMinimumDuration(0)
        self.progress.canceled.connect(self._cancel)
        self.progress.setValue(0)
        # same guard as the Auto window: a modal progress window hidden behind the PDF
        # Occlusion window would block every click while a run is going
        def keep_in_front(state, p=self.progress):
            try:
                if p.isVisible() and state == Qt.ApplicationState.ApplicationActive:
                    p.raise_()
                    p.activateWindow()
            except RuntimeError:   # the window is already gone
                pass
        QApplication.instance().applicationStateChanged.connect(keep_in_front)
        self._keep_progress_in_front = keep_in_front
        # Slides are rendered one per event-loop tick, and their text recognition
        # (a separate process per slide, ~0.5 s each on a Mac) runs on four workers
        # in parallel; slides are finished in order as their recognition comes back.
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=4) if ocr.available() else None
        self._images, self._reads = [], []
        self._rendered = 0
        QTimer.singleShot(0, self._prepare)

    def _prepare(self):
        from . import vision
        if self.cancelled:
            if self._pool:
                self._pool.shutdown(wait=False, cancel_futures=True)
            return self._finish()
        n = len(self.pages)
        if self._rendered < n:
            gidx, path, local, image = self.pages[self._rendered]
            if callable(image):
                image = image()
            self._images.append(image)
            self._reads.append(self._pool.submit(read_picture_text, QImage(image)) if self._pool else None)
            self._rendered += 1
        # finish every slide whose recognition is back, in order
        while len(self.prepared) < self._rendered:
            k = len(self.prepared)
            fut = self._reads[k]
            if fut is not None and not fut.done():
                break
            gidx, path, local, _img = self.pages[k]
            image = self._images[k]
            try:
                lines = slide_lines(path, local, self.dlg._render_scale, image,
                                    read=fut.result() if fut is not None else [])
            except Exception:       # noqa: BLE001 - a slide that can't be read is skipped
                lines = []
            units = vision.units_for_slide(lines)
            payload = (atomic_payload(local + 1, image, lines) if (self.atomic or self.quiz)
                       else vision.slide_payload(local + 1, image, units))
            self.prepared.append((gidx, local, image, lines, payload, units))
            self.progress.setValue(len(self.prepared))
        if len(self.prepared) < n:
            # keep rendering at full speed; only wait (briefly) when everything left is recognition
            QTimer.singleShot(0 if self._rendered < n else 30, self._prepare)
            return
        if self._pool:
            self._pool.shutdown(wait=False)
        self._start_job()

    def _start_job(self):
        self.progress.setLabelText("Reading the whole document")
        doc = self.dlg._doc_for_page(self.pages[0][0])
        lecture = doc.get("lecture", "")
        whole = document_payload(self.dlg, doc, self.prepared, self.notes if self.s.get("use_notes") else "")
        body = {"slides": len(self.prepared), "lecture": lecture, "doc": whole,
                "settings": {k: self.s[k] for k in SENT}}
        if self.quiz:
            body["kind"] = "quiz"
            # so the server can check the balance covers the questions asked for
            body["settings"]["count"] = int(self.s.get("quiz_count") or 20)
        # the server reads the whole document before the first batch
        in_background(lambda: call("/v1/jobs", body, self.token, 300), self._job_started)

    def _cancel(self):
        self.cancelled = True

    def _job_started(self, res, err):
        if err:
            telemetry.track("auto_occlude_error", stage="start", status=getattr(err, "status", 0),
                            message=str(err)[:200])
            self.progress.close()
            if isinstance(err, ServerError) and err.status == 402:
                showWarning(f"{err}. Get more on the pricing page.",
                            parent=self.dlg, title="PDF Occlusion")
                webbrowser.open(f"{SITE}/pricing/")
            elif isinstance(err, ServerError) and err.status == 401:
                sign_out()
                showWarning("Your sign-in has expired. Sign in again from Generate.",
                            parent=self.dlg, title="PDF Occlusion")
            else:
                showWarning(str(err), parent=self.dlg, title="PDF Occlusion")
            return self.on_finish(self.made, self.dropped, self.done_slides, False)
        self.job, self.batch = res["job"], int(res.get("batch_size") or 6)
        if self.quiz:
            return self._make_quiz()
        what = (res.get("document") or {}).get("subject")
        if what:
            self.progress.setLabelText(f"Occluding: {what}")
        self.queue = [self.prepared[i:i + self.batch] for i in range(0, len(self.prepared), self.batch)]
        self._next()

    def _next(self, attempt: int = 0):
        if self.cancelled or not self.queue:
            return self._finish()
        chunk = self.queue[0]
        body = {"slides": [{k: v for k, v in c[4].items() if not k.startswith("_")} for c in chunk],
                "settings": {k: self.s[k] for k in SENT}}
        if self.s.get("use_notes") and self.notes:
            body["notes"] = self.notes

        def done(res, err):
            if err:
                retry = isinstance(err, ServerError) and err.status in (0, 502, 503) and attempt < 2
                if retry and not self.cancelled:
                    QTimer.singleShot(3000 * (attempt + 1), lambda: self._next(attempt + 1))
                    return
                telemetry.track("auto_occlude_error", stage="batch", status=getattr(err, "status", 0),
                                message=str(err)[:200])
                self.progress.close()
                showWarning(f"Auto-occlude stopped: {err}\n\nBoxes made so far are kept.",
                            parent=self.dlg, title="PDF Occlusion")
                return self.on_finish(self.made, self.dropped, self.done_slides, False)
            self.queue.pop(0)
            pages = res.get("pages") or {}
            if self.atomic:
                wanted = {int(n) for n in (res.get("occlude") or []) if str(n).isdigit()}
                for gidx, local, _image, _lines, _payload, _units in chunk:
                    if self.on_page_cards:
                        self.on_page_cards(gidx, pages.get(str(local + 1)) or [], self)
                    if local + 1 in wanted:
                        self.occlude.append(gidx)
            else:
                for gidx, local, image, _lines, payload, units in chunk:
                    plan = pages.get(str(local + 1)) or pages.get(f"p{local + 1}") or {}
                    self.on_page_boxes(gidx, units, image, payload["_scale"], plan, self)
            self.done_slides += len(chunk)
            self.progress.setValue(len(self.pages) + self.done_slides)
            self._next()
        route = "atomic" if self.atomic else "batch"
        in_background(lambda: call(f"/v1/jobs/{self.job}/{route}", body, self.token, 300), done)

    def _make_quiz(self):
        """A quiz is written from the whole lecture in one request, then opened in the browser."""
        self.progress.setLabelText("Writing the questions")
        body = {"slides": [{k: v for k, v in c[4].items() if not k.startswith("_")} for c in self.prepared[:100]],
                "settings": {"style": self.s.get("quiz_style") or "step1",
                             "count": int(self.s.get("quiz_count") or 20),
                             "custom": self.s.get("custom") or ""}}

        def done(res, err):
            if err:
                telemetry.track("auto_occlude_error", stage="quiz", status=getattr(err, "status", 0),
                                message=str(err)[:200])
                self.progress.close()
                showWarning(f"The quiz could not be made: {err}", parent=self.dlg, title="PDF Occlusion")
                return self.on_finish(self.made, self.dropped, self.done_slides, False)
            self.quiz_url = res.get("url")
            self.made = int(res.get("n_questions") or 0)
            self.done_slides = len(self.prepared)
            self._finish()
        in_background(lambda: call(f"/v1/jobs/{self.job}/quiz", body, self.token, 900), done)

    def _finish(self):
        try:
            QApplication.instance().applicationStateChanged.disconnect(self._keep_progress_in_front)
        except (AttributeError, TypeError, RuntimeError):
            pass
        complete = not self.cancelled    # closing the progress window emits canceled
        self.progress.close()
        self.on_finish(self.made, self.dropped, self.done_slides, complete)
