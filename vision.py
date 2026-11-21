"""Vision auto-occlude (v2).

Every piece of text on a slide (from the PDF's text layer, plus text recognised inside pictures) is
grouped into layout units: a figure label, a bullet with its wrapped lines, a table cell, a title.
The units are outlined and numbered on the slide image that goes to the model, so it chooses what to
cover by looking at the slide, and answers with unit ids, phrases inside a unit, or picture regions.
The answer is turned into boxes shaped the way a person draws them: one padded box per label or
phrase block, an L-shape for a phrase that starts mid-line, padding that stops short of other words,
bullets left out, picture regions tightened to what is drawn and evened out across a row or column.
"""
import base64
import re
import unicodedata
import uuid

from aqt.qt import (QBuffer, QColor, QFont, QIODevice, QImage, QPainter, QPen, QRectF, Qt, qGray)

from .auto_occlude import MAX_PIXELS, _separate

from statistics import median


def lines_from_words(words, W=1, H=1):
    """words: normalised dicts {t,x,y,w,h} -> lines: [{"words": [...px words], "x0","y0","x1","y1","h"}]."""
    px = [{"t": w["t"], "x0": w["x"] * W, "y0": w["y"] * H, "x1": (w["x"] + w["w"]) * W, "y1": (w["y"] + w["h"]) * H}
          for w in words if w["w"] > 0 and w["h"] > 0 and w["t"].strip()]
    # 1) rows: words whose vertical extents overlap enough share a row
    px.sort(key=lambda w: (w["y0"] + w["y1"]) / 2)
    rows = []
    for w in px:
        h = w["y1"] - w["y0"]; cy = (w["y0"] + w["y1"]) / 2
        for r in rows:
            rh = r["h"]
            if abs(r["cy"] - cy) < 0.4 * max(h, rh) and 0.55 < h / rh < 1.8:
                r["ws"].append(w); r["cy"] = sum((v["y0"] + v["y1"]) / 2 for v in r["ws"]) / len(r["ws"])
                r["h"] = median(v["y1"] - v["y0"] for v in r["ws"])
                break
        else:
            rows.append({"ws": [w], "cy": cy, "h": h})
    # 2) within a row, left to right, split where the horizontal gap is large (separate labels/columns)
    lines = []
    for r in rows:
        ws = sorted(r["ws"], key=lambda w: w["x0"])
        cur = [ws[0]]
        for w in ws[1:]:
            if w["x0"] - cur[-1]["x1"] > 1.2 * r["h"]:
                lines.append({"words": cur}); cur = [w]
            else:
                cur.append(w)
        lines.append({"words": cur})
    for ln in lines:
        ws = ln["words"]
        ln.update(x0=min(w["x0"] for w in ws), y0=min(w["y0"] for w in ws), x1=max(w["x1"] for w in ws),
                  y1=max(w["y1"] for w in ws), h=median(w["y1"] - w["y0"] for w in ws))
        ln["text"] = " ".join(w["t"] for w in ws)
    lines.sort(key=lambda l: (l["y0"], l["x0"]))
    return lines


def units_from_lines(lines):
    """Stack lines into units: next line starts close below, similar height, and overlaps horizontally
    with a left edge near the unit's (a wrapped bullet) or is centred under it (a stacked label)."""
    units = []
    for ln in lines:
        home = None
        for u in reversed(units):
            last = u["lines"][-1]
            gap = ln["y0"] - last["y1"]
            hs = max(ln["h"], last["h"])
            similar = 0.7 < ln["h"] / last["h"] < 1.4
            overlap = min(ln["x1"], last["x1"]) - max(ln["x0"], last["x0"])
            left_ok = abs(ln["x0"] - last["x0"]) < 1.5 * hs or abs(ln["x0"] - u["lines"][0]["x0"]) < 1.5 * hs
            centred = abs((ln["x0"] + ln["x1"]) / 2 - (last["x0"] + last["x1"]) / 2) < 1.5 * hs
            # a new bullet starts with a bullet char or a capital after a line that ended a sentence
            starts_new = ln["text"][:1] in "•-–▪◦●○■" or (last["text"].rstrip()[-1:] in ".:;" and ln["text"][:1].isupper())
            if -0.3 * hs < gap < 0.8 * hs and similar and overlap > 0 and (left_ok or centred) and not starts_new:
                home = u
                break
            if ln["y0"] - u["lines"][-1]["y1"] > 2 * hs:
                continue
        if home is None:
            units.append({"lines": [ln]})
        else:
            home["lines"].append(ln)
    for k, u in enumerate(units):
        ls = u["lines"]
        u.update(id=f"U{k + 1}", x0=min(l["x0"] for l in ls), y0=min(l["y0"] for l in ls),
                 x1=max(l["x1"] for l in ls), y1=max(l["y1"] for l in ls), h=median(l["h"] for l in ls))
        u["text"] = " / ".join(l["text"] for l in ls)
    return units


