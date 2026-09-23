"""v2.1 misc branch: lyric romanisation/translation, richer Discord presence,
sleep timer."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
import statusify_rpc as rpc_mod
import statusify_sleep as sleep_mod
import statusify_translate as tr

URI = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"


# ── Translation ──────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_translate(monkeypatch, tmp_path):
    """No network, a fresh cache file, and the setting forgotten."""
    def _no_net(*_a, **_k):
        raise AssertionError("network used in a test")
    monkeypatch.setattr(tr, "_post", _no_net)
    monkeypatch.setattr(tr, "_SUBLINE", None)
    monkeypatch.setattr(tr, "_TARGET", None)
    monkeypatch.setattr(tr, "_net_error_logged", False)
    monkeypatch.setattr(tr, "_db_path", lambda: str(tmp_path / "translations.db"))
    tr.close()
    tr._current.update(uri=None, data={})
    yield
    tr.close()


def _drain():
    while True:
        try:
            main.event_queue.get_nowait()
        except Exception:
            return


def test_hangul_revised_romanization():
    assert tr.romanise_hangul("사랑해요") == "saranghaeyo"
    assert tr.romanise_hangul("안녕하세요") == "annyeonghaseyo"
    assert tr.romanise_hangul("한국어") == "hangugeo"          # liaison
    assert tr.romanise_hangul("감사합니다") == "gamsahamnida"   # nasalisation
    assert tr.romanise_hangul("신라") == "silla"                # liquid assimilation
    assert tr.romanise_hangul("너를 사랑해!") == "neoreul saranghae!"


def test_cyrillic_and_greek_transliteration():
    assert tr.romanise_cyrillic("Привет, мир") == "Privet, mir"
    assert tr.romanise_cyrillic("Щука") == "Shchuka"
    assert tr.romanise_cyrillic("Љубав и ђак") == "Ljubav i đak"
    assert tr.romanise_greek("Καλημέρα κόσμε") == "Kalimera kosme"
    assert tr.romanise_greek("ούτε") == "oute"


def test_script_detection():
    assert not tr.needs_romanisation("Hello, world")
    assert not tr.needs_romanisation("Café señor über")
    for s in ("こんにちは", "我爱你", "사랑", "Привет", "Γειά", "สวัสดี", "مرحبا", "שלום", "नमस्ते"):
        assert tr.needs_romanisation(s), s


def test_offline_romanisation_needs_network_for_cjk_without_libs(monkeypatch):
    monkeypatch.setattr(tr, "_KAKASI", None)
    monkeypatch.setattr(tr, "_pinyin", lambda: None)
    assert tr.romanise_offline("こんにちは") is None
    assert tr.romanise_offline("我爱你") is None
    assert tr.romanise_offline("สวัสดี") is None
    assert tr.romanise_offline("사랑해") == "saranghae"
    assert tr.romanise_offline("hello") is None


def test_locale_to_lang():
    assert tr.locale_to_lang("en_US") == "en"
    assert tr.locale_to_lang("sr_Latn_RS") == "sr"
    assert tr.locale_to_lang("zh_CN") == "zh-CN"
    assert tr.locale_to_lang("zh_TW") == "zh-TW"
    assert tr.locale_to_lang(None) == "en"
    assert tr.locale_to_lang("C") == "en"
    assert tr.system_lang()                 # never empty


# The shape Google returned for "こんにちは世界\n|\n愛してる\n|\nさよなら".
REAL = [[["hello world\n", "こんにちは世界\n", None, None, 3], ["|\n", "|\n", None, None, 3],
         ["I love you\n", "愛してる\n", None, None, 3], ["|\n", "|\n", None, None, 3],
         ["Goodbye", "さよなら", None, None, 3],
         [None, None, None, "Kon'nichiwa sekai | itoshi teru | sayonara"]],
        None, "ja"]


def test_parse_response_splits_on_the_separator():
    trs, roms, src = tr.parse_response(REAL, 3)
    assert trs == ["hello world", "I love you", "Goodbye"]
    assert roms == ["Kon'nichiwa sekai", "itoshi teru", "sayonara"]
    assert src == "ja"


def test_parse_response_drops_misaligned_answers():
    trs, roms, _ = tr.parse_response(REAL, 4)
    assert trs is None and roms is None
    assert tr.parse_response(None, 1) == (None, None, "")
    assert tr.parse_response([[], None, None], 1) == (None, None, "")


def _fake_google(calls):
    """A post() stand-in: 'translates' by prefixing t: and 'romanises' by
    prefixing r:, in the endpoint's shape."""
    def post(tl, text):
        calls.append((tl, text))
        parts = text.split("\n|\n")
        segs = [["t:" + p + ("\n|\n" if i < len(parts) - 1 else ""), p, None, None, 3]
                for i, p in enumerate(parts)]
        segs.append([None, None, None, " | ".join("r:" + p for p in parts)])
        return [segs, None, "ja"]
    return post


