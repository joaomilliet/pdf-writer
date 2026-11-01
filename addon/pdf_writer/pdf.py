"""A small PDF writer, standard library only: pages, text with inline styles, headings, lists, tables, charts,
callouts and images (JPEG, PNG). render(spec) answers the file's bytes, its page count and its image count.

Lengths in a spec are millimetres; font sizes are points.
"""
from __future__ import annotations

import base64
import math
import re
import struct
import time
import unicodedata
import zlib

MM = 72.0 / 25.4
PAGES = {"A4": (595.28, 841.89), "A5": (419.53, 595.28), "Letter": (612.0, 792.0), "Legal": (612.0, 1008.0)}
PALETTE = ["#2E86AB", "#F18F01", "#C73E1D", "#6A994E", "#A23B72", "#3B1F2B", "#E9C46A", "#5C6B73", "#2A9D8F",
           "#8D6A9F"]
BLOCKS = ("heading", "paragraph", "list", "table", "chart", "image", "callout", "quote", "rule", "spacer",
          "pagebreak")


class PdfError(ValueError):
    pass


# ---- font metrics (the 14 standard fonts need no embedding) ----------------------------------------------------------
_HELV = ([278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 +
         [278, 278, 584, 584, 584, 556, 1015] +
         [667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722,
          667, 944, 667, 667, 611] +
         [278, 278, 278, 469, 556, 333] +
         [556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556,
          500, 722, 500, 500, 500] +
         [334, 260, 334, 584])
_BOLD = ([278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 +
         [333, 333, 584, 584, 584, 611, 975] +
         [722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722,
          667, 944, 667, 667, 611] +
         [333, 278, 333, 584, 556, 333] +
         [556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611,
          556, 778, 556, 556, 500] +
         [389, 280, 389, 584])
_EXTRA_R = {"\u2022": 350, "\u2013": 556, "\u2014": 1000, "\u2018": 222, "\u2019": 222, "\u201c": 333,
            "\u201d": 333, "\u2026": 1000, "\u20ac": 556, "\u00b0": 400, "\u00a9": 737, "\u00ae": 737,
            "\u00b1": 584, "\u00d7": 584, "\u00b7": 278, "\u00ab": 556, "\u00bb": 556, "\u00a0": 278,
            "\u00aa": 370, "\u00ba": 365, "\u00bf": 611, "\u00a1": 333, "\u2122": 1000}
_EXTRA_B = dict(_EXTRA_R, **{"\u201c": 500, "\u201d": 500, "\u2018": 278, "\u2019": 278, "\u00bf": 611})
FONTS = {"r": ("F1", "Helvetica"), "b": ("F2", "Helvetica-Bold"), "i": ("F3", "Helvetica-Oblique"),
         "bi": ("F4", "Helvetica-BoldOblique"), "m": ("F5", "Courier"), "mb": ("F6", "Courier-Bold")}
_BOLDER = {"r": "b", "i": "bi", "m": "mb", "b": "b", "bi": "bi", "mb": "mb"}
_ITALIC = {"r": "i", "b": "bi", "m": "m", "mb": "mb", "i": "i", "bi": "bi"}


LINK_HEX = "1A5FB4"
_SINK = []   # the current page's links: (x1, y1, x2, y2, url), filled by line_ops


def _sp(style: str):
    """A run's style -> (font style, colour hex or '', link url or '')."""
    head, _, url = style.partition("~")
    base, _, hx = head.partition("@")
    return base, hx, url


def char_width(ch: str, style: str) -> int:
    style = _sp(style)[0]
    if style in ("m", "mb"):
        return 600
    bold = style in ("b", "bi")
    o = ord(ch)
    if 32 <= o <= 126:
        return (_BOLD if bold else _HELV)[o - 32]
    extra = _EXTRA_B if bold else _EXTRA_R
    if ch in extra:
        return extra[ch]
    base = unicodedata.normalize("NFD", ch)[:1]
    if base and base != ch and 32 <= ord(base) <= 126:
        return (_BOLD if bold else _HELV)[ord(base) - 32]
    return 556


def text_width(s: str, style: str, size: float) -> float:
    return sum(char_width(c, style) for c in s) * size / 1000.0


def _clean(s: str) -> str:
    """Characters the WinAnsi encoding has stay; others become their base letter, else '?'."""
    out = []
    for ch in s.replace("\t", "    ").replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n":
            out.append(ch)
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            base = unicodedata.normalize("NFD", ch)[:1]
            try:
                base.encode("cp1252")
                out.append(base if base != ch else "?")
            except UnicodeEncodeError:
                out.append("?")
    return "".join(out)


def _pdfstr(s: str) -> str:
    b = s.encode("cp1252", "replace")
    b = b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\n", b" ")
    return "(" + b.decode("latin-1") + ")"


# ---- colours ---------------------------------------------------------------------------------------------------------
_NAMED = {"black": "#000000", "white": "#FFFFFF", "gray": "#808080", "grey": "#808080", "red": "#C73E1D",
          "green": "#6A994E", "blue": "#2E86AB", "orange": "#F18F01", "purple": "#A23B72", "yellow": "#E9C46A",
          "teal": "#2A9D8F", "violet": "#8D6A9F"}


def color(c, default):
    if c is None or c == "":
        return default if isinstance(default, tuple) else color(default, (0, 0, 0))
    s = _NAMED.get(str(c).strip().lower(), str(c).strip())
    m = re.match(r"^#?([0-9a-fA-F]{6})$", s)
    if not m:
        raise PdfError("colour %r is not #RRGGBB or one of %s" % (c, ", ".join(sorted(_NAMED))))
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def tint(c, k):
    return tuple(1 - (1 - v) * k for v in c)


def _rg(c):
    return "%.3f %.3f %.3f rg" % tuple(c)


def _RG(c):
    return "%.3f %.3f %.3f RG" % tuple(c)


WHITE = (1.0, 1.0, 1.0)
GRID = (0.86, 0.86, 0.86)


# ---- inline text: **bold**, *italic*, `mono`, \ escapes ----------------------------------------------------------------
_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|mailto:)[^)\s]+)\)")
_COLOR_TAG = re.compile(r"\{(#?[0-9a-fA-F]{6}|[a-zA-Z]+|/)\}")