def listing(units):
    """What the model reads beside the marked image."""
    out = []
    for u in units:
        out.append(f'{u["id"]} @{int(u["x0"])},{int(u["y0"])} {int(u["x1"] - u["x0"])}x{int(u["y1"] - u["y0"])} | {u["text"]}')
    return "\n".join(out)



PAD_Y, PAD_L, PAD_R = 0.30, 0.30, 0.50     # in word heights; Ravi's medians ~0.25 / 0.22 / 0.64


def _norm(t):
    t = unicodedata.normalize("NFKC", t).lower()
    return re.sub(r"^[^\w<>≤≥±↑↓%]+|[^\w<>≤≥±↑↓%]+$", "", t)


def _bbox(ws):
    return [min(w["x0"] for w in ws), min(w["y0"] for w in ws), max(w["x1"] for w in ws), max(w["y1"] for w in ws)]


def _contains(r, w):
    cx, cy = (w["x0"] + w["x1"]) / 2, (w["y0"] + w["y1"]) / 2
    return r[0] <= cx <= r[2] and r[1] <= cy <= r[3]


def _fuzzy_words(flat, toks, cutoff=0.8):
    """OCR misreads ("intimal thoracio artery" for "internal thoracic artery"): the run of
    words, n-1 to n+1 long, whose letters are closest to the phrase, if close enough."""
    from difflib import SequenceMatcher
    want, n, best = " ".join(toks), len(toks), (cutoff, None)
    for m in {max(1, n - 1), n, n + 1}:
        for s in range(len(flat) - m + 1):
            got = " ".join(_norm(w["t"]) for _, w in flat[s:s + m])
            r = SequenceMatcher(None, want, got).ratio()
            if r > best[0]:
                best = (r, flat[s:s + m])
    return best[1]


def phrase_words(unit, text, img=None):
    """Consecutive words of `unit` matching `text` -> list of (line index, word)."""
    flat = [(k, w) for k, ln in enumerate(unit["lines"]) for w in ln["words"] if _norm(w["t"])]
    toks = [t for t in (_norm(t) for t in str(text).split()) if t]
    if not toks:
        raise ValueError("empty phrase")
    n = len(toks)
    for s in range(len(flat) - n + 1):
        if all(_norm(flat[s + i][1]["t"]) == toks[i] for i in range(n)):
            return flat[s:s + n]
    # tolerate OCR splitting/joining: match on concatenated characters
    joined = "".join(toks)
    for s in range(len(flat)):
        acc = ""
        for e in range(s, len(flat)):
            acc += _norm(flat[e][1]["t"])
            if acc == joined:
                return flat[s:e + 1]
            if not joined.startswith(acc):
                break
    # last resort: the phrase sits inside glued tokens ("fibrosis-this", "load=GFRxPx"); match characters
    # and box only the matching part of a token, in proportion to its characters
    raw = [(k, w) for k, ln in enumerate(unit["lines"]) for w in ln["words"]]
    s, spans = "", []
    for k, w in raw:
        if s:
            s += " "
        spans.append((len(s), len(s) + len(w["t"]), k, w))
        s += w["t"]
    want = " ".join(str(text).split()).lower()
    at = s.lower().find(want)
    if at < 0:
        fuzzy = _fuzzy_words(flat, toks)
        if fuzzy:
            return fuzzy
        raise ValueError(f"phrase not in {unit['id']}: {text!r}")
    end = at + len(want)
    out = []
    for a0, a1, k, w in spans:
        lo, hi = max(a0, at), min(a1, end)
        if lo >= hi:
            continue
        n = max(1, a1 - a0)
        part = dict(w)
        part["x0"] = w["x0"] + (w["x1"] - w["x0"]) * (lo - a0) / n
        part["x1"] = w["x0"] + (w["x1"] - w["x0"]) * (hi - a0) / n
        part["_of"] = id(w)
        if img is not None:
            if lo > a0:
                part["x0"] = _blank_col(img, part["x0"], w)
            if hi < a1:
                part["x1"] = _blank_col(img, part["x1"], w)
        out.append((k, part))
    return out