def test_compute_translation_and_romanisation(monkeypatch):
    monkeypatch.setattr(tr, "_KAKASI", None)
    calls = []
    lines = ["こんにちは", "", "사랑해", "hello", "こんにちは"]
    res, done = tr.compute(lines, "en", {"rom", "tr"}, post=_fake_google(calls))
    assert done == {"rom", "tr"}
    # One batch per script (cjk, hangul, latin); the repeated line sent once.
    assert len(calls) == 3
    assert sum(text.count("こんにちは") for _, text in calls) == 1
    assert res[0] == {"rom": "r:こんにちは", "tr": "t:こんにちは"}
    assert res[1] == {"rom": None, "tr": None}
    assert res[2]["rom"] == "saranghae"        # offline, not Google's
    assert res[3] == {"rom": None, "tr": "t:hello"}
    assert res[4] == res[0]


def test_lines_are_batched_by_script():
    assert tr.script_group("東京") == tr.script_group("こんにちは") == "cjk"
    assert tr.script_group("사랑") == "hangul"
    assert tr.script_group("hello") == "latin"
    assert tr.script_group("สวัสดี") == "thai"
    assert tr.script_group("oh 사랑") == "hangul"


def test_compute_skips_translation_into_the_source_language():
    calls = []
    res, done = tr.compute(["Привет"], "ja", {"rom", "tr"}, post=_fake_google(calls))
    assert res[0]["tr"] is None                # detected "ja" == target
    assert res[0]["rom"] == "Privet"


def test_compute_romanised_only_stays_offline_when_it_can():
    res, done = tr.compute(["사랑해", "hello"], "en", {"rom"})   # _post would raise
    assert res[0]["rom"] == "saranghae" and res[1]["rom"] is None
    assert done == {"rom"}


def test_network_failure_is_silent_and_not_cached(monkeypatch):
    logs = []
    monkeypatch.setattr(main, "log", logs.append)
    def boom(*_a):
        raise OSError("offline")
    res, done = tr.compute(["hello"], "de", {"tr"}, post=boom)
    tr.compute(["again"], "de", {"tr"}, post=boom)
    assert res[0]["tr"] is None and done == set()
    assert len([l for l in logs if "translation unavailable" in l]) == 1


def test_request_stores_state_posts_event_and_caches(monkeypatch):
    monkeypatch.setattr(tr, "_KAKASI", None)
    monkeypatch.setattr(main.state, "track_uri", URI, raising=False)
    _drain()
    got = []
    calls = []
    fut = tr.request(URI, ["こんにちは", "hello"], lambda u, r: got.append((u, r)),
                     want={"rom", "tr"}, lang="en", post=_fake_google(calls))
    fut.result(5)
    assert main.state.translation[1]["tr"] == "t:hello"
    assert got and got[0][0] == URI
    assert main.event_queue.get_nowait() == ("translation",)
    # A replay comes from the cache: no network at all.
    def no_net(*_a):
        raise AssertionError("should be cached")
    main.state.translation = {}
    tr.request(URI, ["こんにちは", "hello"], want={"tr"}, lang="en", post=no_net).result(5)
    assert main.state.translation[0]["rom"] == "r:こんにちは"
    assert main.state.translation[1]["tr"] == "t:hello"


def test_request_for_a_skipped_track_is_not_applied(monkeypatch):
    monkeypatch.setattr(main.state, "track_uri", "spotify:track:other", raising=False)
    monkeypatch.setattr(main.state, "translation", {}, raising=False)
    _drain()
    tr.request(URI, ["hello"], want={"tr"}, lang="de", post=_fake_google([])).result(5)
    assert main.state.translation == {}
    assert main.event_queue.empty()


