"""Auto-occlude: a first draft of a lecture's boxes, placed by Claude.

The add-on does the slide work here: it reads each slide's words (the PDF's
text layer, plus text recognition on the picture for figure labels), sends
the slide images and their words to the PDF Occlusion server in small
batches, and turns the plan that comes back into ordinary boxes on the
canvas. Nothing is saved until the student presses Create All Cards.

The server holds the model key and counts lecture credits; this file only
ever holds the student's own sign-in key, in user_files/account.json.
"""
import base64
import json
import os
import re
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
    QPlainTextEdit, QProgressDialog, QPushButton, QTimer, QVBoxLayout, Qt,
)
from aqt.utils import showWarning, tooltip

from . import ocr

DEFAULT_SERVER = "https://pdf-occlusion-api.pdf-occlusion.workers.dev"
SITE = "https://pdf-occlusion.github.io"
MAX_PIXELS = 1_150_000
_ACCOUNT = os.path.join(os.path.dirname(__file__), "user_files", "account.json")

# (setting, label, [(value, label), ...]); the first choice is the default
SUBJECT = ("subject", "Subject", [
    ("auto", "Work it out"),
    ("anatomy", "Anatomy or embryology"),
    ("histology", "Histology or pathology"),
    ("pharmacology", "Pharmacology"),
    ("physiology", "Physiology or biochemistry"),
    ("microbiology", "Microbiology"),
    ("clinical", "Clinical medicine"),
    ("language", "A language"),
    ("other", "Something else"),
])
WHAT = [
    ("labels", "Figure labels", [
        ("all", "All labels"),
        ("relevant", "Only the important ones"),
        ("none", "Leave alone"),
    ]),
    ("text", "Bullet text", [
        ("key_phrase", "Key phrase of each fact"),
        ("whole_bullet", "Whole bullet, keep its subject"),
        ("block", "Sub-bullets as one block"),
        ("skip_on_figures", "Only on slides without a figure"),
        ("none", "Leave alone"),
    ]),
    ("numbers", "Numbers and doses", [
        ("key", "Key values only"),
        ("all", "Every number"),
        ("none", "Leave alone"),
    ]),
    ("emphasis", "Bold or colored text", [
        ("first", "Always box it"),
        ("same", "Like any other text"),
    ]),
    ("titles", "Slide titles", [
        ("never", "Leave alone"),
        ("when_answer", "Box when it is the answer"),
    ]),
    ("pictures", "Pictures without labels", [
        ("name", "Box the caption"),
        ("skip", "Leave alone"),
    ]),
]
HOW = [
    ("density", "Cards", [
        ("balanced", "Balanced"),
        ("fewer", "Fewer, only the most testable"),
        ("more", "More, every testable fact"),
    ]),
    ("lists", "Lists", [
        ("one_card", "One card for the whole list"),
        ("per_item", "One card per item"),
    ]),
    ("tables", "Tables", [
        ("per_cell", "One card per cell"),
        ("per_row", "One card per row"),
        ("per_column", "One card per column"),
        ("skip", "Leave alone"),
    ]),
    ("flows", "Flowcharts and pathways", [
        ("per_step", "One card per step"),
        ("whole", "One card for the chain"),
        ("skip", "Leave alone"),
    ]),
    ("repeats", "Same label twice", [
        ("together", "One card"),
        ("separate", "Separate cards"),
    ]),
    ("pairs", "Term and meaning lists", [
        ("meaning", "Hide the meaning"),
        ("term", "Hide the term"),
        ("both", "Card both ways"),
    ]),
]
CHOICES = [SUBJECT] + WHAT + HOW
SKIPS = [
    ("title", "Title slides"),
    ("objectives", "Objectives and outlines"),
    ("references", "References and thank-you slides"),
    ("cases", "Cases and audience questions"),
    ("recaps", "Recaps of earlier slides"),
    ("duplicates", "Repeated slides and builds"),
]
SCOPES = [
    ("empty", "Slides without boxes"),
    ("current", "This slide"),
    ("all", "Every slide, replacing its boxes"),
]
DEFAULTS = {
    **{key: opts[0][0] for key, _, opts in CHOICES},
    "skip": [k for k, _ in SKIPS], "custom": "", "use_notes": True, "scope": "empty",
}


# ------------------------------------------------------------------ config

def _cfg() -> dict:
    return mw.addonManager.getConfig(__name__.split(".")[0]) or {}


def settings() -> dict:
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (_cfg().get("auto_occlude") or {}).items() if k in DEFAULTS})
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


