"""Text drawing for the rendered lyric sheet (PIL, no Tk).

Tk labels fall back to another font on their own when a glyph is missing;
PIL does not, so Japanese lyrics in Segoe UI would draw as a row of boxes.
Text is split into runs by script and each run gets a font that has it:
Yu Gothic / Microsoft YaHei for CJK, Malgun Gothic for Hangul, Leelawadee for
Thai, Segoe UI Emoji / Symbol for pictographs, Segoe UI for everything else
(Latin, Greek, Cyrillic, Arabic, Hebrew).
"""
import glob
import os
import re

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

_FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

_FILES = {
    ("main", "regular"):  ["segoeui.ttf"],
    ("main", "semibold"): ["seguisb.ttf", "segoeuib.ttf", "segoeui.ttf"],
    ("main", "bold"):     ["segoeuib.ttf", "seguisb.ttf", "segoeui.ttf"],
    ("cjk", "regular"):   ["YuGothM.ttc", "msyh.ttc", "msgothic.ttc"],
    ("cjk", "bold"):      ["YuGothB.ttc", "msyhbd.ttc", "msgothic.ttc"],
    ("hangul", "regular"): ["malgun.ttf"],
    ("hangul", "bold"):   ["malgunbd.ttf", "malgun.ttf"],
    ("thai", "regular"):  ["LeelawUI.ttf", "leelawad.ttf"],
    ("thai", "bold"):     ["LeelaUIb.ttf", "leelawdb.ttf", "LeelawUI.ttf"],
    ("sym", "regular"):   ["seguisym.ttf"],
    ("emoji", "regular"): ["seguiemj.ttf", "seguisym.ttf"],
}


# Lyric font families the user can choose (Settings → Appearance). Each
# weight lists (file pattern, variation name) candidates; the first one found
# in the system or the per-user font folder wins. "Segoe UI" is the built-in
# default and uses _FILES. Only the "main" script changes family: CJK, Hangul,
# Thai, symbols and emoji keep their own fonts, and any other character the
# chosen family lacks falls back to Segoe UI.
FAMILIES = {
    "Segoe UI Variable": {"regular": [("SegUIVar.ttf", b"Regular")],
                          "semibold": [("SegUIVar.ttf", b"Semibold Display")],
                          "bold": [("SegUIVar.ttf", b"Bold Display")]},
    "Bahnschrift": {"regular": [("bahnschrift.ttf", b"Regular")],
                    "semibold": [("bahnschrift.ttf", b"SemiBold")],
                    "bold": [("bahnschrift.ttf", b"Bold")]},
    "Georgia": {"regular": [("georgia.ttf", None)],
                "semibold": [("georgiab.ttf", None)], "bold": [("georgiab.ttf", None)]},
    "Consolas": {"regular": [("consola.ttf", None)],
                 "semibold": [("consolab.ttf", None)], "bold": [("consolab.ttf", None)]},
    "Lexend": {"regular": [("Lexend-Regular.*", None), ("Lexend*wght*.ttf", b"Regular")],
               "semibold": [("Lexend-SemiBold.*", None), ("Lexend*wght*.ttf", b"SemiBold"),
                            ("Lexend-Bold.*", None)],
               "bold": [("Lexend-Bold.*", None), ("Lexend*wght*.ttf", b"Bold")]},
    "OpenDyslexic": {"regular": [("OpenDyslexic-Regular.*", None), ("OpenDyslexic*Regular.*", None)],
                     "semibold": [("OpenDyslexic-Bold.*", None), ("OpenDyslexic*Bold.*", None)],
                     "bold": [("OpenDyslexic-Bold.*", None), ("OpenDyslexic*Bold.*", None)]},
}
DEFAULT_FAMILY = "Segoe UI"


def _font_dirs():
    user = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")
    return [_FONT_DIR] + ([user] if os.path.isdir(user) else [])


def _find_font(pattern):
    for d in _font_dirs():
        hits = sorted(glob.glob(os.path.join(d, pattern)))
        hits = [h for h in hits if h.lower().endswith((".ttf", ".otf", ".ttc"))]
        if hits:
            return hits[0]
    return None


def available_families():
    """Families that are installed here, the default first."""
    out = [DEFAULT_FAMILY]
    for name, spec in FAMILIES.items():
        if any(_find_font(p) for p, _v in spec["regular"]):
            out.append(name)
    return out


def _script(ch):
    o = ord(ch)
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "hangul"
    if (0x3000 <= o <= 0x30FF or 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
            or 0xFF00 <= o <= 0xFFEF):
        return "cjk"
    if 0x0E00 <= o <= 0x0E7F:
        return "thai"
    if o >= 0x1F000:
        return "emoji"
    if 0x2190 <= o <= 0x2BFF and o not in (0x2022, 0x2026):
        return "sym"
    return "main"


# Characters that may break a line on their own (no spaces in CJK text).
_BREAK_ANY = {"cjk", "hangul", "thai"}