def test_subline_for_follows_the_setting(monkeypatch):
    monkeypatch.setattr(main.state, "track_uri", URI, raising=False)
    monkeypatch.setattr(main, "_cfg_set", lambda *a: None)
    main.state.translation = {0: {"rom": "ai", "tr": "love"}, 1: {"rom": None, "tr": None}}
    tr._current.update(uri=URI, data=main.state.translation)
    monkeypatch.setattr(tr, "refresh", lambda: None)
    tr.set_subline_mode("off")
    assert tr.subline_for(0) is None
    tr.set_subline_mode("rom")
    assert tr.subline_for(0) == "ai"
    tr.set_subline_mode("tr")
    assert tr.subline_for(0) == "love"
    tr.set_subline_mode("both")
    assert tr.subline_for(0) == "ai  ·  love"
    assert tr.sublines_for(0) == ("ai", "love")
    assert tr.subline_for(1) is None and tr.subline_for(99) is None
    main.state.track_uri = "spotify:track:next"            # stale data never shows
    assert tr.subline_for(0) is None


def test_apply_lyrics_requests_only_when_enabled(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_save_history", lambda *a, **k: None)
    monkeypatch.setattr(tr, "request", lambda uri, lines, *a, **k: seen.append((uri, lines)))
    monkeypatch.setattr(main.state, "track_uri", URI, raising=False)
    monkeypatch.setattr(tr, "_SUBLINE", "off")
    main._apply_lyrics("synced", [{"startMs": 0, "words": "こんにちは"}], [], "test")
    assert seen == [] and main.state.translation == {}
    monkeypatch.setattr(tr, "_SUBLINE", "both")
    main._apply_lyrics("synced", [{"startMs": 0, "words": "こんにちは"}], [], "test")
    main._apply_lyrics("plain", [], ["line a", "line b"], "test")
    assert seen == [(URI, ["こんにちは"]), (URI, ["line a", "line b"])]


# ── Discord presence ─────────────────────────────────────────────
@pytest.fixture
def rpc_env(monkeypatch):
    monkeypatch.setattr(rpc_mod, "uri_fn", lambda: URI)
    monkeypatch.setattr(rpc_mod, "album_fn", lambda: "Some Album")
    monkeypatch.setattr(rpc_mod, "link_track", True)
    monkeypatch.setattr(rpc_mod, "listen_button", True)
    return rpc_mod.DiscordRPC("123")


def test_activity_has_album_hover_and_listen_button(rpc_env):
    act = rpc_env._activity("Song", "Artist", ["a lyric line"], "spotify:image:x", 1000, 200_000)
    assert act["type"] == 2                                  # Listening, still
    assert act["assets"]["large_text"] == "Some Album"
    assert act["buttons"] == [{"label": "Listen on Spotify",
                               "url": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"}]
    assert len(act["buttons"][0]["label"]) <= 32
    assert act["state"] == "a lyric line"                    # the live lyric line
    assert act["details"] == "Song — Artist"
    assert "timestamps" in act


def test_listen_button_can_be_turned_off(rpc_env, monkeypatch):
    monkeypatch.setattr(rpc_mod, "listen_button", False)
    assert "buttons" not in rpc_env._activity("S", "A", [], "")
    # The button doesn't depend on the title link setting.
    monkeypatch.setattr(rpc_mod, "listen_button", True)
    monkeypatch.setattr(rpc_mod, "link_track", False)
    act = rpc_env._activity("S", "A", [], "")
    assert act["buttons"] and "details_url" not in act


def test_no_button_for_local_files_or_podcasts(rpc_env, monkeypatch):
    monkeypatch.setattr(rpc_mod, "uri_fn", lambda: "spotify:local:a:b:c:1")
    assert "buttons" not in rpc_env._activity("S", "A", [], "")


def test_hover_text_falls_back_when_album_missing_or_too_short(rpc_env, monkeypatch):
    monkeypatch.setattr(rpc_mod, "album_fn", lambda: "")
    assert rpc_env._activity("S", "Artist", [], "")["assets"]["large_text"] == "Artist"
    monkeypatch.setattr(rpc_mod, "album_fn", lambda: "V")      # Discord needs >= 2 chars
    assert rpc_env._activity("S", "Artist", [], "")["assets"]["large_text"] == "Artist"


def test_main_wires_album_into_the_presence(monkeypatch):
    monkeypatch.setattr(main.state, "album", "Main Album", raising=False)
    assert rpc_mod.album_fn() == "Main Album"


class _FakeWS:
    def __init__(self, msgs):
        self._msgs = [json.dumps(m) for m in msgs]
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._msgs:
            yield m


def test_track_change_parses_album(monkeypatch):
    async def nosleep(*_a, **_k):
        pass
    monkeypatch.setattr(main.asyncio, "sleep", nosleep)
    for fn in ("_save_history", "_on_track_start", "_start_play", "_finish_play"):
        monkeypatch.setattr(main, fn, lambda *a, **k: None)
    monkeypatch.setattr(main, "_cached_lyrics", lambda uri: None)
    msg = {"type": "track_change", "artist": "A", "title": "T", "track_uri": URI,
           "album_art": "", "duration_ms": 1000, "album": "The Album"}
    asyncio.run(main.ws_handler(_FakeWS([msg])))
    assert main.state.album == "The Album"
    del msg["album"]                                  # older bridge
    asyncio.run(main.ws_handler(_FakeWS([msg])))
    assert main.state.album == ""


def test_bridge_sends_album():
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "lyrics-bridge.js"), encoding="utf-8").read()
    assert "album: item.metadata?.album_title" in js


# ── Sleep timer ──────────────────────────────────────────────────
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _St:
    track_uri = URI
    duration_ms = 200_000
    position_ms = 0
    is_playing = True


def test_minutes_timer_pauses_once_at_the_deadline():
    clock, paused = _Clock(), []
    t = sleep_mod.SleepTimer(clock=clock, pause=lambda: paused.append(1))
    t.set(15)
    assert t.value() == "15" and t.remaining() == 900
    assert t.label() == "Sleep in 15:00"
    clock.t += 899
    assert not t.tick() and paused == []
    assert t.label() == "Sleep in 0:01"
    clock.t += 1
    assert t.tick() and paused == [1]
    assert not t.active and t.value() == "off" and t.label() == ""
    clock.t += 100
    assert not t.tick() and paused == [1]


def test_hour_label_and_cancel():
    clock = _Clock()
    t = sleep_mod.SleepTimer(clock=clock)
    t.set(60)
    assert t.label() == "Sleep in 1:00:00"
    t.set(None)
    assert t.remaining() is None and not t.active


def test_end_of_song_pauses_just_before_the_end():
    st, paused = _St(), []
    t = sleep_mod.SleepTimer(pause=lambda: paused.append(1), state=lambda: st)
    t.set("eos")
    assert t.value() == "eos"
    st.position_ms = 150_000
    assert t.remaining() == 50 and t.label() == "Sleep at end of song · 0:50"
    assert not t.tick()
    st.position_ms = 199_700
    assert t.tick() and paused == [1] and not t.active


def test_end_of_song_fires_if_the_next_song_already_started():
    st, paused = _St(), []
    t = sleep_mod.SleepTimer(pause=lambda: paused.append(1), state=lambda: st)
    t.set("eos")
    st.track_uri, st.position_ms = "spotify:track:next", 0
    assert t.tick() and paused == [1]


def test_end_of_song_waits_while_paused():
    st = _St()
    st.position_ms, st.is_playing = 199_900, False
    t = sleep_mod.SleepTimer(state=lambda: st)
    t.set("eos")
    assert not t.tick() and t.active


# ── GUI ──────────────────────────────────────────────────────────
from test_gui_smoke import app  # noqa: E402,F401  (the real-window fixture)


def test_app_sleep_timer_methods(app, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "player_command", lambda a: sent.append(a))
    app._sleep_timer_set(30)
    assert app._sleep_timer_label().startswith("Sleep in 30:00") or \
        app._sleep_timer_label().startswith("Sleep in 29:5")
    assert 1790 < app._sleep_timer_remaining() <= 1800
    assert "sleeptick" in app._timers
    app._sleep_timer.deadline = app._sleep_timer.clock() - 1
    app._sleep_timer_tick()
    assert sent == ["pause"]
    assert app._sleep_timer_remaining() is None and app._sleep_timer_label() == ""
    app._sleep_timer_set(None)


def test_settings_page_builds_new_rows(app, monkeypatch):
    monkeypatch.setattr(main, "_cfg_set", lambda *a: None)
    monkeypatch.setattr(tr, "refresh", lambda: None)
    app._build_deferred_pages()
    app._show("SETTINGS")
    for _ in range(20):
        app._root.update()
    assert app.lbl_sleep.cget("text") == "Off"
    assert app.lbl_translate_to.cget("text").startswith("Auto")
    app._sleep_timer_set("eos")
    app._root.update()
    assert app._sleep_seg.get() == "eos"
    app._sleep_timer_set(None)
    assert app.lbl_sleep.cget("text") == "Off"
    app._subline_seg.choose("tr")
    assert tr.subline_mode() == "tr"