def _tag_hex(tag: str):
    s = _NAMED.get(tag.lower(), tag)
    m = re.match(r"^#?([0-9a-fA-F]{6})$", s)
    return m.group(1).upper() if m else None


def parse_inline(text) -> list:
    """Runs of (text, style): style is r, b, i, bi, m or mb, with '@RRGGBB' added for a coloured run."""
    s = _clean("" if text is None else str(text))
    runs = []
    buf = []
    state = {"b": False, "i": False}
    colors = []

    def tail():
        return ("@" + colors[-1]) if colors else ""

    def flush():
        if buf:
            st = ("b" if state["b"] else "") + ("i" if state["i"] else "")
            runs.append(("".join(buf), (st or "r") + tail()))
            del buf[:]

    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            buf.append(s[i + 1])
            i += 2
            continue
        if c == "[":
            m = _LINK.match(s, i)
            if m:
                flush()
                url = m.group(2)
                for t, st in parse_inline(m.group(1)):
                    base, hx, _ = _sp(st)
                    if state["b"]:
                        base = _BOLDER[base]
                    if state["i"]:
                        base = _ITALIC[base]
                    hx = hx or (colors[-1] if colors else LINK_HEX)
                    runs.append((t, "%s@%s~%s" % (base, hx, url)))
                i = m.end()
                continue
        if c == "{":
            m = _COLOR_TAG.match(s, i)
            if m:
                tag = m.group(1)
                if tag == "/" and colors:
                    flush()
                    colors.pop()
                    i = m.end()
                    continue
                hx = _tag_hex(tag) if tag != "/" else None
                if hx:
                    flush()
                    colors.append(hx)
                    i = m.end()
                    continue
        if s.startswith("**", i):
            flush()
            state["b"] = not state["b"]
            i += 2
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j > i:
                flush()
                runs.append((s[i + 1:j], ("mb" if state["b"] else "m") + tail()))
                i = j + 1
                continue
        if c == "*":
            opening = not state["i"] and i + 1 < n and not s[i + 1].isspace()
            closing = state["i"] and i > 0 and not s[i - 1].isspace()
            if opening or closing:
                flush()
                state["i"] = not state["i"]
                i += 1
                continue
        buf.append(c)
        i += 1
    flush()
    return runs


def restyle(runs, table) -> list:
    out = []
    for t, s in runs:
        base, hx, url = _sp(s)
        out.append((t, table[base] + ("@" + hx if hx else "") + ("~" + url if url else "")))
    return out


def plain(text) -> str:
    return "".join(t for t, _ in parse_inline(text))


def wrap(runs, width: float, size: float) -> list:
    """Lines of [(text, style)] that fit `width` at `size`; each line comes with its width."""
    lines = []
    cur = []
    acc = [0.0]

    def push():
        while cur and cur[-1][0].isspace():
            acc[0] -= text_width(cur[-1][0], cur[-1][1], size)
            cur.pop()
        lines.append((list(cur), max(acc[0], 0.0)))
        del cur[:]
        acc[0] = 0.0

    def add_word(tok, style):
        w = text_width(tok, style, size)
        if acc[0] + w <= width + 0.01 or not cur:
            if w > width + 0.01:
                piece = ""
                for ch in tok:
                    if text_width(piece + ch, style, size) > width and piece:
                        if cur:
                            push()
                        cur.append((piece, style))
                        acc[0] = text_width(piece, style, size)
                        push()
                        piece = ch
                    else:
                        piece += ch
                if piece:
                    cur.append((piece, style))
                    acc[0] += text_width(piece, style, size)
                return
            cur.append((tok, style))
            acc[0] += w
        else:
            push()
            add_word(tok, style)

    for text, style in runs:
        for tok in re.findall(r"\n| +|[^ \n]+", text):
            if tok == "\n":
                push()
            elif tok.startswith(" "):
                if cur:
                    cur.append((tok, style))
                    acc[0] += text_width(tok, style, size)
            else:
                add_word(tok, style)
    if cur or not lines:
        push()
    return lines


def _merge(pieces):
    out = []
    for t, s in pieces:
        if out and out[-1][1] == s:
            out[-1] = (out[-1][0] + t, s)
        else:
            out.append((t, s))
    return out


def line_ops(line, x: float, y: float, size: float, col, align: str = "left", width: float = 0.0,
             justify: bool = False) -> str:
    pieces, w = line
    if not pieces:
        return ""
    if align == "center":
        x += (width - w) / 2.0
    elif align == "right":
        x += width - w
    ops = [_rg(col), "BT", "%.2f %.2f Td" % (x, y)]
    tw = 0.0
    if justify:
        spaces = sum(t.count(" ") for t, _ in pieces)
        if spaces:
            tw = (width - w) / spaces
            if tw > size * 0.6:
                tw = 0.0
    if tw:
        ops.append("%.3f Tw" % tw)
    painted = None
    cx = x
    unders = []
    for t, s in _merge(pieces):
        base, hx, url = _sp(s)
        if hx != painted:
            if hx or painted:
                ops.append(_rg(color("#" + hx, col) if hx else col))
            painted = hx
        ops.append("/%s %.2f Tf %s Tj" % (FONTS[base][0], size, _pdfstr(t)))
        pw = text_width(t, s, size) + tw * t.count(" ")
        if url:
            lead = len(t) - len(t.lstrip(" "))
            x1 = cx + text_width(t[:lead], s, size) + tw * lead
            x2 = cx + pw - (text_width(t[len(t.rstrip(" ")):], s, size) if t.strip() else 0)
            if x2 > x1:
                _SINK.append((x1, y - size * 0.22, x2, y + size * 0.82, url))
                unders.append((x1, x2, color("#" + hx, col) if hx else col))
        cx += pw
    if tw:
        ops.append("0 Tw")
    ops.append("ET")
    for x1, x2, c in unders:
        ops.append("%s %.2f w %.2f %.2f m %.2f %.2f l S" % (_RG(c), max(0.4, size * 0.05), x1, y - size * 0.12,
                                                           x2, y - size * 0.12))
    return " ".join(ops)


def _fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return "%d" % round(v)
    return ("%.2f" % v).rstrip("0").rstrip(".")


def _nice(x: float) -> float:
    if x <= 0:
        return 1.0
    e = math.floor(math.log10(x))
    f = x / 10 ** e
    nf = 1 if f <= 1 else 2 if f <= 2 else 2.5 if f <= 2.5 else 5 if f <= 5 else 10
    return nf * 10 ** e