class TextRenderer:
    """Font cache plus measuring, wrapping and drawing across scripts."""

    def __init__(self, family=None):
        self._fonts = {}
        self.family = family if family in FAMILIES else None
        self._covered = {}      # char -> does the family have it
        self._notdef = None

    def _family_font(self, weight, px):
        spec = FAMILIES[self.family]
        for pattern, var in spec.get(weight) or spec["regular"]:
            path = _find_font(pattern)
            if not path:
                continue
            try:
                f = ImageFont.truetype(path, int(px))
                if var:
                    try:
                        f.set_variation_by_name(var)
                    except Exception:
                        pass
                return f
            except OSError:
                continue
        return None

    def _has(self, ch):
        """Whether the chosen family draws `ch` (not its missing-glyph box)."""
        ok = self._covered.get(ch)
        if ok is None:
            f = self.font("main", "regular", 24)

            def ink(c):
                im = Image.new("L", (40, 40), 0)
                ImageDraw.Draw(im).text((4, 4), c, font=f, fill=255)
                return im.tobytes()
            try:
                if self._notdef is None:
                    self._notdef = ink(chr(0xFFFF))
                got = ink(ch)
                ok = any(got) and got != self._notdef
            except Exception:
                ok = False
            self._covered[ch] = ok
        return ok

    def font(self, script, weight, px):
        if script == "main" and self.family:
            key = ("fam", weight, int(px))
            f = self._fonts.get(key)
            if f is None:
                f = self._family_font(weight, px) or self._builtin("main", weight, px)
                self._fonts[key] = f
            return f
        return self._builtin(script, weight, px)

    def _builtin(self, script, weight, px):
        if script == "fb":
            script = "main"
        w = weight if (script, weight) in _FILES else (
            "bold" if weight != "regular" and (script, "bold") in _FILES else "regular")
        key = (script, w, int(px))
        f = self._fonts.get(key)
        if f is None:
            f = None
            for name in _FILES.get((script, w), []) + _FILES[("main", "regular")]:
                try:
                    f = ImageFont.truetype(os.path.join(_FONT_DIR, name), int(px))
                    break
                except OSError:
                    continue
            if f is None:
                f = ImageFont.load_default(int(px))
            self._fonts[key] = f
        return f

    def runs(self, text):
        out = []
        fam = self.family
        for ch in text:
            sc = _script(ch)
            if fam and sc == "main" and ord(ch) > 0xFF and not ch.isspace() and not self._has(ch):
                sc = "fb"              # the chosen family lacks it: Segoe UI
            if ch.isspace() and out:
                sc = out[-1][0]        # spaces stay with the run they follow
            if out and out[-1][0] == sc:
                out[-1][1] += ch
            else:
                out.append([sc, ch])
        return out

    def measure(self, text, weight, px):
        return sum(self.font(sc, weight, px).getlength(s) for sc, s in self.runs(text))

    def ascent(self, weight, px):
        return self.font("main", weight, px).getmetrics()[0]

    def line_height(self, weight, px):
        a, d = self.font("main", weight, px).getmetrics()
        return a + d

    def draw(self, draw, xy, text, weight, px, fill):
        """Draw `text` with its top-left at xy. Returns the advance width."""
        x, y = xy
        base = y + self.ascent(weight, px)
        for sc, s in self.runs(text):
            f = self.font(sc, weight, px)
            draw.text((x, base), s, font=f, fill=fill, anchor="ls")
            x += f.getlength(s)
        return x - xy[0]

    def ellipsize(self, text, weight, px, maxw):
        if self.measure(text, weight, px) <= maxw:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.measure(text[:mid].rstrip() + "…", weight, px) <= maxw:
                lo = mid
            else:
                hi = mid - 1
        return (text[:lo].rstrip() + "…") if lo else "…"

    _TOKEN = re.compile(r"\s+|[^\s]")

    def wrap(self, text, weight, px, maxw):
        """Greedy word wrap. CJK/Hangul/Thai may break between any two
        characters; everything else breaks at spaces, and a single word
        longer than the line is split where it has to be."""
        # Tokens: words (runs of non-space, split at every break-anywhere
        # character) and the whitespace between them.
        tokens, word = [], ""
        for ch in text:
            if ch.isspace():
                if word:
                    tokens.append(word); word = ""
                if tokens and tokens[-1].isspace():
                    tokens[-1] += ch
                else:
                    tokens.append(ch)
            elif _script(ch) in _BREAK_ANY:
                if word:
                    tokens.append(word); word = ""
                tokens.append(ch)
            else:
                word += ch
        if word:
            tokens.append(word)

        lines, cur = [], ""
        for tok in tokens:
            cand = cur + tok
            if not cur.strip() or self.measure(cand.rstrip(), weight, px) <= maxw:
                cur = cand if cur.strip() or not tok.isspace() else ""
                # An over-long single word: hard-split it.
                while self.measure(cur.rstrip(), weight, px) > maxw and len(cur.strip()) > 1:
                    cut = len(cur)
                    while cut > 1 and self.measure(cur[:cut], weight, px) > maxw:
                        cut -= 1
                    lines.append(cur[:cut])
                    cur = cur[cut:]
                continue
            lines.append(cur.rstrip())
            cur = "" if tok.isspace() else tok
        if cur.strip():
            lines.append(cur.rstrip())
        return lines or [""]