def _blank_col(img, x, w):
    """Move a cut inside a glued token to the nearest column with no ink (a gap between words or
    glyphs), searching within a third of the token's width; character proportions alone misplace it
    when the token holds wide glyphs such as long arrows."""
    try:
        g = img.convert("L")
    except Exception:
        return x
    y0, y1 = int(w["y0"]), max(int(w["y0"]) + 1, int(w["y1"]))
    span = (w["x1"] - w["x0"]) / 3
    lo, hi = int(max(w["x0"], x - span)), int(min(w["x1"], x + span))
    if hi - lo < 2:
        return x
    edge = [g.getpixel((int(w["x0"]), yy)) for yy in range(y0, y1, max(1, (y1 - y0) // 8))]
    bg = sorted(edge)[len(edge) // 2]
    best = None
    for cx in range(lo, hi):
        ink = sum(1 for yy in range(y0, y1, max(1, (y1 - y0) // 12)) if abs(g.getpixel((cx, yy)) - bg) > 60)
        if ink == 0 and (best is None or abs(cx - x) < abs(best - x)):
            best = cx
    return float(best) if best is not None else x


BULLETS = set("•◦▪▫●○■□►▸*·")
KEEP = set("<>≤≥±↑↓→←%+=×/~")


def strip_marks(ws):
    """Drop bullet marks and punctuation-only tokens at the start or end of a line's span."""
    def mark(w):
        return w["t"] in BULLETS or (not _norm(w["t"]) and not (set(w["t"]) & KEEP))
    while ws and mark(ws[0]):
        ws = ws[1:]
    while ws and mark(ws[-1]):
        ws = ws[:-1]
    return ws


def tidy_rects(spans, others):
    """spans: list of word lists, one per line, top to bottom. Merge neighbouring lines into one rectangle
    whenever the union covers no other word; otherwise keep separate rectangles (the L-shape a person draws)."""
    rects = []
    for ws in spans:
        ws = strip_marks(ws)
        if not ws:
            continue
        b = _bbox(ws)
        if rects:
            u = [min(rects[-1][0], b[0]), rects[-1][1], max(rects[-1][2], b[2]), b[3]]
            if not any(_contains(u, o) for o in others):
                rects[-1] = u
                continue
        rects.append(b)
    return rects


def pad(r, h, others, W, H):
    """Pad a text rectangle, but stop each side a small gap short of any other word it would reach."""
    x0, y0, x1, y1 = r
    gap = 0.12 * h
    L, T, R, B = x0 - PAD_L * h, y0 - PAD_Y * h, x1 + PAD_R * h, y1 + PAD_Y * h
    for o in others:
        vert = o["y1"] > y0 and o["y0"] < y1          # beside the text (shares rows)
        horiz = o["x1"] > x0 and o["x0"] < x1         # above/below the text (shares columns)
        if vert and o["x1"] <= x0: L = max(L, min(x0, o["x1"] + gap))
        if vert and o["x0"] >= x1: R = min(R, max(x1, o["x0"] - gap))
        if horiz and o["y1"] <= y0: T = max(T, min(y0, o["y1"] + gap))
        if horiz and o["y0"] >= y1: B = min(B, max(y1, o["y0"] - gap))
    return [max(0, L), max(0, T), min(W, R), min(H, B)]


def resolve_region(r, words, img, W, H):
    """A rough picture/text region from the model -> a clean box.
    Words mostly inside the region are covered whole; every other word is a wall the box stops short of;
    with no words inside, the box shrinks to the ink within it (never grows past what the model drew)."""
    x0, y0, x1, y1 = r
    inside, others = [], []
    for w in words:
        ix = max(0, min(x1, w["x1"]) - max(x0, w["x0"])); iy = max(0, min(y1, w["y1"]) - max(y0, w["y0"]))
        area = max(1e-6, (w["x1"] - w["x0"]) * (w["y1"] - w["y0"]))
        (inside if ix * iy / area >= 0.5 else others).append(w)
    if inside:
        b = _bbox(inside)
        h = sorted(w["y1"] - w["y0"] for w in inside)[len(inside) // 2]
        return pad(b, h, others, W, H), True
    shrunk = snap_region(r, img, W, H, grow=False)
    c = _clip(shrunk, others)
    return (c if c[2] - c[0] > 2 and c[3] - c[1] > 2 else shrunk), False


def _clip(r, others):
    """Pull the sides of r back so it overlaps no other word."""
    x0, y0, x1, y1 = r
    for o in others:
        if o["x1"] <= x0 or o["x0"] >= x1 or o["y1"] <= y0 or o["y0"] >= y1:
            continue
        # cut on the side that loses the least area
        cuts = [(o["x1"] - x0, "L"), (x1 - o["x0"], "R"), (o["y1"] - y0, "T"), (y1 - o["y0"], "B")]
        d, side = min(cuts)
        if side == "L": x0 = o["x1"]
        elif side == "R": x1 = o["x0"]
        elif side == "T": y0 = o["y1"]
        else: y1 = o["y0"]
    return [x0, y0, x1, y1]


def snap_region(r, img, W, H, grow=True):
    """Tighten a rough region to the drawn content inside it (pixels that differ from the region's
    border colour). Uses only img.convert("L").getpixel/width/height, so a QImage adapter works too."""
    if img is None:
        return r
    g = img.convert("L")
    x0, y0, x1, y1 = (int(round(v)) for v in r)
    mx, my = (int(0.15 * (x1 - x0)), int(0.15 * (y1 - y0))) if grow else (0, 0)
    X0, Y0, X1, Y1 = max(0, x0 - mx), max(0, y0 - my), min(g.width, x1 + mx), min(g.height, y1 + my)
    if X1 - X0 < 4 or Y1 - Y0 < 4:
        return r
    step = max(1, int(max(X1 - X0, Y1 - Y0) / 200))
    edge = [g.getpixel((x, Y0)) for x in range(X0, X1, step)] + [g.getpixel((x, Y1 - 1)) for x in range(X0, X1, step)]
    bg = sorted(edge)[len(edge) // 2]
    bx0 = by0 = 10 ** 9
    bx1 = by1 = -1
    for y in range(Y0, Y1, step):
        for x in range(X0, X1, step):
            if abs(g.getpixel((x, y)) - bg) > 40:
                bx0, by0, bx1, by1 = min(bx0, x), min(by0, y), max(bx1, x + step), max(by1, y + step)
    if bx1 < 0 or (bx1 - bx0) * (by1 - by0) < 0.05 * max(1, (x1 - x0) * (y1 - y0)):
        return r
    if not grow:
        return [max(x0, bx0), max(y0, by0), min(x1, bx1), min(y1, by1)]
    h = max(8, 0.25 * min(bx1 - bx0, by1 - by0))
    return [max(0, bx0 - 0.3 * h), max(0, by0 - 0.3 * h), min(W, bx1 + 0.3 * h), min(H, by1 + 0.3 * h)]


def _clean_edges(r, img):
    """A vertical box edge that lands on ink (it would slice a letter) moves to the nearest blank
    column within half a line height. r is [x, y, w, h]."""
    if img is None:
        return
    g = img.convert("L")
    x, y, w, h = r
    y0, y1 = int(y + 0.15 * h), int(y + 0.85 * h)
    if y1 - y0 < 3:
        return
    rows = range(y0, y1, max(1, (y1 - y0) // 10))
    ref = sorted(g.getpixel((min(g.width - 1, max(0, int(x + w / 2))), yy)) for yy in (int(y + 1), int(y + h - 2)))
    W = g.width

    def inked(cx):
        if cx < 0 or cx >= W:
            return False
        col = [g.getpixel((cx, yy)) for yy in rows]
        bg = max(set(col), key=col.count)
        return any(abs(v - bg) > 70 for v in col) and len({v // 32 for v in col}) > 1

    reach = int(max(3, 0.5 * h))
    for side in ("L", "R"):
        cx = int(x) if side == "L" else int(x + w)
        if not inked(cx):
            continue
        for d in range(1, reach):
            for c in (cx - d, cx + d):
                if not inked(c):
                    if side == "L":
                        r[2] = (x + w) - c; r[0] = c
                    else:
                        r[2] = c - r[0]
                    break
            else:
                continue
            break


def _split_own(rects):
    """Rectangles of one card that overlap (an L-shaped phrase after padding) meet at a clean line."""
    rects.sort(key=lambda r: r[1])
    for a, b in zip(rects, rects[1:]):
        oy = a[1] + a[3] - b[1]
        ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
        if oy > 0 and ox > 0:
            mid = b[1] + oy / 2
            a[3] = mid - a[1]
            b[3] = b[1] + b[3] - mid
            b[1] = mid


def _even_regions(cards, region_ids):
    """Picture-region boxes lined up in a column or row (checkmark cells, a strip of images) get one size."""
    regs = [r for k, (rects, _q) in enumerate(cards) for j, r in enumerate(rects) if (k, j) in region_ids]
    for axis in (0, 1):
        groups = []
        for r in regs:
            c = r[axis] + r[axis + 2] / 2
            for g in groups:
                if abs(g[0] - c) < 0.5 * max(r[2 + axis], 1):
                    g[1].append(r)
                    break
            else:
                groups.append([c, [r]])
        for _c, g in groups:
            if len(g) < 2:
                continue
            ws = [r[2] for r in g]; hs = [r[3] for r in g]
            if max(ws) > 1.6 * min(ws) or max(hs) > 1.6 * min(hs):
                continue          # only boxes that are already nearly the same size
            w = max(r[2] for r in g); h = max(r[3] for r in g)
            for r in g:
                cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
                r[0], r[1], r[2], r[3] = cx - w / 2, cy - h / 2, w, h


def plan_to_boxes(plan, units, W, H, img=None):
    """plan: {"skip", "cards": [{"q", "hide": [...]}]} -> boxes (px dicts with group)."""
    by_id = {u["id"]: u for u in units}
    all_words = [w for u in units for ln in u["lines"] for w in ln["words"]]
    cards, dropped, region_ids = [], 0, set()
    for card in (plan or {}).get("cards", []):
        hide = card.get("hide") if isinstance(card, dict) else card
        if isinstance(hide, (str, dict)):
            hide = [hide]
        rects = []
        for t in hide or []:
            try:
                if isinstance(t, str) and t.strip() in by_id:
                    u = by_id[t.strip()]
                    target = [w for ln in u["lines"] for w in ln["words"]]
                    spans = [ln["words"] for ln in u["lines"]]
                elif isinstance(t, dict) and "unit" in t and "text" in t:
                    u = by_id[str(t["unit"]).strip()]
                    pw = phrase_words(u, t["text"], img)
                    target = [w for _, w in pw]
                    spans = [[w for k2, w in pw if k2 == k] for k in sorted({k for k, _ in pw})]
                elif isinstance(t, dict) and "region" in t:
                    x0, y0, x1, y1 = (float(v) for v in t["region"])
                    box, had_text = resolve_region([max(0, x0), max(0, y0), min(W, x1), min(H, y1)], all_words, img, W, H)
                    rects.append(box)
                    if not had_text:      # only picture cells take part in evening out
                        region_ids.add((len(cards), len(rects) - 1))
                    continue
                else:
                    raise ValueError(f"bad target {t!r}")
                target = [w for w in target if w["t"] not in BULLETS and (_norm(w["t"]) or set(w["t"]) & KEEP)]
                if not target:
                    raise ValueError("only marks")
                whole = {id(x) for x in target if "_of" not in x}
                others = [w for w in all_words if id(w) not in whole and not any(x.get("_of") == id(w) for x in target)]
                for x in target:
                    if "_of" in x:   # the uncovered rest of a glued token stays visible
                        src = next(w for w in all_words if id(w) == x["_of"])
                        if x["x0"] > src["x0"] + 1:
                            others.append({**src, "x1": x["x0"]})
                        if x["x1"] < src["x1"] - 1:
                            others.append({**src, "x0": x["x1"]})
                h = sorted(w["y1"] - w["y0"] for w in target)[len(target) // 2]
                rects += [pad(r, h, others, W, H) for r in tidy_rects(spans, others)]
            except (ValueError, KeyError, TypeError):
                continue  # one unplaceable target (OCR misread, bad id) loses only itself
        if rects:
            cards.append(([[r[0], r[1], r[2] - r[0], r[3] - r[1]] for r in rects], card.get("q", "")))
        else:
            dropped += 1
    gray = img.convert("L") if img is not None else None   # converted once for every box
    for rects, _q in cards:
        for r in rects:
            _clean_edges(r, gray)
        _split_own(rects)
    _even_regions(cards, region_ids)
    _separate(cards)
    out = []
    for g, (rects, _q) in enumerate(cards):
        for r in rects:
            out.append({"x": r[0], "y": r[1], "w": r[2], "h": r[3], "group": g})
    return out, len(cards), dropped


class _Gray:
    """A QImage seen through the few PIL calls the geometry uses: convert("L"), getpixel, width, height."""
    mode = "L"

    def __init__(self, qimage):
        self.q = qimage
        self.width, self.height = qimage.width(), qimage.height()

    def convert(self, _mode):
        return self

    def getpixel(self, xy):
        x = min(self.width - 1, max(0, int(xy[0])))
        y = min(self.height - 1, max(0, int(xy[1])))
        return qGray(self.q.pixel(x, y))


# ------------------------------------------------------------------ add-on glue

def units_for_slide(lines):
    """The add-on's slide_lines() output (text layer + OCR, image px) -> units."""
    words = [{"t": w["t"], "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"]}
             for ln in lines for w in ln["words"] if w.get("w", 0) > 0 and w.get("h", 0) > 0]
    return units_from_lines(lines_from_words(words))


def _marked(image, units, f):
    """Scaled copy of the slide with each unit outlined and numbered."""
    img = image.convertToFormat(QImage.Format.Format_RGB32)
    if f < 1:
        img = img.scaled(int(image.width() * f), int(image.height() * f),
                         Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    size = max(12, int(img.height() / 60))
    font = QFont("Arial", 1)
    font.setPixelSize(size)
    font.setBold(True)
    p.setFont(font)
    blue = QColor(0, 110, 255, 170)
    for u in units:
        r = QRectF(u["x0"] * f - 2, u["y0"] * f - 2, (u["x1"] - u["x0"]) * f + 4, (u["y1"] - u["y0"]) * f + 4)
        p.setPen(QPen(blue, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)
        tag = u["id"][1:]
        tw = p.fontMetrics().horizontalAdvance(tag)
        tx, ty = max(0.0, r.x() - tw - 4), max(0.0, r.y())
        p.fillRect(QRectF(tx - 2, ty - 1, tw + 4, size + 3), QColor(0, 110, 255, 210))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(tx, ty, tw + 2, size + 2), Qt.AlignmentFlag.AlignLeft, tag)
    p.end()
    return img


def slide_payload(n, image, units):
    """What the server gets for one slide: the marked image and the unit list, both in the
    pixels of the image the model sees (scaled down when the slide is very large)."""
    f = min(1.0, (MAX_PIXELS / max(1, image.width() * image.height())) ** 0.5)
    img = _marked(image, units, f)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPEG", 88)
    return {
        "n": n, "w": img.width(), "h": img.height(), "media_type": "image/jpeg",
        "image": base64.b64encode(bytes(buf.data())).decode(),
        "units": [{"id": u["id"], "x": u["x0"] * f, "y": u["y0"] * f, "w": (u["x1"] - u["x0"]) * f,
                   "h": (u["y1"] - u["y0"]) * f, "text": u["text"]} for u in units],
        "_scale": f,
    }


def boxes_for_page(page_plan, units, image, scale, next_group):
    """One page of the model's answer -> (add-on boxes, cards made, cards dropped)."""
    W, H = image.width(), image.height()
    plan = page_plan if isinstance(page_plan, dict) else {"cards": page_plan or []}
    if plan.get("skip"):
        return [], 0, 0
    # regions come back in the scaled image's pixels
    for c in plan.get("cards", []):
        for t in (c.get("hide") or []) if isinstance(c, dict) else []:
            if isinstance(t, dict) and "region" in t and scale and scale < 1:
                t["region"] = [float(v) / scale for v in t["region"]]
    raw, made, dropped = plan_to_boxes(plan, units, W, H, _Gray(image))
    out, groups = [], {}
    for b in raw:
        groups.setdefault(b["group"], []).append(b)
    for _g, bs in groups.items():
        grouped = len(bs) > 1
        guid = uuid.uuid4().hex if grouped else None
        for b in bs:
            out.append({"x": int(round(b["x"])), "y": int(round(b["y"])), "w": int(round(b["w"])), "h": int(round(b["h"])),
                        "group": next_group if grouped else None, "group_uid": guid,
                        "shape": "rect", "mode": None, "note": "", "id": uuid.uuid4().hex})
        next_group += grouped
    return out, made, dropped