def _scale(lo: float, hi: float, n: int = 5):
    if hi == lo:
        hi = lo + 1
    step = _nice((hi - lo) / float(n))
    a = math.floor(lo / step + 1e-9) * step
    z = math.ceil(hi / step - 1e-9) * step
    if z <= a:
        z = a + step
    ticks = []
    t = a
    while t <= z + step * 1e-6:
        ticks.append(round(t, 10))
        t += step
    return a, z, ticks


def _arc(cx, cy, r, a0, a1) -> list:
    ops = []
    n = max(1, int(math.ceil(abs(a1 - a0) / (math.pi / 2))))
    d = (a1 - a0) / n
    for i in range(n):
        s = a0 + i * d
        e = s + d
        k = 4.0 / 3.0 * math.tan(d / 4.0) * r
        x1, y1 = cx + r * math.cos(s), cy + r * math.sin(s)
        x2, y2 = cx + r * math.cos(e), cy + r * math.sin(e)
        ops.append("%.2f %.2f %.2f %.2f %.2f %.2f c" % (x1 - k * math.sin(s), y1 + k * math.cos(s),
                                                       x2 + k * math.sin(e), y2 - k * math.cos(e), x2, y2))
    return ops


# ---- images ----------------------------------------------------------------------------------------------------------
def _read_image(b) -> bytes:
    if b.get("path"):
        path = str(b["path"])
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise PdfError("no image file at %s" % path)
        except PermissionError:
            raise PdfError("the image %s cannot be read from the add-on's sandbox: it must sit in Documents, Desktop, "
                           "Downloads, Music, Movies or on another drive" % path)
        except OSError as e:
            raise PdfError("the image %s cannot be read (%s)" % (path, e.strerror or e))
    if b.get("data"):
        data = str(b["data"])
        if data.startswith("data:"):
            data = data.split(",", 1)[-1]
        try:
            return base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            raise PdfError("`data` is not base64")
    raise PdfError("an image needs `path` (an absolute path) or `data` (base64)")


