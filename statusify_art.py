"""Album art: memory -> disk -> network cache, and rounded-corner rendering.

Split out of main.py; main calls init() with the cache folder and logger.
"""
import os
import threading
from io import BytesIO

try:
    from PIL import Image, ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

log = lambda *_a, **_k: None


def init(disk_dir, log_fn=None):
    global _ART_DISK_DIR, log
    _ART_DISK_DIR = disk_dir
    if log_fn is not None:
        log = log_fn


# ── Album art cache ───────────────────────────────────────────────
# Art was refetched over the network on every single track change with no
# caching at all, so replaying an album re-downloaded the same JPEG each
# time. Keyed by (url, size) because the hero image and the history
# thumbnails want different resolutions of the same source.
_ART_CACHE      = {}
_ART_CACHE_LOCK = threading.Lock()
_ART_CACHE_MAX  = 80
_ART_DISK_DIR   = None   # set by init()

_ART_DISK_MAX_FILES = 400   # ~2 PNGs per track, so roughly 200 albums

def _art_disk_path(url, size):
    import hashlib
    h = hashlib.sha1(f"{url}@{size}".encode("utf-8")).hexdigest()
    return os.path.join(_ART_DISK_DIR, f"{h}.png")

def _prune_art_cache(max_files=_ART_DISK_MAX_FILES):
    """Drop the least-recently-modified PNGs from the on-disk art cache.

    The in-memory cache has had a size cap from the start, but its disk tier
    only ever grew: every distinct album at every distinct size wrote a PNG
    that nothing ever removed. Called once at startup, off the Tk thread."""
    try:
        entries = []
        with os.scandir(_ART_DISK_DIR) as it:
            for de in it:
                if de.is_file() and de.name.endswith(".png"):
                    try:
                        entries.append((de.stat().st_mtime, de.path))
                    except OSError:
                        pass
        if len(entries) <= max_files:
            return
        entries.sort()
        for _, path in entries[: len(entries) - max_files]:
            try:
                os.remove(path)
            except OSError:
                pass
        log(f"Art cache pruned  ·  {len(entries) - max_files} file(s) removed")
    except (FileNotFoundError, OSError):
        pass

def _round_image(img, radius, bg):
    """Return `img` with rounded corners, composited onto solid colour `bg`.

    Applied at display time rather than inside _fetch_art on purpose: the
    disk cache holds the raw square artwork, so the corner radius and the
    surface colour behind it stay free to change (theme switch, different
    panel) without invalidating a single cached PNG.

    Composites onto an opaque background instead of returning RGBA because
    Tk's PhotoImage does not alpha-blend against a Canvas — a transparent
    corner renders as black, which is precisely the artefact this is meant
    to avoid. The caller passes whatever colour sits behind the art."""
    if img is None or not PIL_AVAILABLE:
        return img
    try:
        img = img.convert("RGB")
        w, h = img.size
        radius = max(0, min(int(radius), min(w, h) // 2))
        if radius == 0:
            return img
        # Build the mask at 4× and downsample: PIL's rounded_rectangle is
        # hard-edged, and an un-antialiased 10 px corner on a 120 px image is
        # visibly staircased.
        scale = 4
        mask = Image.new("L", (w * scale, h * scale), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, w * scale - 1, h * scale - 1),
            radius=radius * scale, fill=255)
        mask = mask.resize((w, h), Image.LANCZOS)
        out = Image.new("RGB", (w, h), bg)
        out.paste(img, (0, 0), mask)
        return out
    except Exception:
        return img   # never let decoration break the image path

def _fetch_art(url, size):
    """Return a PIL image of `url` resized to size×size, or None.

    Three tiers: in-memory dict → on-disk PNG cache → network. Always called
    from a worker thread, never the Tk loop."""
    if not PIL_AVAILABLE or not url:
        return None
    key = (url, size)
    with _ART_CACHE_LOCK:
        hit = _ART_CACHE.get(key)
    if hit is not None:
        return hit

    img = None
    disk = _art_disk_path(url, size)
    try:
        if os.path.exists(disk):
            img = Image.open(disk)
            img.load()   # force decode now, while we're off the Tk thread
    except Exception:
        img = None

    if img is None:
        try:
            import urllib.request
            data = urllib.request.urlopen(url, timeout=4).read()
            img  = Image.open(BytesIO(data)).convert("RGB").resize((size, size), Image.LANCZOS)
        except Exception:
            return None
        try:
            os.makedirs(_ART_DISK_DIR, exist_ok=True)
            img.save(disk, "PNG")
        except Exception:
            pass  # disk cache is an optimisation, not a requirement

    with _ART_CACHE_LOCK:
        if len(_ART_CACHE) >= _ART_CACHE_MAX:
            # Cheap FIFO eviction — good enough for a cache this small, and
            # avoids pulling in an LRU dependency.
            for k in list(_ART_CACHE)[: _ART_CACHE_MAX // 4]:
                _ART_CACHE.pop(k, None)
        _ART_CACHE[key] = img
    return img


def dominant_tint(img):
    """Cover tint for the lyric-sheet theme (statusify_colors.tint_from_pixels)."""
    if img is None or not PIL_AVAILABLE:
        return None
    try:
        from statusify_colors import tint_from_pixels
        small = img.convert("RGB").resize((24, 24), Image.BILINEAR)
        return tint_from_pixels(small.getdata())
    except Exception:
        return None
