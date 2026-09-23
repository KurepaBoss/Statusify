"""Romanisation and translation of lyric lines.

For the current track's lyric lines this works out, per line,
    {"rom": romanised text or None, "tr": translation or None}
and leaves the result in main.state.translation ({line_index: {...}}), then
posts ("translation",) on main.event_queue so the lyric sheet redraws.

Where the text comes from:
  * Romanisation is only attempted for lines written in a non-Latin script.
    Hangul (Revised Romanization), Cyrillic and Greek are done here, offline,
    from tables. Japanese and Chinese use pykakasi / pypinyin when they happen
    to be installed (optional, never required). Everything else — and
    Japanese/Chinese without those libraries — takes the source
    transliteration Google's free web endpoint returns alongside a
    translation (dt=rm).
  * Translation uses the same endpoint, one POST per chunk of lines. Lines
    are joined with a "\\n|\\n" separator: plain newlines are preserved in the
    translation but collapsed to spaces in the transliteration, while a lone
    "|" survives both, so the answer can be split back into lines. A chunk
    whose answer doesn't split into the right number of pieces is dropped
    rather than risk showing a line under the wrong lyric.

Results are cached in translations.db in the data folder, keyed by
(track uri, target language, hash of the lines), so a replay is instant and
works offline. A failed network lookup is not cached, so it's retried the
next time the track plays.

main binds M (the live main module) at import time; until then the module is
inert and importable in tests.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

M = None   # the main module, bound by main

# ── Settings ─────────────────────────────────────────────────────
# preferences.lyric_subline: what the lyric sheet shows under each line.
SUBLINE_MODES = (("Off", "off"), ("Romanised", "rom"), ("Translated", "tr"), ("Both", "both"))
# preferences.translate_to: "auto" follows the Windows display language.
LANGUAGES = (
    ("auto", "Auto"), ("en", "English"), ("es", "Spanish"), ("fr", "French"),
    ("de", "German"), ("it", "Italian"), ("pt", "Portuguese"), ("nl", "Dutch"),
    ("pl", "Polish"), ("sr", "Serbian"), ("hr", "Croatian"), ("ru", "Russian"),
    ("uk", "Ukrainian"), ("tr", "Turkish"), ("ar", "Arabic"), ("hi", "Hindi"),
    ("id", "Indonesian"), ("ja", "Japanese"), ("ko", "Korean"),
    ("zh-CN", "Chinese"), ("sv", "Swedish"),
)
_SUBLINE = None       # cached preference, read once
_TARGET  = None


def _cfg(key, default):
    try:
        return (M._cfg_get("preferences", key, default) or default) if M else default
    except Exception:
        return default


def subline_mode():
    global _SUBLINE
    if _SUBLINE is None:
        v = _cfg("lyric_subline", "off").lower()
        _SUBLINE = v if v in {k for _, k in SUBLINE_MODES} else "off"
    return _SUBLINE


def set_subline_mode(mode):
    global _SUBLINE
    _SUBLINE = mode if mode in {k for _, k in SUBLINE_MODES} else "off"
    if M:
        M._cfg_set("preferences", "lyric_subline", _SUBLINE)
    refresh()


def translate_to():
    """The stored choice: "auto" or a language code."""
    global _TARGET
    if _TARGET is None:
        _TARGET = _cfg("translate_to", "auto")
    return _TARGET


def set_translate_to(code):
    global _TARGET
    _TARGET = code or "auto"
    if M:
        M._cfg_set("preferences", "translate_to", _TARGET)
    refresh()


def language_name(code):
    for c, name in LANGUAGES:
        if c == code:
            return name
    return code


_SYS_LANG = None


def system_lang():
    """The Windows display language as a Google language code ("en" if unknown)."""
    global _SYS_LANG
    if _SYS_LANG is None:
        _SYS_LANG = _detect_system_lang()
    return _SYS_LANG


def _detect_system_lang():
    import locale
    name = None
    try:
        import ctypes
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        name = locale.windows_locale.get(lcid)
    except Exception:
        pass
    if not name:
        try:
            name = locale.getlocale()[0]
        except Exception:
            name = None
    return locale_to_lang(name)


def locale_to_lang(name):
    """'en_US' -> 'en', 'zh_TW' -> 'zh-TW', 'sr_Latn_RS' -> 'sr'; None -> 'en'."""
    if not name:
        return "en"
    parts = re.split(r"[_\-.]", name)
    base = parts[0].lower()
    if not re.fullmatch(r"[a-z]{2,3}", base):
        return "en"
    if base == "zh":
        rest = {p.upper() for p in parts[1:]}
        return "zh-TW" if rest & {"TW", "HK", "MO", "HANT"} else "zh-CN"
    if base == "nb" or base == "nn":
        return "no"
    return base


def target_lang():
    t = translate_to()
    return system_lang() if t == "auto" else t


def _base(code):
    return (code or "").split("-")[0].lower()


# ── Script detection ─────────────────────────────────────────────
def _script(ch):
    """Coarse script of one character: 'latin', 'hangul', 'kana', 'han',
    'cyrillic', 'greek', 'other' (a non-Latin letter), or None (not a letter)."""
    o = ord(ch)
    if o < 0x80:
        return "latin" if ch.isalpha() else None
    if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "hangul"
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9F:
        return "kana"
    if (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF
            or 0x20000 <= o <= 0x2FA1F):
        return "han"
    if 0x0400 <= o <= 0x052F:
        return "cyrillic" if ch.isalpha() else None
    if 0x0370 <= o <= 0x03FF or 0x1F00 <= o <= 0x1FFF:
        return "greek" if ch.isalpha() else None
    if not ch.isalpha():
        return None
    try:
        return "latin" if unicodedata.name(ch).startswith("LATIN") else "other"
    except ValueError:
        return "other"


def scripts_in(text):
    return {s for s in map(_script, text or "") if s}


def needs_romanisation(text):
    return bool(scripts_in(text) - {"latin"})


# ── Offline romanisation ─────────────────────────────────────────
# Hangul: Revised Romanization with the common sound changes (liaison,
# nasalisation, liquid assimilation). Good enough to sing along to.
_L = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj",
      "ch", "k", "t", "p", "h"]
_V = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo",
      "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
# Final consonant, as (spelled before a consonant, carried onto a following
# vowel-initial syllable as (stay, move)).
_T = [("", ("", "")), ("k", ("", "g")), ("k", ("", "kk")), ("k", ("k", "s")),
      ("n", ("", "n")), ("n", ("n", "j")), ("n", ("", "n")), ("t", ("", "d")),
      ("l", ("", "r")), ("k", ("l", "g")), ("m", ("l", "m")), ("l", ("l", "b")),
      ("l", ("l", "s")), ("l", ("l", "t")), ("p", ("l", "p")), ("l", ("", "r")),
      ("m", ("", "m")), ("p", ("", "b")), ("p", ("p", "s")), ("t", ("", "s")),
      ("t", ("", "ss")), ("ng", ("ng", "")), ("t", ("", "j")), ("t", ("", "ch")),
      ("k", ("", "k")), ("t", ("", "t")), ("p", ("", "p")), ("t", ("", ""))]


def _hangul_word(word):
    syl = []
    for ch in word:
        s = ord(ch) - 0xAC00
        syl.append((s // 588, (s % 588) // 28, s % 28))
    out = []
    carry = None          # initial forced onto this syllable by the previous final
    for i, (l, v, t) in enumerate(syl):
        nxt = syl[i + 1] if i + 1 < len(syl) else None
        ini = _L[l] if carry is None else carry
        carry = None
        fin = ""
        if t:
            spelled, (stay, move) = _T[t]
            if nxt is not None and nxt[0] == 11:          # next starts with silent ㅇ
                fin, carry = stay, (move or None)
            elif nxt is not None:
                nl = nxt[0]
                fin = spelled
                if nl in (2, 6):                           # ㄴ ㅁ: nasalise
                    fin = {"k": "ng", "t": "n", "p": "m"}.get(fin, fin)
                elif nl == 5 and fin in ("n", "l"):        # ㄴ/ㄹ + ㄹ -> ll
                    fin, carry = "l", "l"
                elif nl == 5:                              # ㄹ after other stops -> n
                    carry = "n"
                    fin = {"k": "ng", "p": "m", "t": "n"}.get(fin, fin)
                elif nl == 18 and fin in ("k", "t", "p"):  # aspiration with ㅎ
                    carry = {"k": "k", "t": "t", "p": "p"}[fin]
                    fin = ""
            else:
                fin = spelled
        out.append(ini + _V[v] + fin)
    return "".join(out)


def romanise_hangul(text):
    return re.sub(r"[가-힣]+", lambda m: _hangul_word(m.group(0)), text)


_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
    # Ukrainian / Belarusian
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
    # Serbian / Macedonian (Gaj's Latin, which Serbian itself uses)
    "ђ": "đ", "ј": "j", "љ": "lj", "њ": "nj", "ћ": "ć", "џ": "dž", "ѓ": "gj",
    "ќ": "kj", "ѕ": "dz",
}
_GRK_DI = {"ου": "ou", "αι": "ai", "ει": "ei", "οι": "oi", "αυ": "av", "ευ": "ev",
           "γγ": "ng", "γκ": "gk", "μπ": "b", "ντ": "d"}
_GRK = {"α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th",
        "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p",
        "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "φ": "f", "χ": "ch", "ψ": "ps",
        "ω": "o"}


def _case_like(src, out):
    if not out or not src.isupper():
        return out
    return out[0].upper() + out[1:]


def romanise_cyrillic(text):
    return "".join(_case_like(ch, _CYR[ch.lower()]) if ch.lower() in _CYR else ch
                   for ch in text)


def romanise_greek(text):
    # Strip tonos/dialytika first: ά -> α.
    t = "".join(c for c in unicodedata.normalize("NFD", text)
                if unicodedata.category(c) != "Mn")
    out, i = [], 0
    while i < len(t):
        pair = t[i:i + 2]
        if pair.lower() in _GRK_DI:
            out.append(_case_like(pair[0], _GRK_DI[pair.lower()]))
            i += 2
            continue
        ch = t[i]
        out.append(_case_like(ch, _GRK[ch.lower()]) if ch.lower() in _GRK else ch)
        i += 1
    return "".join(out)


_KAKASI = False     # False = not tried yet, None = unavailable


def _kakasi():
    global _KAKASI
    if _KAKASI is False:
        try:
            import pykakasi
            _KAKASI = pykakasi.kakasi()
        except Exception:
            _KAKASI = None
    return _KAKASI


def _pinyin():
    try:
        from pypinyin import lazy_pinyin, Style
        return lambda s: " ".join(p for p in lazy_pinyin(s, style=Style.TONE) if p.strip())
    except Exception:
        return None


def romanise_offline(text, japanese=False):
    """Romanised text, or None when this line needs the network."""
    sc = scripts_in(text) - {"latin"}
    if not sc:
        return None
    out = text
    if "hangul" in sc:
        out = romanise_hangul(out)
    if "cyrillic" in sc:
        out = romanise_cyrillic(out)
    if "greek" in sc:
        out = romanise_greek(out)
    rest = sc - {"hangul", "cyrillic", "greek"}
    if not rest:
        return out
    if rest <= {"kana", "han"} and (japanese or "kana" in rest):
        k = _kakasi()
        if k is None:
            return None
        try:
            return " ".join(p["hepburn"] for p in k.convert(out) if p.get("hepburn")).strip()
        except Exception:
            return None
    if rest == {"han"}:
        py = _pinyin()
        return py(out) if py else None
    return None


# ── Google web endpoint ──────────────────────────────────────────
_URL = ("https://translate.googleapis.com/translate_a/single"
        "?client=gtx&sl=auto&tl={tl}&dt=t&dt=rm")
_SEP = "\n|\n"
_SPLIT = re.compile(r"\s*[|｜]\s*")
_CHUNK_CHARS = 3500
TIMEOUT_S = 8
_net_error_logged = False


def _post(tl, text):
    req = urllib.request.Request(
        _URL.format(tl=urllib.parse.quote(tl)),
        data=urllib.parse.urlencode({"q": text}).encode("utf-8"),
        headers={"User-Agent": "Mozilla/5.0",
                 "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_response(data, n):
    """Google's nested-array answer for n "|"-joined lines ->
    (translations or None, romanisations or None, detected source language)."""
    segs = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
    tr_text = "".join(s[0] for s in segs
                      if isinstance(s, list) and s and isinstance(s[0], str))
    rom_text = "".join(s[3] for s in segs
                       if isinstance(s, list) and len(s) > 3 and s[0] is None
                       and isinstance(s[3], str))
    src = data[2] if isinstance(data, list) and len(data) > 2 and isinstance(data[2], str) else ""
    trs = [p.strip() for p in _SPLIT.split(tr_text.strip())] if tr_text else None
    roms = [p.strip() for p in _SPLIT.split(rom_text.strip())] if rom_text else None
    if trs is not None and len(trs) != n:
        trs = None
    if roms is not None and len(roms) != n:
        roms = None
    return trs, roms, src


def _chunks(texts):
    cur, size = [], 0
    for t in texts:
        if cur and size + len(t) + 3 > _CHUNK_CHARS:
            yield cur
            cur, size = [], 0
        cur.append(t)
        size += len(t) + 3
    if cur:
        yield cur


def script_group(text):
    """Which batch a line goes in. Google detects one source language per
    request, so a Korean line batched with Japanese ones comes back as
    nonsense; lines are grouped by script and each group sent on its own.
    Kana and Han share a group so a Japanese song's kanji-only lines travel
    with its kana lines."""
    for ch in text:
        sc = _script(ch)
        if sc in ("kana", "han"):
            return "cjk"
        if sc in ("hangul", "cyrillic", "greek"):
            return sc
        if sc == "other":
            try:
                return unicodedata.name(ch).split()[0].lower()
            except ValueError:
                return "other"
    return "latin"


def _fetch(texts, tl, post=None):
    """{text: (tr, rom, detected source language)} for unique texts.
    Raises on network failure."""
    post = post or _post
    groups = {}
    for t in texts:
        groups.setdefault(script_group(t), []).append(t)
    out = {}
    for group in groups.values():
        for chunk in _chunks(group):
            safe = [t.replace("|", "/") for t in chunk]
            trs, roms, src = parse_response(post(tl, _SEP.join(safe)), len(chunk))
            for i, t in enumerate(chunk):
                out[t] = (trs[i] if trs else None, roms[i] if roms else None, src)
    return out


def _log(msg):
    try:
        if M:
            M.log(msg)
    except Exception:
        pass


def compute(lines, lang, want, post=None):
    """Work out {index: {"rom", "tr"}} for `lines`. `want` is a subset of
    {"rom", "tr"}. Returns (result, fields actually completed)."""
    global _net_error_logged
    result = {i: {"rom": None, "tr": None} for i in range(len(lines))}
    done = set(want)
    japanese = any("kana" in scripts_in(l) for l in lines)
    net_rom = set()
    if "rom" in want:
        for i, l in enumerate(lines):
            if needs_romanisation(l):
                r = romanise_offline(l, japanese)
                if r is None:
                    net_rom.add(l)
                elif r.strip() and r.strip() != l.strip():
                    result[i]["rom"] = r.strip()
    texts = []
    if "tr" in want:
        texts = [l for l in dict.fromkeys(x.strip() for x in lines)
                 if l and any(ch.isalpha() for ch in l)]
    elif net_rom:
        texts = list(dict.fromkeys(x.strip() for x in net_rom))
    if not texts:
        return result, done
    try:
        got = _fetch(texts, lang, post)
    except Exception as e:
        if not _net_error_logged:
            _net_error_logged = True
            _log(f"Lyric translation unavailable: {type(e).__name__}: {e}")
        if "tr" in want:
            done.discard("tr")
        if net_rom:
            done.discard("rom")
        return result, done
    for i, l in enumerate(lines):
        tr, rom, src = got.get(l.strip(), (None, None, ""))
        same_lang = bool(src) and _base(src) == _base(lang)
        if "tr" in want and tr and not same_lang and tr.strip().lower() != l.strip().lower():
            result[i]["tr"] = tr
        if l in net_rom and rom and rom.strip() != l.strip():
            result[i]["rom"] = rom
    return result, done


# ── Disk cache ───────────────────────────────────────────────────
_DB = None
_DB_LOCK = threading.Lock()


def _db_path():
    base = getattr(M, "_APP_DIR", None) or os.environ.get("STATUSIFY_DATA_DIR") or "."
    return os.path.join(base, "translations.db")


def _db():
    global _DB
    if _DB is None:
        _DB = sqlite3.connect(_db_path(), check_same_thread=False)
        _DB.execute("CREATE TABLE IF NOT EXISTS translations ("
                    "uri TEXT, lang TEXT, hash TEXT, fields TEXT, data TEXT, "
                    "PRIMARY KEY (uri, lang, hash))")
        _DB.commit()
    return _DB


def lines_hash(lines):
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


def cache_get(uri, lang, h):
    try:
        with _DB_LOCK:
            row = _db().execute("SELECT fields, data FROM translations "
                                "WHERE uri=? AND lang=? AND hash=?", (uri, lang, h)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        data = {int(k): v for k, v in json.loads(row[1]).items()}
        return set(filter(None, row[0].split(","))), data
    except Exception:
        return None


def cache_put(uri, lang, h, fields, data):
    try:
        with _DB_LOCK:
            _db().execute("INSERT OR REPLACE INTO translations VALUES (?,?,?,?,?)",
                          (uri, lang, h, ",".join(sorted(fields)), json.dumps(data)))
            _db().commit()
    except Exception as e:
        _log(f"Translation cache write failed: {e}")


def close():
    global _DB
    with _DB_LOCK:
        if _DB is not None:
            try:
                _DB.close()
            except Exception:
                pass
            _DB = None


# ── Requests ─────────────────────────────────────────────────────
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="translate")
_current = {"uri": None, "data": {}}
_gen = [0]


def _wanted(mode=None):
    mode = mode or subline_mode()
    return {"off": set(), "rom": {"rom"}, "tr": {"tr"}, "both": {"rom", "tr"}}.get(mode, set())


def _work(uri, lines, lang, want, gen, callback, post=None):
    h = lines_hash(lines)
    cached = cache_get(uri, lang, h)
    if cached and want <= cached[0]:
        result = cached[1]
    else:
        prev_fields, prev = cached if cached else (set(), {})
        need = want | prev_fields
        result, done = compute(lines, lang, need, post)
        # Keep any field cached earlier that this run didn't recompute.
        for f in prev_fields - done:
            for i, entry in prev.items():
                if i in result and entry.get(f):
                    result[i][f] = entry[f]
        done |= prev_fields
        if done:
            cache_put(uri, lang, h, done, result)
    _deliver(uri, result, gen)
    if callback:
        try:
            callback(uri, result)
        except Exception as e:
            _log(f"Translation callback failed: {e}")
    return result


def _deliver(uri, result, gen):
    if M is None or gen != _gen[0]:
        return
    if getattr(M.state, "track_uri", "") != uri:
        return
    _current["uri"], _current["data"] = uri, result
    M.state.translation = result
    M.event_queue.put(("translation",))


def request(uri, lines, callback=None, *, want=None, lang=None, post=None):
    """Romanise/translate `lines` (a list of str) for track `uri` off the Tk
    thread. The result lands in main.state.translation (if `uri` is still
    playing) and ("translation",) is posted; `callback(uri, result)` is then
    called on the worker thread. Returns the Future."""
    lines = [l if isinstance(l, str) else "" for l in (lines or [])]
    want = set(_wanted() if want is None else want)
    _gen[0] += 1
    if not want or not lines or not uri:
        return None
    return _executor.submit(_work, uri, lines, lang or target_lang(), want, _gen[0],
                            callback, post)


def lyric_lines(mode, synced, plain):
    if mode == "synced" and synced:
        return [(l.get("words") or "") if isinstance(l, dict) else str(l) for l in synced]
    if mode == "plain" and plain:
        return [l if isinstance(l, str) else str(l) for l in plain]
    return []


def on_lyrics(uri, mode, synced, plain):
    """Hook for main._apply_lyrics: forget the old sublines and fetch new ones."""
    _current["uri"], _current["data"] = None, {}
    if M is not None:
        M.state.translation = {}
    if subline_mode() == "off":
        _gen[0] += 1
        return None
    return request(uri, lyric_lines(mode, synced, plain))


def refresh():
    """Re-run for the current track after a settings change."""
    if M is None:
        return None
    st = M.state
    return on_lyrics(getattr(st, "track_uri", ""), getattr(st, "lyrics_mode", "none"),
                     getattr(st, "synced", []), getattr(st, "plain", []))


def sublines_for(index):
    """(romanised, translated) for line `index` of the current track, each
    None when not available or not wanted by the setting."""
    mode = subline_mode()
    if mode == "off" or M is None:
        return None, None
    if _current["uri"] != getattr(M.state, "track_uri", None):
        return None, None
    entry = (getattr(M.state, "translation", None) or {}).get(index) or {}
    rom = entry.get("rom") if mode in ("rom", "both") else None
    tr = entry.get("tr") if mode in ("tr", "both") else None
    return rom, tr


def subline_for(index):
    """Text to draw under lyric line `index`, or None. "Both" joins the
    romanisation and the translation with " · "."""
    parts = [p for p in sublines_for(index) if p]
    return "  ·  ".join(parts) if parts else None