def _jpeg(raw: bytes):
    i = 2
    n = len(raw)
    while i + 9 < n:
        if raw[i] != 0xFF:
            i += 1
            continue
        m = raw[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        ln = struct.unpack(">H", raw[i + 2:i + 4])[0]
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            bpc = raw[i + 4]
            h, w = struct.unpack(">HH", raw[i + 5:i + 9])
            nc = raw[i + 9]
            cs = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(nc)
            if not cs or not w or not h:
                raise PdfError("this JPEG's colour layout is not supported")
            return w, h, "/Width %d /Height %d /ColorSpace %s /BitsPerComponent %d /Filter /DCTDecode" % (
                w, h, cs, bpc), raw, None
        i += 2 + ln
    raise PdfError("the JPEG has no frame header")


def _unfilter(data: bytes, w: int, h: int, bpp: int) -> bytearray:
    stride = w * bpp
    out = bytearray(stride * h)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ft = data[pos]
        pos += 1
        line = bytearray(data[pos:pos + stride])
        pos += stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


def _png(raw: bytes):
    pos = 8
    ihdr = None
    plte = b""
    idat = []
    while pos + 8 <= len(raw):
        ln, typ = struct.unpack(">I4s", raw[pos:pos + 8])
        data = raw[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            ihdr = data
        elif typ == b"PLTE":
            plte = data
        elif typ == b"IDAT":
            idat.append(data)
        elif typ == b"IEND":
            break
    if not ihdr or not idat:
        raise PdfError("the PNG is incomplete")
    w, h, bd, ct, _, _, inter = struct.unpack(">IIBBBBB", ihdr[:13])
    if inter:
        raise PdfError("interlaced PNG is not supported: save it without interlacing")
    z = b"".join(idat)
    if ct in (0, 2, 3):
        colors = {0: 1, 2: 3, 3: 1}[ct]
        if ct == 3:
            if not plte:
                raise PdfError("the PNG has no palette")
            cs = "[/Indexed /DeviceRGB %d <%s>]" % (len(plte) // 3 - 1, plte.hex())
        else:
            cs = "/DeviceGray" if ct == 0 else "/DeviceRGB"
        d = ("/Width %d /Height %d /ColorSpace %s /BitsPerComponent %d /Filter /FlateDecode /DecodeParms "
             "<< /Predictor 15 /Colors %d /BitsPerComponent %d /Columns %d >>" % (w, h, cs, bd, colors, bd, w))
        return w, h, d, z, None
    if ct in (4, 6):
        if bd != 8:
            raise PdfError("a PNG with transparency must have 8 bits per channel")
        ch = 2 if ct == 4 else 4
        try:
            px = _unfilter(zlib.decompress(z), w, h, ch)
        except (zlib.error, IndexError):
            raise PdfError("the PNG's pixel data is damaged")
        alpha = bytes(px[ch - 1::ch])
        if ch == 2:
            col = bytes(px[0::2])
            cs = "/DeviceGray"
        else:
            c3 = bytearray(w * h * 3)
            c3[0::3] = px[0::4]
            c3[1::3] = px[1::4]
            c3[2::3] = px[2::4]
            col = bytes(c3)
            cs = "/DeviceRGB"
        d = "/Width %d /Height %d /ColorSpace %s /BitsPerComponent 8 /Filter /FlateDecode" % (w, h, cs)
        sm = ("/Width %d /Height %d /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode" % (w, h),
              zlib.compress(alpha))
        return w, h, d, zlib.compress(col), sm
    raise PdfError("PNG colour type %d is not supported" % ct)


def load_image(b):
    raw = _read_image(b)
    if raw[:2] == b"\xff\xd8":
        return _jpeg(raw)
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return _png(raw)
    raise PdfError("the image must be a JPEG or a PNG")


def _num(v, what):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise PdfError("%s must be a number, not %r" % (what, v))
    if math.isnan(f) or math.isinf(f):
        raise PdfError("%s must be a finite number" % what)
    return f


# ---- the document ----------------------------------------------------------------------------------------------------
class Doc:
    def __init__(self, spec: dict):
        page = spec.get("page") or {}
        size = str(page.get("size") or "A4")
        if size not in PAGES:
            raise PdfError("page.size must be one of %s" % ", ".join(PAGES))
        w, h = PAGES[size]
        if page.get("orientation") == "landscape":
            w, h = h, w
        m = page.get("margin_mm", 18)
        if isinstance(m, (list, tuple)):
            if len(m) != 4:
                raise PdfError("page.margin_mm is one number or [top, right, bottom, left]")
            mt, mr, mb, ml = [_num(x, "page.margin_mm") * MM for x in m]
        else:
            mt = mr = mb = ml = _num(m, "page.margin_mm") * MM
        self.W, self.H, self.mt, self.mr, self.mb, self.ml = w, h, mt, mr, mb, ml
        self.cw = w - ml - mr
        if self.cw < 100 or h - mt - mb < 100:
            raise PdfError("the margins leave no room on the page")
        theme = spec.get("theme") or {}
        self.accent = color(theme.get("accent"), "#2E86AB")
        self.text = color(theme.get("text"), "#222222")
        self.muted = color(theme.get("muted"), "#6B6B6B")
        self.base = _num(theme.get("size", 10.5), "theme.size")
        self.pages = []
        self.ops = []
        self.y = 0.0
        self.images = []
        self.links = []
        self.new_page()

    # pages and space
    def new_page(self):
        global _SINK
        self.ops = []
        self.pages.append(self.ops)
        _SINK = []
        self.links.append(_SINK)
        self.y = self.mt

    def room(self) -> float:
        return self.H - self.mb - self.y

    def at_top(self) -> bool:
        return self.y <= self.mt + 0.5

    def ensure(self, h: float):
        if h > self.room() and not self.at_top():
            self.new_page()

    def py(self, y: float) -> float:
        return self.H - y

    def rect(self, x, y, w, h, fill):
        self.ops.append("%s %.2f %.2f %.2f %.2f re f" % (_rg(fill), x, self.py(y + h), w, h))

    def hline(self, x1, x2, y, col, width=0.5):
        self.ops.append("%s %.2f w %.2f %.2f m %.2f %.2f l S" % (_RG(col), width, x1, self.py(y), x2, self.py(y)))

    def vline(self, x, y1, y2, col, width=0.5):
        self.ops.append("%s %.2f w %.2f %.2f m %.2f %.2f l S" % (_RG(col), width, x, self.py(y1), x, self.py(y2)))

    @staticmethod
    def baseline(top, lh, size):
        return top + (lh - size) / 2.0 + 0.78 * size

    def text_block(self, runs, size, col, align="left", indent=0.0, leading=1.38, width=None):
        width = (self.cw - indent) if width is None else width
        lines = wrap(runs, width, size)
        lh = size * leading
        for k, ln in enumerate(lines):
            self.ensure(lh)
            just = align == "justify" and k < len(lines) - 1
            self.ops.append(line_ops(ln, self.ml + indent, self.py(self.baseline(self.y, lh, size)), size, col,
                                     "left" if align == "justify" else align, width, just))
            self.y += lh

    def label(self, ops, s, x, y_pdf, size, style, col, align="left", maxw=None):
        s = _clean(str(s))
        if maxw is not None and text_width(s, style, size) > maxw:
            while s and text_width(s + "\u2026", style, size) > maxw:
                s = s[:-1]
            s += "\u2026"
        w = text_width(s, style, size)
        if align == "center":
            x -= w / 2.0
        elif align == "right":
            x -= w
        ops.append("%s BT /%s %.2f Tf %.2f %.2f Td %s Tj ET" % (_rg(col), FONTS[style][0], size, x, y_pdf, _pdfstr(s)))

    # the title block
    def title_block(self, spec):
        if not spec.get("title"):
            return
        self.text_block(restyle(parse_inline(spec["title"]), _BOLDER), 24, self.accent, leading=1.2)
        if spec.get("subtitle"):
            self.y += 2
            self.text_block(parse_inline(spec["subtitle"]), 13, self.muted, leading=1.3)
        meta = " \u00b7 ".join(str(x) for x in (spec.get("author"), spec.get("date")) if x)
        if meta:
            self.y += 3
            self.text_block(parse_inline(meta), 9.5, self.muted)
        self.y += 6
        self.hline(self.ml, self.ml + self.cw, self.y, self.accent, 1.5)
        self.y += 14

    # blocks
    def b_heading(self, b):
        lvl = int(_num(b.get("level", 1), "level"))
        if lvl < 1 or lvl > 4:
            raise PdfError("level is 1 to 4")
        sz = {1: 18.0, 2: 14.5, 3: 12.0, 4: 10.5}[lvl]
        col = color(b.get("color"), self.accent if lvl <= 2 else self.text)
        self.ensure(sz * 1.3 + 50)
        if not self.at_top():
            self.y += sz * 0.8
        self.text_block(restyle(parse_inline(b.get("text", "")), _BOLDER), sz, col, b.get("align", "left"), leading=1.25)
        if lvl == 1:
            self.y += 2
            self.hline(self.ml, self.ml + self.cw, self.y, tint(col, 0.35), 0.8)
            self.y += 4
        self.y += sz * 0.3

    def b_paragraph(self, b):
        size = _num(b.get("size", self.base), "size")
        col = color(b.get("color"), self.text)
        runs = parse_inline(b.get("text", ""))
        if b.get("style") == "bold":
            runs = restyle(runs, _BOLDER)
        elif b.get("style") == "italic":
            runs = restyle(runs, _ITALIC)
        align = b.get("align", "left")
        if align not in ("left", "center", "right", "justify"):
            raise PdfError("align is left, center, right or justify")
        self.text_block(runs, size, col, align)
        self.y += size * 0.6

    def _list(self, items, ordered, level, size):
        if not isinstance(items, list):
            raise PdfError("items must be a list")
        lh = size * 1.38
        for n, it in enumerate(items, 1):
            if isinstance(it, dict):
                text, sub, sub_ordered = it.get("text", ""), it.get("items"), it.get("ordered", ordered)
            else:
                text, sub, sub_ordered = it, None, ordered
            indent = 16 + level * 16
            mark = ("%d." % n) if ordered else ["\u2022", "\u2013", "\u00b7"][level % 3]
            self.ensure(lh)
            mw = text_width(mark, "r", size)
            self.label(self.ops, mark, self.ml + indent - 5 - mw, self.py(self.baseline(self.y, lh, size)), size,
                       "r", self.accent if not ordered else self.text)
            self.text_block(parse_inline(text), size, self.text, indent=indent)
            self.y += size * 0.12
            if sub:
                self._list(sub, sub_ordered, level + 1, size)

    def b_list(self, b):
        size = _num(b.get("size", self.base), "size")
        self._list(b.get("items") or [], bool(b.get("ordered")), 0, size)
        self.y += size * 0.5

    def b_table(self, b):
        cols = ["" if c is None else str(c) for c in (b.get("columns") or [])]
        rows = b.get("rows") or []
        if not isinstance(rows, list) or any(not isinstance(r, list) for r in rows):
            raise PdfError("rows must be a list of lists")
        foot = b.get("footer")
        ncol = len(cols) or max([len(r) for r in rows] + [len(foot or [])] + [0])
        if not ncol:
            raise PdfError("a table needs columns or rows")

        def norm(r):
            r = ["" if v is None else (_fmt(v) if isinstance(v, float) else str(v)) for v in r]
            return (r + [""] * ncol)[:ncol]

        body = [norm(r) for r in rows]
        foot = norm(foot) if foot else None
        size = _num(b.get("size", self.base - 1.5), "size")
        pad, lead = 4.0, size * 1.3
        if b.get("widths"):
            rel = [_num(x, "widths") for x in b["widths"]]
            if len(rel) != ncol or min(rel) <= 0:
                raise PdfError("widths needs %d positive numbers, one per column" % ncol)
        else:
            rel = []
            for c in range(ncol):
                cells = ([cols[c]] if cols else []) + [r[c] for r in body] + ([foot[c]] if foot else [])
                nat = max([text_width(plain(x), "b", size) for x in cells] + [10]) + 2 * pad
                rel.append(min(nat, self.cw * 0.45) + 8)
        tot = sum(rel)
        widths = [r / tot * self.cw for r in rel]
        num_re = re.compile(r"^[\s$\u20acR\u00a3+\-\u2212(]*[\d][\d.,:\s]*\)?\s*(%|dB|dBFS|dBTP|LUFS|LU|s|ms|Hz|kHz|x)?\s*$")
        aligns = b.get("align")
        if aligns:
            if len(aligns) != ncol:
                raise PdfError("align needs %d entries, one per column" % ncol)
        else:
            aligns = []
            for c in range(ncol):
                vals = [plain(r[c]) for r in body if plain(r[c]).strip()]
                aligns.append("right" if vals and all(num_re.match(v) for v in vals) else "left")
        head_bg = color(b.get("headerColor"), self.accent)
        zebra = b.get("zebra", True)
        grid = bool(b.get("grid"))

        def lay(row, style):
            cells = []
            for c, v in enumerate(row):
                runs = parse_inline(v)
                if style:
                    runs = restyle(runs, style)
                cells.append(wrap(runs, widths[c] - 2 * pad, size))
            return cells, max(len(x) for x in cells) * lead + 2 * pad

        def draw(layout, kind, idx):
            cells, hgt = layout
            y0 = self.y
            if kind == "head":
                self.rect(self.ml, y0, self.cw, hgt, head_bg)
            elif kind == "body" and zebra and idx % 2 == 1:
                self.rect(self.ml, y0, self.cw, hgt, tint(self.accent, 0.07))
            x = self.ml
            col = WHITE if kind == "head" else self.text
            for c, lines in enumerate(cells):
                for k, ln in enumerate(lines):
                    base = self.baseline(y0 + pad + k * lead, lead, size)
                    self.ops.append(line_ops(ln, x + pad, self.py(base), size, col, aligns[c], widths[c] - 2 * pad))
                if grid and c:
                    self.vline(x, y0, y0 + hgt, GRID)
                x += widths[c]
            if kind == "foot":
                self.hline(self.ml, self.ml + self.cw, y0, self.text, 0.8)
            elif kind == "body":
                self.hline(self.ml, self.ml + self.cw, y0 + hgt, GRID, 0.5)
            self.y = y0 + hgt

        head = lay(cols, _BOLDER) if cols else None
        first = head[1] if head else 0
        first += lay(body[0], None)[1] if body else 0
        self.ensure(first)
        top = self.y
        if head:
            draw(head, "head", 0)
        for i, r in enumerate(body):
            layout = lay(r, None)
            if layout[1] > self.room() and not self.at_top():
                if grid:
                    self.ops.append("%s 0.5 w %.2f %.2f %.2f %.2f re S" % (_RG(GRID), self.ml, self.py(self.y),
                                                                         self.cw, self.y - top))
                self.new_page()
                top = self.y
                if head:
                    draw(head, "head", 0)
            draw(layout, "body", i)
        if foot:
            layout = lay(foot, _BOLDER)
            self.ensure(layout[1])
            draw(layout, "foot", 0)
        if grid:
            self.ops.append("%s 0.5 w %.2f %.2f %.2f %.2f re S" % (_RG(GRID), self.ml, self.py(self.y), self.cw,
                                                                 self.y - top))
        if b.get("caption"):
            self.y += 3
            self.text_block(restyle(parse_inline(b["caption"]), _ITALIC), self.base - 2, self.muted)
        self.y += 10

    def _legend(self, ops, items, x0, w, y_top, size=8.0):
        """Draws swatch + name items in rows from y_top (doc coords); answers the height used."""
        rows, cur, cw = [], [], 0.0
        for name, col in items:
            iw = 10 + text_width(_clean(name), "r", size) + 12
            if cur and cw + iw > w:
                rows.append((cur, cw))
                cur, cw = [], 0.0
            cur.append((name, col, iw))
            cw += iw
        if cur:
            rows.append((cur, cw))
        y = y_top
        for items_, rw in rows:
            x = x0 + (w - rw) / 2.0
            for name, col, iw in items_:
                ops.append("%s %.2f %.2f 7 7 re f" % (_rg(col), x, self.py(y + 9)))
                self.label(ops, name, x + 10, self.py(y + 8.5), size, "r", self.text)
                x += iw
            y += 12
        return y - y_top

    def b_chart(self, b):
        kind = b.get("kind", "bar")
        if kind not in ("bar", "hbar", "line", "area", "pie", "donut"):
            raise PdfError("kind is bar, hbar, line, area, pie or donut")
        labels = [_clean(str(x)) for x in (b.get("labels") or [])]
        series = b.get("series")
        if series is None and b.get("values") is not None:
            series = [{"name": b.get("name", ""), "values": b["values"]}]
        if not series or not isinstance(series, list):
            raise PdfError("a chart needs series [{name, values}] or values")
        ser = []
        for j, s in enumerate(series):
            if not isinstance(s, dict) or not isinstance(s.get("values"), list):
                raise PdfError("series %d needs values (a list of numbers)" % (j + 1))
            vals = [_num(v, "series %d values" % (j + 1)) for v in s["values"]]
            if labels and len(vals) != len(labels):
                raise PdfError("series %d has %d values for %d labels" % (j + 1, len(vals), len(labels)))
            ser.append((str(s.get("name") or ""), vals, color(s.get("color"), PALETTE[j % len(PALETTE)])))
        n = len(ser[0][1])
        if not n or any(len(v) != n for _, v, _ in ser):
            raise PdfError("every series needs the same, non-zero number of values")
        if not labels:
            labels = [str(i + 1) for i in range(n)]
        h = _num(b.get("height_mm", 70), "height_mm") * MM
        w = self.cw * min(1.0, max(0.2, _num(b.get("width", 1), "width")))
        title = b.get("title")
        pie = kind in ("pie", "donut")
        legend_items = ([(labels[i], color(None, PALETTE[i % len(PALETTE)])) for i in range(n)] if pie else
                        [(nm, c) for nm, _, c in ser if nm])
        stacked = bool(b.get("stacked")) and kind in ("bar", "hbar")
        need = h + (18 if title else 0) + (0 if pie else (14 * max(1, len(legend_items) // 5) if legend_items else 0)) + 10
        self.ensure(need)
        x0 = self.ml + (self.cw - w) / 2.0
        y = self.y
        ops = ["q", "1 j 1 J"]
        if title:
            self.label(ops, title, x0 + w / 2.0, self.py(y + 11), 10.5, "b", self.text, "center", w)
            y += 18
        ts = 7.5
        if pie:
            vals = ser[0][1]
            if min(vals) < 0 or sum(vals) <= 0:
                raise PdfError("a pie needs values of zero or more, not all zero")
            tot = sum(vals)
            r = min(h / 2.0 - 4, w * 0.25)
            cx, cy = x0 + r + 12, self.py(y + h / 2.0)
            a = math.pi / 2
            for i, v in enumerate(vals):
                if v <= 0:
                    continue
                a1 = a - v / tot * 2 * math.pi
                col = color(None, PALETTE[i % len(PALETTE)])
                ops.append("%s %s 0.8 w %.2f %.2f m %.2f %.2f l %s h B" % (
                    _rg(col), _RG(WHITE), cx, cy, cx + r * math.cos(a), cy + r * math.sin(a), " ".join(_arc(cx, cy, r, a, a1))))
                a = a1
            if kind == "donut":
                ri = r * 0.55
                ops.append("%s %.2f %.2f m %s f" % (_rg(WHITE), cx + ri, cy, " ".join(_arc(cx, cy, ri, 0, 2 * math.pi))))
                self.label(ops, _fmt(tot), cx, cy - 4, 11, "b", self.text, "center")
            lx = cx + r + 24
            lw = x0 + w - lx
            ly = y + h / 2.0 - len(vals) * 7
            for i, v in enumerate(vals):
                col = color(None, PALETTE[i % len(PALETTE)])
                ops.append("%s %.2f %.2f 8 8 re f" % (_rg(col), lx, self.py(ly + 10)))
                pct = 100.0 * v / tot
                self.label(ops, "%s  %s (%s%%)" % (labels[i], _fmt(v), _fmt(round(pct, 1))), lx + 13, self.py(ly + 9),
                           8.5, "r", self.text, "left", lw - 14)
                ly += 14
            ops.append("Q")
            self.ops.append(" ".join(ops))
            self.y = y + h + 10
            return

        allv = [v for _, vals, _ in ser for v in vals]
        if stacked:
            pos = [sum(max(0.0, s[1][i]) for s in ser) for i in range(n)]
            neg = [sum(min(0.0, s[1][i]) for s in ser) for i in range(n)]
            vmin, vmax = min(neg + [0.0]), max(pos + [0.0])
        else:
            vmin, vmax = min(allv + [0.0]), max(allv + [0.0])
        if b.get("min") is not None:
            vmin = _num(b["min"], "min")
        if b.get("max") is not None:
            vmax = _num(b["max"], "max")
        lo, hi, ticks = _scale(vmin, vmax)
        unit = str(b.get("unit") or "")
        tick_lbl = [_fmt(t) + unit for t in ticks]
        pt, pb = y + 4, y + h - 16
        if kind == "hbar":
            lw = min(max(text_width(l, "r", ts) for l in labels) + 8, w * 0.35)
            pl, pr = x0 + lw, x0 + w - 8
            X = lambda v: pl + (v - lo) / (hi - lo) * (pr - pl)
            for t, tl in zip(ticks, tick_lbl):
                xx = X(t)
                ops.append("%s 0.5 w %.2f %.2f m %.2f %.2f l S" % (_RG(GRID), xx, self.py(pt), xx, self.py(pb)))
                self.label(ops, tl, xx, self.py(pb + 10), ts, "r", self.muted, "center")
            gh = (pb - pt) / n
            ns = 1 if stacked else len(ser)
            bh = gh * 0.72 / ns
            for i in range(n):
                gy = pt + i * gh
                self.label(ops, labels[i], pl - 4, self.py(gy + gh / 2.0 + ts * 0.35), ts, "r", self.text, "right", lw - 6)
                accp = accn = 0.0
                for j, (_, vals, col) in enumerate(ser):
                    v = vals[i]
                    if stacked:
                        if v >= 0:
                            a0, a1 = accp, accp + v
                            accp += v
                        else:
                            a0, a1 = accn + v, accn
                            accn += v
                        by = gy + gh * 0.14
                    else:
                        a0, a1 = min(0.0, v), max(0.0, v)
                        by = gy + gh * 0.14 + j * bh
                    xa, xb = X(a0), X(a1)
                    ops.append("%s %.2f %.2f %.2f %.2f re f" % (_rg(col), xa, self.py(by + bh * 0.9), xb - xa, bh * 0.9))
                    if b.get("valueLabels") and not stacked:
                        self.label(ops, _fmt(v) + unit, xb + 3, self.py(by + bh * 0.45 + 2.5), 6.5, "r", self.muted)
            ops.append("%s 0.8 w %.2f %.2f m %.2f %.2f l S" % (_RG(self.muted), X(0) if lo <= 0 <= hi else pl,
                                                               self.py(pt), X(0) if lo <= 0 <= hi else pl, self.py(pb)))
        else:
            lw = max(text_width(t, "r", ts) for t in tick_lbl) + 6
            pl, pr = x0 + lw, x0 + w - 4
            Y = lambda v: pb - (v - lo) / (hi - lo) * (pb - pt)
            for t, tl in zip(ticks, tick_lbl):
                yy = Y(t)
                ops.append("%s 0.5 w %.2f %.2f m %.2f %.2f l S" % (_RG(GRID), pl, self.py(yy), pr, self.py(yy)))
                self.label(ops, tl, pl - 4, self.py(yy) - ts * 0.35, ts, "r", self.muted, "right")
            gw = (pr - pl) / n
            step = max(1, int(math.ceil(max(text_width(l, "r", ts) for l in labels) / max(gw - 2, 1))))
            for i in range(n):
                if i % step == 0:
                    self.label(ops, labels[i], pl + gw * (i + 0.5), self.py(pb + 10), ts, "r", self.text, "center",
                               gw * step - 2)
            if kind == "bar":
                ns = 1 if stacked else len(ser)
                bw = gw * 0.72 / ns
                for i in range(n):
                    gx = pl + i * gw
                    accp = accn = 0.0
                    for j, (_, vals, col) in enumerate(ser):
                        v = vals[i]
                        if stacked:
                            if v >= 0:
                                a0, a1 = accp, accp + v
                                accp += v
                            else:
                                a0, a1 = accn + v, accn
                                accn += v
                            bx = gx + gw * 0.14
                        else:
                            a0, a1 = min(0.0, v), max(0.0, v)
                            bx = gx + gw * 0.14 + j * bw
                        ya, yb = Y(a1), Y(a0)
                        ops.append("%s %.2f %.2f %.2f %.2f re f" % (_rg(col), bx, self.py(yb), bw * 0.9, yb - ya))
                        if b.get("valueLabels") and not stacked:
                            self.label(ops, _fmt(v) + unit, bx + bw * 0.45, self.py(ya - 3), 6.5, "r", self.muted,
                                       "center")
            else:
                for j, (_, vals, col) in enumerate(ser):
                    pts = [(pl + gw * (i + 0.5), self.py(Y(v))) for i, v in enumerate(vals)]
                    if kind == "area":
                        base = self.py(Y(max(lo, min(0.0, hi))))
                        path = "%.2f %.2f m " % (pts[0][0], base) + " ".join("%.2f %.2f l" % p for p in pts) + \
                            " %.2f %.2f l h" % (pts[-1][0], base)
                        ops.append("%s %s f" % (_rg(tint(col, 0.30)), path))
                    ops.append("%s 1.6 w %s S" % (_RG(col), "%.2f %.2f m " % pts[0] + " ".join("%.2f %.2f l" % p
                                                                                             for p in pts[1:])))
                    if n <= 40:
                        for px, pyy in pts:
                            ops.append("%s %.2f %.2f 3.2 3.2 re f" % (_rg(col), px - 1.6, pyy - 1.6))
                    if b.get("valueLabels"):
                        for (px, pyy), v in zip(pts, vals):
                            self.label(ops, _fmt(v) + unit, px, pyy + 5, 6.5, "r", self.muted, "center")
            zero = Y(0) if lo <= 0 <= hi else pb
            ops.append("%s 0.8 w %.2f %.2f m %.2f %.2f l S" % (_RG(self.muted), pl, self.py(zero), pr, self.py(zero)))
        ylab = b.get("yLabel")
        if ylab:
            self.label(ops, ylab, x0, self.py(y - 2), 7, "i", self.muted)
        legend_h = 0.0
        if legend_items and (len(ser) > 1 or b.get("legend")):
            legend_h = self._legend(ops, legend_items, x0, w, y + h + 2)
        ops.append("Q")
        self.ops.append(" ".join(ops))
        self.y = y + h + legend_h + 10
        if b.get("caption"):
            self.text_block(restyle(parse_inline(b["caption"]), _ITALIC), self.base - 2, self.muted, "center")
            self.y += 6

    def b_image(self, b):
        wpx, hpx, d, data, sm = load_image(b)
        name = "Im%d" % (len(self.images) + 1)
        self.images.append((name, d, data, sm))
        wv = b.get("width")
        if wv is None:
            wpt = min(wpx * 0.75, self.cw)
        elif isinstance(wv, str) and wv.strip().endswith("%"):
            wpt = self.cw * min(100.0, _num(wv.strip()[:-1], "width")) / 100.0
        else:
            wpt = min(_num(wv, "width") * MM, self.cw)
        hpt = wpt * hpx / float(wpx)
        maxh = self.H - self.mt - self.mb - 30
        if hpt > maxh:
            hpt = maxh
            wpt = hpt * wpx / float(hpx)
        cap = b.get("caption")
        self.ensure(hpt + (self.base * 1.6 if cap else 0) + 4)
        align = b.get("align", "center")
        x = self.ml + {"left": 0.0, "right": self.cw - wpt}.get(align, (self.cw - wpt) / 2.0)
        self.ops.append("q %.2f 0 0 %.2f %.2f %.2f cm /%s Do Q" % (wpt, hpt, x, self.py(self.y + hpt), name))
        link = str(b.get("link") or "")
        if link:
            if not _LINK.match("[x](%s)" % link):
                raise PdfError("link must start with http://, https:// or mailto: and hold no spaces")
            self.links[-1].append((x, self.py(self.y + hpt), x + wpt, self.py(self.y), link))
        self.y += hpt + 4
        if cap:
            self.text_block(restyle(parse_inline(cap), _ITALIC), self.base - 2, self.muted,
                            align if align in ("left", "right") else "center")
        self.y += 8

    def _box(self, b, quote=False):
        col = color(b.get("color"), self.muted if quote else self.accent)
        size = _num(b.get("size", self.base), "size")
        pad, lh = 8.0, size * 1.38
        runs = parse_inline(b.get("text", ""))
        if quote:
            runs = restyle(runs, _ITALIC)
        inner = self.cw - 2 * pad - 4
        lines = wrap(runs, inner, size)
        title = b.get("title")
        tl = wrap(restyle(parse_inline(title), _BOLDER), inner, size) if title else []
        hgt = (len(lines) + len(tl)) * lh + 2 * pad + (3 if tl else 0)
        if hgt <= self.H - self.mt - self.mb:
            self.ensure(hgt)
        y0 = self.y
        if not quote:
            self.rect(self.ml, y0, self.cw, hgt, tint(col, 0.10))
        self.rect(self.ml, y0, 3, hgt, col)
        y = y0 + pad
        for ln in tl:
            self.ops.append(line_ops(ln, self.ml + 4 + pad, self.py(self.baseline(y, lh, size)), size, col))
            y += lh
        if tl:
            y += 3
        for ln in lines:
            self.ops.append(line_ops(ln, self.ml + 4 + pad, self.py(self.baseline(y, lh, size)), size,
                                     self.muted if quote else self.text))
            y += lh
        self.y = y0 + hgt + 10

    def b_callout(self, b):
        self._box(b)

    def b_quote(self, b):
        self._box(b, quote=True)

    def b_rule(self, b):
        self.y += 4
        self.hline(self.ml, self.ml + self.cw, self.y, color(b.get("color"), GRID), 0.8)
        self.y += 8

    def b_spacer(self, b):
        self.y += _num(b.get("height_mm", 5), "height_mm") * MM
        if self.room() < 0:
            self.new_page()

    def b_pagebreak(self, b):
        if not self.at_top():
            self.new_page()

    # header, footer, file
    def decorate(self, spec):
        global _SINK
        n = len(self.pages)
        title = str(spec.get("title") or "")
        header = spec.get("header")
        footer = spec.get("footer", "{page} / {pages}")
        for i, ops in enumerate(self.pages, 1):
            _SINK = self.links[i - 1]

            def sub(s):
                return str(s).replace("{page}", str(i)).replace("{pages}", str(n)).replace("{title}", title)
            if header and not (i == 1 and title):
                ln = wrap(parse_inline(sub(header)), self.cw, 8.5)[0]
                ops.append(line_ops(ln, self.ml, self.H - self.mt * 0.55, 8.5, self.muted, "left", self.cw))
            if footer:
                ln = wrap(parse_inline(sub(footer)), self.cw, 8.5)[0]
                ops.append(line_ops(ln, self.ml, self.mb * 0.45, 8.5, self.muted, "center", self.cw))

    def build(self, spec) -> bytes:
        objs = []

        def add(data):
            objs.append(data)
            return len(objs)

        def stream(d, data):
            return ("<< %s /Length %d >>\nstream\n" % (d, len(data))).encode("latin-1") + data + b"\nendstream"

        catalog = add(None)
        pages_id = add(None)
        fonts = []
        for res, base in FONTS.values():
            fonts.append((res, add(("<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
                                    % base).encode("latin-1"))))
        imgs = []
        for name, d, data, sm in self.images:
            smref = ""
            if sm:
                smref = " /SMask %d 0 R" % add(stream("/Type /XObject /Subtype /Image " + sm[0], sm[1]))
            imgs.append((name, add(stream("/Type /XObject /Subtype /Image " + d + smref, data))))
        res = "<< /Font << %s >> /XObject << %s >> >>" % (" ".join("/%s %d 0 R" % f for f in fonts),
                                                         " ".join("/%s %d 0 R" % m for m in imgs))
        kids = []
        for ops, links in zip(self.pages, self.links):
            content = zlib.compress("\n".join(o for o in ops if o).encode("latin-1"))
            cid = add(stream("/Filter /FlateDecode", content))
            annots = []
            for x1, y1, x2, y2, url in links:
                uri = url.encode("ascii", "ignore").decode("ascii").replace("\\", "\\\\").replace("(", "\\(")
                uri = uri.replace(")", "\\)")
                annots.append(add(("<< /Type /Annot /Subtype /Link /Rect [%.2f %.2f %.2f %.2f] /Border [0 0 0] "
                                   "/A << /Type /Action /S /URI /URI (%s) >> >>" % (x1, y1, x2, y2, uri)
                                   ).encode("latin-1")))
            extra = (" /Annots [%s]" % " ".join("%d 0 R" % a for a in annots)) if annots else ""
            kids.append(add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources %s /Contents %d 0 R%s >>"
                             % (pages_id, self.W, self.H, res, cid, extra)).encode("latin-1")))
        objs[pages_id - 1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % k for k in kids),
                                                                          len(kids))).encode("latin-1")
        objs[catalog - 1] = ("<< /Type /Catalog /Pages %d 0 R >>" % pages_id).encode("latin-1")
        info = "<< /Producer (PDF Writer add-on) /CreationDate (D:%s)" % time.strftime("%Y%m%d%H%M%S")
        if spec.get("title"):
            info += " /Title %s" % _pdfstr(_clean(plain(spec["title"])))
        if spec.get("author"):
            info += " /Author %s" % _pdfstr(_clean(plain(spec["author"])))
        info_id = add((info + " >>").encode("latin-1"))
        out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, o in enumerate(objs, 1):
            offsets.append(len(out))
            out += ("%d 0 obj\n" % i).encode("latin-1") + o + b"\nendobj\n"
        xref = len(out)
        out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode("latin-1")
        for off in offsets:
            out += ("%010d 00000 n \n" % off).encode("latin-1")
        out += ("trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objs) + 1, catalog, info_id, xref)).encode("latin-1")
        return bytes(out)


def render(spec: dict):
    if not isinstance(spec, dict):
        raise PdfError("the document must be an object")
    d = Doc(spec)
    d.title_block(spec)
    blocks = spec.get("blocks") or []
    if not isinstance(blocks, list):
        raise PdfError("blocks must be a list")
    for i, b in enumerate(blocks, 1):
        if isinstance(b, str):
            b = {"type": "paragraph", "text": b}
        if not isinstance(b, dict):
            raise PdfError("block %d must be an object or a text" % i)
        t = b.get("type", "paragraph")
        if t not in BLOCKS:
            raise PdfError("block %d: type %r is not one of %s" % (i, t, ", ".join(BLOCKS)))
        try:
            getattr(d, "b_" + t)(b)
        except PdfError as e:
            raise PdfError("block %d (%s): %s" % (i, t, e))
    d.decorate(spec)
    return d.build(spec), len(d.pages), len(d.images)
