"""Now Playing phase B: shuffle/repeat/like/volume, Up Next, lyric search
with pinning, the drawn context menu, share-as-image, sub-lines and wiring."""
import asyncio
import json
import os
import queue as _queue
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import statusify_np_extras as ex
import statusify_translate as trm
from statusify_history import HistoryStore

tk = main.tk
URI = "spotify:track:np2np2np2np2np2np2np2n"


# ── Pure helpers ──────────────────────────────────────────────────

def test_icon_masks_draw_something():
    for kind in ex.ICONS:
        m = ex.icon_mask(kind, 64)
        assert m.getbbox() is not None, kind
    # The outline heart is hollow, the filled one isn't.
    full, hollow = ex.icon_mask("heart", 64), ex.icon_mask("heart_o", 64)
    assert full.getpixel((32, 30)) == 255 and hollow.getpixel((32, 30)) == 0


def test_clean_results_orders_by_length_match_then_synced():
    raw = [
        {"trackName": "far", "duration": 100, "syncedLyrics": "[00:01.00] a"},
        {"trackName": "near plain", "duration": 201, "plainLyrics": "a"},
        {"trackName": "near synced", "duration": 199, "syncedLyrics": "[00:01.00] a"},
        {"trackName": "empty", "duration": 200},
        {"trackName": "instr", "duration": 200, "instrumental": True, "plainLyrics": "x"},
        "junk",
    ]
    out = ex.clean_results(raw, 200_000)
    assert [r["track"] for r in out] == ["near synced", "near plain", "far"]
    assert out[0]["synced"] and not out[1]["synced"]