def call(path: str, body: Optional[dict] = None, token: Optional[str] = None,
         timeout: float = 120.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server() + path, data=data, method="POST" if data is not None else "GET")
    req.add_header("content-type", "application/json")
    req.add_header("user-agent", "pdf-occlusion-addon")
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


def slide_lines(path: str, page_index: int, scale: float, image: QImage) -> list:
    """Every line of text on a slide, as [{"id", "src", "words": [...]}]."""
    found = _group_lines(_page_words(path, page_index, scale))
    out, wid = [], 0
    for ln in found:
        out.append({"id": f"L{len(out)}", "src": "text", "words": [
            {"t": t, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0} for t, x0, y0, x1, y1 in ln]})
    if ocr.available():
        known = [w for ln in out for w in ln["words"]]
        try:
            read = ocr.recognize(image)
        except ocr.OcrError:
            read = []
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


def _pad(r, W, H):
    pad = max(3.0, 0.15 * r[3]) if r[3] < 120 else 6.0
    x0, y0 = max(0, r[0] - pad), max(0, r[1] - pad)
    x1, y1 = min(W, r[0] + r[2] + pad), min(H, r[1] + r[3] + pad)
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


def plan_to_boxes(cards: list, lines: list, W: int, H: int, next_group: int):
    """One slide's cards -> (boxes, cards made, cards dropped)."""
    page, out, made, dropped = _Page(lines, W, H), [], 0, 0
    for card in cards or []:
        if isinstance(card, (str, list)) or not isinstance(card, dict) or "hide" not in card:
            card = {"hide": card}
        targets = card.get("hide")
        if isinstance(targets, (str, dict)):
            targets = [targets]
        rects = []
        try:
            for t in targets or []:
                got, padded = page.resolve(t)
                rects += [r if padded else _pad(r, W, H) for r in got]
        except (ValueError, KeyError, TypeError):
            dropped += 1
            continue
        rects = [r for r in rects if r[2] > 2 and r[3] > 2]
        if not rects:
            dropped += 1
            continue
        grouped = len(rects) > 1
        guid = uuid.uuid4().hex if grouped else None
        for r in rects:
            out.append({"x": r[0], "y": r[1], "w": r[2], "h": r[3],
                        "group": next_group if grouped else None, "group_uid": guid,
                        "shape": "rect", "mode": None, "note": "", "id": uuid.uuid4().hex})
        next_group += grouped
        made += 1
    return out, made, dropped


# ------------------------------------------------------------------ sign in

class SignIn:
    """Link this Anki to an account: the site approves a code, we collect a key."""

    def __init__(self, parent, on_done):
        self.parent, self.on_done = parent, on_done
        self.timer, self.started = None, 0.0

    def start(self):
        def go():
            label = f"Anki on {os.uname().nodename}" if hasattr(os, "uname") else "Anki on Windows"
            return call("/v1/link/start", {"label": label[:80]})
        in_background(go, self._started)

    def _started(self, res, err):
        if err:
            showWarning(str(err), parent=self.parent, title="PDF Occlusion")
            return
        self.code, self.secret = res["code"], res["secret"]
        webbrowser.open(res["url"])
        self.box = QProgressDialog(f"Approve code {self.code} in your browser.", "Cancel", 0, 0, self.parent)
        self.box.setWindowTitle("Sign in")
        self.box.setMinimumDuration(0)
        self.box.canceled.connect(self._stop)
        self.box.show()
        self.started = time.time()
        self.timer = QTimer(self.parent)
        self.timer.timeout.connect(self._poll)
        self.timer.start(2000)

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
            tooltip(f"Signed in as {res.get('email') or 'your account'}", parent=self.parent)
            self.on_done()
        in_background(go, done)


# ------------------------------------------------------------------ the window