def test_lrclib_query_uses_fields_for_the_prefill(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_lrclib_search", lambda a, t: calls.append((a, t)) or [{"x": 1}])
    assert ex.lrclib_query("Art Song", "Art", "Song") == [{"x": 1}]
    assert calls == [("Art", "Song")]


def test_share_image_is_a_portrait_png_with_content():
    from PIL import Image
    cover = Image.new("RGB", (64, 64), (200, 40, 90))
    img = ex.render_share_image("光の中で 君を待ってる 😀", "next line", "Title", "Artist", cover,
                                [(40, 30, 90), (200, 60, 120)], (30, 215, 96))
    assert img.size == ex.SHARE_SIZE and img.mode == "RGB"
    # The big line is white text: some pixels near the middle are bright.
    mid = img.crop((100, 300, 980, 900)).convert("L")
    assert mid.getextrema()[1] > 240
    no_cover = ex.render_share_image("", "", "", "", None, [], (30, 215, 96))
    assert no_cover.size == ex.SHARE_SIZE


def test_history_store_pins(tmp_path):
    st = HistoryStore(str(tmp_path / "h.db"))
    st.save_lyrics(URI, "synced", [{"startMs": 0, "words": "spotify"}], [], "Spicy")
    assert st.get_pin(URI) is None
    st.pin_lyrics(URI, "synced", [{"startMs": 0, "words": "mine"}], [], ex.PIN_SOURCE)
    assert st.get_pin(URI)[1][0]["words"] == "mine"
    st.save_lyrics(URI, "synced", [{"startMs": 0, "words": "mine"}], [], ex.PIN_SOURCE)
    assert st.unpin_lyrics(URI) is True
    assert st.get_pin(URI) is None and st.get_lyrics(URI) is None   # the chosen copy is gone too
    assert st.unpin_lyrics(URI) is False
    st.close()


# ── Pins through the real bridge handler ─────────────────────────

class FakeWS:
    def __init__(self, msgs):
        self._msgs = [json.dumps(m) for m in msgs]

    async def send(self, data):
        pass

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._msgs:
            yield m


async def _nosleep(*_a, **_k):
    return None


def _drain():
    while True:
        try:
            main.event_queue.get_nowait()
        except _queue.Empty:
            return


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = HistoryStore(str(tmp_path / "hist.db"))
    monkeypatch.setattr(main, "_HISTORY_STORE", st)
    monkeypatch.setattr(main, "SAVE_HISTORY", True)
    yield st
    st.close()


def test_pinned_lyrics_survive_the_bridge_and_come_back_on_replay(store, monkeypatch):
    monkeypatch.setattr(main, "_start_play", lambda: None)
    monkeypatch.setattr(main.asyncio, "sleep", _nosleep)
    for k, v in {"track_uri": "", "synced": [], "plain": [], "lyrics_mode": "none"}.items():
        monkeypatch.setattr(main.state, k, v, raising=False)
    ex._STASH.clear()
    mine = [{"startMs": 0, "words": "my pick"}]
    store.pin_lyrics(URI, "synced", mine, [], ex.PIN_SOURCE)
    track = {"type": "track_change", "artist": "A", "title": "T", "track_uri": URI,
             "album_art": "", "duration_ms": 200_000}
    theirs = {"type": "lyrics", "track_uri": URI, "mode": "synced",
              "synced": [{"startMs": 0, "words": "spotify's"}], "plain": [], "source": "Spicy"}
    asyncio.run(main.ws_handler(FakeWS([track, theirs])))
    _drain()
    assert main.state.synced[0]["words"] == "my pick"
    assert ex._STASH[URI][1][0]["words"] == "spotify's"      # kept for "use Spotify's again"
    assert main._cached_lyrics(URI)[1][0]["words"] == "my pick"


# ── App ───────────────────────────────────────────────────────────

@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(main, "_register_hotkeys", lambda *a, **k: None)
    for attempt in range(3):
        try:
            a = main.App()
            break
        except tk.TclError as e:
            transient = ("installed properly" in str(e) or "tcl_findLibrary" in str(e))
            if not transient or attempt == 2:
                raise
            time.sleep(0.2)
    yield a
    try:
        a._np_fullscreen_exit()
    except Exception:
        pass
    for fn in (a._cancel_all_timers, a._tray_stop):
        try:
            fn()
        except Exception:
            pass
    a._alive = False
    a._root.destroy()
    del a
    import gc
    gc.collect()


class _Cap:
    def __init__(self, W, H):
        self.W, self.H, self.img = W, H, None

    def width(self):
        return self.W

    def height(self):
        return self.H

    def paste(self, im):
        self.img = im


LINES = [("first line", 2000), ("光の中で 君を待ってる", 5000), ("third line", 9000)]


@pytest.fixture
def page(app, monkeypatch):
    st = main.state
    for k, v in {"synced": [{"startMs": t, "words": w} for w, t in LINES], "lyrics_mode": "synced",
                 "plain": [], "instrumental_gaps": [], "duration_ms": 60000, "is_playing": True,
                 "track_uri": URI, "title": "Song", "artist": "Artist", "queue": [], "volume": 0.6,
                 "shuffle": False, "repeat": 0, "liked": False, "translation": {}}.items():
        monkeypatch.setattr(st, k, v, raising=False)
    monkeypatch.setattr(main, "_track_offset_ms", lambda uri=None: 0)
    monkeypatch.setattr(main, "RENDER_QUALITY", "high")
    monkeypatch.setattr(app, "_estimate_pos_ms", lambda: 5500)
    sent = []
    monkeypatch.setattr(main, "player_command", lambda a, **k: sent.append(("player", a)) or True)
    monkeypatch.setattr(main, "set_volume", lambda v: (sent.append(("volume", round(v, 2))),
                                                       setattr(st, "volume", v))[0] or True)
    monkeypatch.setattr(main, "skip_to_queue", lambda u, uid="": sent.append(("skip", u, uid)) or True)
    app.dot_sp.config(fg=main.ACCENT)
    app.lbl_title.config(text="Song")
    app._np_size = (540, 720)
    app._np_photo = _Cap(540, 720)
    app._np_relayout()
    app._update_sheet(force=True)
    app._np_render()
    app.sent = sent
    return app


def _keys(app):
    return {h[4] for h in app._np_hits}


def _center(app, key):
    for x1, y1, x2, y2, k in app._np_hits:
        if k == key:
            return (x1 + x2) // 2, (y1 + y2) // 2
    raise AssertionError(f"{key} not on the page: {sorted(map(str, _keys(app)))}")


class _E:
    def __init__(self, x, y, delta=0, widget=None):
        self.x, self.y, self.delta, self.state, self.widget = x, y, delta, 0, widget
        self.x_root, self.y_root = x, y


def _click(app, key):
    x, y = _center(app, key)
    app._np_on_click(_E(x, y))
    app._np_render()


def _settle(app):
    app._np_panel_t0 -= 10
    if app._np_menu:
        app._np_menu["t0"] -= 10
    app._np_render()
    app._np_render()


def test_new_controls_are_on_the_page(page):
    assert {"shuffle", "repeat", "like", "vol", "queue", "more", "overlay"} <= _keys(page)


def test_shuffle_repeat_like_send_commands(page):
    for k in ("shuffle", "repeat", "like"):
        _click(page, k)
    assert [s for s in page.sent if s[0] == "player"] == [("player", "shuffle"), ("player", "repeat"),
                                                          ("player", "like")]


def test_lit_states_change_the_footer_and_header(page):
    before = page._np_ftr[0].tobytes()
    main.state.shuffle, main.state.repeat = True, 2
    page._np_on_event("player_state")
    page._np_render()
    assert page._np_ftr[0].tobytes() != before
    h0 = page._np_hdr[0].tobytes()
    main.state.liked = True
    page._np_on_event("player_state")
    page._np_render()
    assert page._np_hdr[0].tobytes() != h0


def test_wheel_over_play_changes_volume_in_steps_and_shows_a_pill(page):
    x, y = _center(page, "play")
    page._np_on_wheel(_E(x, y, delta=120))
    assert page.sent[-1] == ("volume", 0.65)
    assert page._np_toast[0] == "Volume 65%"
    x, y = _center(page, "vol")
    page._np_on_wheel(_E(x, y, delta=-120))
    page._np_on_wheel(_E(x, y, delta=-120))
    assert page.sent[-1] == ("volume", 0.55)


def test_speaker_click_mutes_and_restores(page):
    _click(page, "vol")
    assert main.state.volume == 0.0 and page._np_toast[0] == "Muted"
    _click(page, "vol")
    assert main.state.volume == pytest.approx(0.6)


def test_keyboard_shortcuts(page):
    page._np_key_extra(_E(0, 0), "vol_up")
    assert page.sent[-1] == ("volume", 0.65)
    page._np_key_extra(_E(0, 0), "shuffle")
    assert page.sent[-1] == ("player", "shuffle")
    entry = tk.Entry(page._root)
    assert page._np_key_extra(_E(0, 0, widget=entry), "like") is None      # typing: ignored


def test_tab_shortcuts_follow_tab_order(page, monkeypatch):
    got = {}
    monkeypatch.setattr(page._root, "bind", lambda seq, fn=None: got.__setitem__(seq, fn))
    page._bind_shortcuts()
    shown = []
    monkeypatch.setattr(page, "_show", shown.append)
    for n in "1234":
        got[f"<Control-Key-{n}>"](None)
    assert shown == ["NOW PLAYING", "HISTORY", "STATS", "SETTINGS"]
    for seq in ("<Control-Up>", "<Control-Down>", "<Control-s>", "<Control-r>", "<Control-l>"):
        assert seq in got, seq


def test_up_next_lists_the_queue_and_skips(page):
    main.state.queue = [{"uri": f"spotify:track:q{n}", "uid": f"u{n}", "title": f"Song {n}",
                         "artist": "X", "album_art": "", "duration_ms": 180000} for n in range(10)]
    _click(page, "queue")
    _settle(page)
    rows = sorted(k for k in _keys(page) if str(k).startswith("q:"))
    assert rows and len(rows) <= ex.QUEUE_ROWS
    _click(page, "q:2")
    assert ("skip", "spotify:track:q2", "u2") in page.sent
    _settle(page)
    assert page._np_panel is None


def test_up_next_empty_and_live_update(page):
    page._np_panel_open("queue")
    _settle(page)
    assert not any(str(k).startswith("q:") for k in _keys(page))
    main.state.queue = [{"uri": "spotify:track:a", "uid": "", "title": "A", "artist": "B",
                         "album_art": "", "duration_ms": 0}]
    page._np_on_event("queue")
    page._np_render()
    assert "q:0" in _keys(page)


def test_escape_and_outside_click_close_the_panel(page):
    page._np_panel_open("queue")
    _settle(page)
    assert page._np_close_overlays() is True
    _settle(page)
    assert page._np_panel is None
    page._np_panel_open("queue")
    _settle(page)
    page._np_on_click(_E(20, 300))            # on the sheet, outside the card
    _settle(page)
    assert page._np_panel is None


def test_search_pick_pins_and_unpin_restores(page, store, monkeypatch):
    applied = []
    real_apply = main._apply_lyrics
    monkeypatch.setattr(main, "_apply_lyrics", lambda *a: (applied.append(a), real_apply(*a)))
    monkeypatch.setattr(main, "_save_history", lambda *a, **k: None)
    monkeypatch.setattr(trm, "on_lyrics", lambda *a, **k: None)
    ex._STASH.clear()
    raw = [{"trackName": "Song", "artistName": "Artist", "albumName": "LP", "duration": 60,
            "syncedLyrics": "[00:01.00] chosen words", "plainLyrics": ""}]
    monkeypatch.setattr(ex, "lrclib_query", lambda *a, **k: raw)
    page._np_panel_open("search")
    assert page._np_search_entry.get() == "Artist Song"
    # The worker hands its result back through the event queue.
    deadline = time.time() + 3
    while not page._np_search["results"] and time.time() < deadline:
        page._poll()
        page._root.update()
        time.sleep(0.02)
    assert len(page._np_search["results"]) == 1
    _settle(page)
    _click(page, "res:0")
    assert applied[-1][3] == ex.PIN_SOURCE
    assert main.state.synced[0]["words"] == "chosen words"
    assert store.get_pin(URI) is not None
    # A later bridge message for this track doesn't override the pick.
    assert ex.keep_pinned(URI, "synced", [{"startMs": 0, "words": "bridge"}], [], "Spicy") is True
    page._np_unpin_lyrics()
    assert store.get_pin(URI) is None
    assert main.state.synced[0]["words"] == "bridge"


def test_right_click_line_menu_copy_and_share(page, monkeypatch):
    line_key = next(k for k in sorted(map(str, _keys(page))) if k.startswith("line:")
                    and not page._ly["items"][int(k[5:])].get("dots"))
    x, y = _center(page, line_key)
    page._np_on_right(_E(x, y))
    _settle(page)
    assert any(str(k).startswith("menu:share:") for k in _keys(page))
    copied = []
    monkeypatch.setattr(page, "_to_clipboard", lambda t, what="": copied.append(t))
    _click(page, f"menu:copy_line:{line_key[5:]}")
    assert copied and copied[0] == page._ly["items"][int(line_key[5:])]["text"]
    assert page._np_menu is None
    # Copy image: rendered off the Tk thread, then put on the clipboard.
    import statusify_ui_stats
    got = []
    monkeypatch.setattr(statusify_ui_stats, "copy_image_to_clipboard", lambda img: got.append(img.size) or True)
    page._np_share("a line", "next", save=False)
    deadline = time.time() + 5
    while not got and time.time() < deadline:
        page._poll()
        page._root.update()
        time.sleep(0.02)
    assert got == [ex.SHARE_SIZE]


def test_outside_click_only_closes_a_menu(page):
    page._np_sheet_menu(100, 200)
    _settle(page)
    _click(page, "shuffle")
    assert page._np_menu is None
    assert ("player", "shuffle") not in page.sent


def test_sublines_add_height_and_cost_nothing_when_off(page, monkeypatch):
    plain_h = [it["h"] for it in page._ly["items"]]
    assert page._np_sublines(len(plain_h)) is None
    monkeypatch.setattr(trm, "subline_mode", lambda: "rom")
    monkeypatch.setattr(trm, "subline_for", lambda i: "Hikari no naka de" if i == 1 else None)
    main.state.translation = {1: {"rom": "Hikari no naka de"}}
    page._np_on_event("translation")
    page._np_render()
    items = page._ly["items"]
    row = next(i for i, it in enumerate(items) if it["text"].startswith("光"))
    assert items[row]["sub"] == ["Hikari no naka de"]
    assert items[row]["h"] > plain_h[row]
    assert items[row + 1]["y"] > items[row]["y"] + plain_h[row]
    # The sub-line is in the line's own mask, dimmer than the line.
    m = page._np_item_mask(items[row])
    lower = m.crop((0, m.size[1] - items[row]["slh"] - items[row]["m"], m.size[0], m.size[1]))
    assert 120 < lower.getextrema()[1] < 200


def test_footer_fits_at_minimum_width_with_overflow(page):
    page._np_size = (460, 640)
    page._np_photo = _Cap(460, 640)
    page._np_relayout()
    page._np_render()
    keys = _keys(page)
    assert "ov_more" in keys and "mini" in keys
    ctl = [h for h in page._np_hits if h[4] in ("dec", "inc", "delay", "mini", "overlay", "top", "copy",
                                                "ov_more")]
    ctl.sort(key=lambda h: h[0])
    for a, b in zip(ctl, ctl[1:]):
        assert a[2] <= b[0] + 1, (a, b)
    assert all(h[2] <= 460 for h in page._np_hits)
    over = {o[0] for o in page._np_overflow}
    _click(page, "ov_more")
    _settle(page)
    assert {f"menu:ov:{k}" for k in over} <= _keys(page)


def test_sleep_timer_shows_in_footer(page, monkeypatch):
    before = page._np_ftr[0].tobytes()
    monkeypatch.setattr(page, "_sleep_timer_label", lambda: "Sleep in 14:32")
    page._np_render()
    assert page._np_ftr[0].tobytes() != before


def test_overlay_action_toggles(page, monkeypatch):
    calls = []
    monkeypatch.setattr(page, "_toggle_overlay", lambda: calls.append(1))
    _click(page, "overlay")
    assert calls == [1]


def test_poll_routes_new_events(page, monkeypatch):
    seen = []
    monkeypatch.setattr(page, "_np_on_event", seen.append)
    for k in ("queue", "player_state", "translation", "beats"):
        main.event_queue.put((k,))
    main.event_queue.put(("np_call", lambda: seen.append("call")))
    page._poll()
    assert seen == ["queue", "player_state", "translation", "beats", "call"]


def test_tray_menu_has_overlay_items(app):
    if app._tray is None:
        pytest.skip("no tray backend")
    labels = [i.text for i in app._tray.menu.items if i.text]
    assert "Desktop overlay" in labels
    assert "Unlock overlay to move" in labels


def test_idle_frames_still_skip(page):
    page._np_render()
    page._np_want_frame = False
    page._np_last_busy = False
    page._np_render(force=False)
    assert page._np_render(force=False) is False