class AutoOccludeDialog(QDialog):
    """Settings, account and Start. Returns the chosen settings via .chosen."""

    def __init__(self, parent, has_notes: bool, slide_counts: dict, primary_qss: str):
        super().__init__(parent)
        self.setWindowTitle("Auto-occlude")
        self.setMinimumWidth(780)
        self.slide_counts = slide_counts
        self.chosen = None
        s = settings()
        root = QVBoxLayout(self)

        # account
        acct = QHBoxLayout()
        self.who = QLabel()
        self.who.setWordWrap(True)
        self.sign_btn = QPushButton()
        self.more_btn = QPushButton("Get more pages")
        self.more_btn.clicked.connect(lambda: webbrowser.open(f"{SITE}/pricing/"))
        acct.addWidget(self.who, stretch=1)
        acct.addWidget(self.more_btn)
        acct.addWidget(self.sign_btn)
        root.addLayout(acct)

        top = QFormLayout()
        top.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.scope = QComboBox()
        for value, label in SCOPES:
            self.scope.addItem(label, value)
        self.scope.setCurrentIndex([v for v, _ in SCOPES].index(s["scope"]) if s["scope"] in dict(SCOPES) else 0)
        self.scope.currentIndexChanged.connect(self._refresh)
        top.addRow("Slides", self.scope)
        self.combos = {}
        top.addRow(SUBJECT[1], self._combo(SUBJECT, s))
        root.addLayout(top)

        cols = QHBoxLayout()
        cols.setSpacing(24)
        for title, group in (("What to box", WHAT), ("How to card it", HOW)):
            col = QVBoxLayout()
            head = QLabel(title)
            head.setStyleSheet("font-weight:bold; margin-top:8px;")
            col.addWidget(head)
            form = QFormLayout()
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setVerticalSpacing(6)
            for item in group:
                form.addRow(item[1], self._combo(item, s))
            col.addLayout(form)
            cols.addLayout(col, stretch=1)
        root.addLayout(cols)

        skip_title = QLabel("Skip")
        skip_title.setStyleSheet("font-weight:bold; margin-top:6px;")
        root.addWidget(skip_title)
        grid = QGridLayout()
        self.skips = {}
        for i, (key, label) in enumerate(SKIPS):
            cb = QCheckBox(label)
            cb.setChecked(key in s["skip"])
            self.skips[key] = cb
            grid.addWidget(cb, i // 2, i % 2)
        root.addLayout(grid)

        self.notes = QCheckBox("Use the notes PDF")
        self.notes.setChecked(bool(s["use_notes"]) and has_notes)
        self.notes.setEnabled(has_notes)
        root.addSpacing(4)
        root.addWidget(self.notes)

        custom_title = QLabel("Your own instructions")
        custom_title.setStyleSheet("font-weight:bold; margin-top:6px;")
        root.addWidget(custom_title)
        self.custom = QPlainTextEdit()
        self.custom.setPlainText(s["custom"])
        self.custom.setFixedHeight(70)
        root.addWidget(self.custom)

        buttons = QDialogButtonBox()
        self.start_btn = buttons.addButton("Start", QDialogButtonBox.ButtonRole.AcceptRole)
        self.start_btn.setStyleSheet(primary_qss)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.sign_btn.clicked.connect(self._sign)
        self.adjustSize()
        self.balance = None
        self._refresh()
        self._load_balance()

    def _combo(self, item, s) -> QComboBox:
        key, _, opts = item
        c = QComboBox()
        for value, text in opts:
            c.addItem(text, value)
        values = [v for v, _ in opts]
        c.setCurrentIndex(values.index(s[key]) if s[key] in values else 0)
        self.combos[key] = c
        return c

    def _load_balance(self):
        a = account()
        if not a.get("token"):
            return
        in_background(lambda: call("/v1/me", token=a["token"], timeout=20), self._got_balance)

    def _got_balance(self, me, err):
        if err:
            if isinstance(err, ServerError) and err.status == 401:
                sign_out()
            self.balance = None
        else:
            self.balance = me
        self._refresh()

    def _sign(self):
        if account().get("token"):
            sign_out()
            self.balance = None
            self._refresh()
        else:
            # kept on the window: Qt only holds a weak reference to the
            # timer's slot, so an unreferenced SignIn would never poll
            self._signin = SignIn(self, lambda: (self._refresh(), self._load_balance()))
            self._signin.start()

    def _refresh(self):
        a = account()
        signed = bool(a.get("token"))
        if signed:
            left = self.balance.get("pages_left") if self.balance else None
            tail = "" if left is None else f", {left} page{'s' if left != 1 else ''} left"
            self.who.setText(f"{a.get('email') or 'Signed in'}{tail}")
        else:
            self.who.setText("Not signed in")
        self.sign_btn.setText("Sign out" if signed else "Sign in")
        n = self.slide_counts.get(self.scope.currentData(), 0)
        self.start_btn.setText(f"Auto-occlude {n} slide{'s' if n != 1 else ''}")
        left = self.balance.get("pages_left") if (signed and self.balance) else None
        self.start_btn.setEnabled(signed and n > 0 and (left is None or n <= left))

    def _gather(self) -> dict:
        s = {k: c.currentData() for k, c in self.combos.items()}
        s["skip"] = [k for k, cb in self.skips.items() if cb.isChecked()]
        s["custom"] = self.custom.toPlainText().strip()[:600]
        s["use_notes"] = self.notes.isChecked()
        s["scope"] = self.scope.currentData()
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

    def __init__(self, dlg, pages: list, s: dict, notes_text: str, on_page_boxes, on_finish):
        self.dlg, self.pages, self.s = dlg, pages, s
        self.notes = notes_text
        self.on_page_boxes, self.on_finish = on_page_boxes, on_finish
        self.token = account().get("token")
        self.cancelled = False
        self.made = self.dropped = self.done_slides = 0
        self.prepared = []

    def start(self):
        total = len(self.pages)
        self.progress = QProgressDialog("Reading the slides…", "Cancel", 0, total * 2, self.dlg)
        self.progress.setWindowTitle("Auto-occlude")
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.setMinimumDuration(0)
        self.progress.canceled.connect(self._cancel)
        self.progress.setValue(0)
        # reading is local and quick per slide; keep the window alive between
        for i, (gidx, path, local, image) in enumerate(self.pages):
            if self.cancelled:
                return self._finish()
            try:
                lines = slide_lines(path, local, self.dlg._render_scale, image)
            except Exception:       # noqa: BLE001 - a slide that can't be read is skipped
                lines = []
            self.prepared.append((gidx, local, image, lines, slide_payload(local + 1, image, lines)))
            self.progress.setValue(i + 1)
            QApplication.processEvents()
        self.progress.setLabelText("Occluding…")
        lecture = self.dlg._doc_for_page(self.pages[0][0]).get("lecture", "")
        in_background(lambda: call("/v1/jobs", {"slides": len(self.prepared), "lecture": lecture}, self.token, 30),
                      self._job_started)

    def _cancel(self):
        self.cancelled = True

    def _job_started(self, res, err):
        if err:
            self.progress.close()
            if isinstance(err, ServerError) and err.status == 402:
                showWarning(f"{err}. Get more on the pricing page.",
                            parent=self.dlg, title="PDF Occlusion")
                webbrowser.open(f"{SITE}/pricing/")
            elif isinstance(err, ServerError) and err.status == 401:
                sign_out()
                showWarning("Your sign-in has expired. Sign in again from Auto-occlude.",
                            parent=self.dlg, title="PDF Occlusion")
            else:
                showWarning(str(err), parent=self.dlg, title="PDF Occlusion")
            return self.on_finish(self.made, self.dropped, self.done_slides, False)
        self.job, self.batch = res["job"], int(res.get("batch_size") or 6)
        self.queue = [self.prepared[i:i + self.batch] for i in range(0, len(self.prepared), self.batch)]
        self._next()

    def _next(self, attempt: int = 0):
        if self.cancelled or not self.queue:
            return self._finish()
        chunk = self.queue[0]
        body = {"slides": [c[4] for c in chunk],
                "settings": {k: self.s[k] for k in [c[0] for c in CHOICES] + ["skip", "custom"]}}
        if self.s.get("use_notes") and self.notes:
            body["notes"] = self.notes

        def done(res, err):
            if err:
                retry = isinstance(err, ServerError) and err.status in (0, 502, 503) and attempt < 2
                if retry and not self.cancelled:
                    QTimer.singleShot(3000 * (attempt + 1), lambda: self._next(attempt + 1))
                    return
                self.progress.close()
                showWarning(f"Auto-occlude stopped: {err}\n\nBoxes made so far are kept.",
                            parent=self.dlg, title="PDF Occlusion")
                return self.on_finish(self.made, self.dropped, self.done_slides, False)
            self.queue.pop(0)
            pages = res.get("pages") or {}
            for gidx, local, image, lines, _p in chunk:
                cards = pages.get(str(local + 1)) or pages.get(f"p{local + 1}") or []
                self.on_page_boxes(gidx, lines, image.width(), image.height(), cards, self)
            self.done_slides += len(chunk)
            self.progress.setValue(len(self.pages) + self.done_slides)
            self._next()
        in_background(lambda: call(f"/v1/jobs/{self.job}/batch", body, self.token, 180), done)

    def _finish(self):
        complete = not self.cancelled    # closing the progress window emits canceled
        self.progress.close()
        self.on_finish(self.made, self.dropped, self.done_slides, complete)
