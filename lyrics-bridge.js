(async function lyricsBridge() {
    while (!Spicetify?.Player?.data || !Spicetify?.CosmosAsync) {
        await new Promise(r => setTimeout(r, 300));
    }
    await new Promise(r => setTimeout(r, 1000));
    console.log("[LyricsBridge] Loaded.");

    // ── Spicy Lyrics API version ─────────────────────────────────
    // Sent in the SpicyLyrics-Version header and the request body. The API is
    // lenient about the exact value (6.1.1 is still accepted even though the
    // live extension is on 6.3.x), so this does NOT need to track releases
    // release-for-release.
    //
    // NOTE for future maintainers: the "lyrics silently came from Spotify"
    // outage was NOT a version-pin problem. Spicy Lyrics 6.x changed the
    // response *format* — queries[0] became a "_notice" object (pushing the
    // result to a later index) and result.data became an SLObjPack-packed
    // [valuesList, stream] payload instead of a plain object. The old parser
    // read queries[0].result.data.Content, found nothing, and fell back to
    // Spotify for every track. The fix lives in fetchSpicyLyrics/slUnpack
    // below, not here. See: https://github.com/Spikerko/spicy-lyrics/releases
    const SPICY_VERSION = "6.1.1";

    let ws             = null;
    let reconnectTimer = null;
    let lastTrackUri   = "";   // tracks what we last sent a track_change for
    // Remembers why the last Spicy attempt failed, so the fallback can say so
    // instead of silently downgrading.
    let lastSpicyError = "";

    // ── In-memory storage for synced lyrics ──────────────────────
    let currentLyrics = null;  // { mode: "synced", synced: [{startMs, words}, ...], plain: [...] }

    function connect() {
        if (ws?.readyState === WebSocket.OPEN) return;
        ws = new WebSocket("ws://127.0.0.1:8765");

        ws.onopen = async () => {
            console.log("[LyricsBridge] Connected.");
            clearTimeout(reconnectTimer);
            lastTrackUri = "";
            // Retry loop in case Player.data isn't populated immediately.
            for (let attempt = 0; attempt < 8; attempt++) {
                if (ws?.readyState !== WebSocket.OPEN) break;
                const item = Spicetify.Player.data?.item;
                const uri  = item?.uri;
                console.log(`[LyricsBridge] startup attempt ${attempt} — uri:`, uri);
                if (uri) {
                    lastTrackUri = uri;
                    const posMs   = Spicetify.Player.getProgress();
                    const durMs   = parseInt(item.metadata?.["duration"] || item.duration_ms || 0);
                    const playing = Spicetify.Player.isPlaying();
                    // Always send track info — don't gate on isPlaying() which can
                    // transiently return false even when music is playing.
                    await sendTrackAndLyrics(item);
                    send({ type: "position", position_ms: posMs, duration_ms: durMs, is_playing: playing });
                    if (!playing) send({ type: "paused" });
                    break;
                }
                await new Promise(r => setTimeout(r, 300));
            }
        };

        ws.onmessage = async (ev) => {
            try {
                const msg = JSON.parse(ev.data);
                if (msg.type === "request_state") {
                    // Do NOT clear fetchingUris here. onopen has usually just
                    // started a fetch for this very track; clearing the guard
                    // let a second one run alongside it, sending track_change
                    // and lyrics twice. sendTrackAndLyrics' finally{} already
                    // guarantees the set can't wedge.
                    lastTrackUri = "";
                    const item = Spicetify.Player.data?.item;
                    if (item?.uri) {
                        lastTrackUri = item.uri;
                        const posMs   = Spicetify.Player.getProgress();
                        const durMs   = parseInt(item.metadata?.["duration"] || item.duration_ms || 0);
                        const playing = Spicetify.Player.isPlaying();
                        await sendTrackAndLyrics(item);
                        send({ type: "position", position_ms: posMs, duration_ms: durMs, is_playing: playing });
                        if (!playing) send({ type: "paused" });
                    }
                } else if (msg.type === "skip_track") {
                    Spicetify.Player.next();
                } else if (msg.type === "skip_instrumental") {
                    // ── Skip instrumental: seek to the next lyric line ──
                    if (!currentLyrics || currentLyrics.mode !== "synced" || !currentLyrics.synced?.length) {
                        console.log("[LyricsBridge] Skip instrumental: no synced lyrics available");
                        return;
                    }
                    const posMs = Spicetify.Player.getProgress();
                    // Find the first lyric line that starts AFTER the current position
                    const nextLine = currentLyrics.synced.find(line => line.startMs > posMs + 500);
                    if (nextLine) {
                        console.log(`[LyricsBridge] Skip instrumental: seeking from ${posMs}ms to ${nextLine.startMs}ms`);
                        Spicetify.Player.seek(nextLine.startMs);
                    } else {
                        console.log("[LyricsBridge] Skip instrumental: no next lyric found (might be near end of song)");
                    }
                }
            } catch(e) {}
        };

        ws.onclose = () => {
            ws = null;
            reconnectTimer = setTimeout(connect, 3000);
        };

        ws.onerror = () => {
            if (ws) { ws.close(); ws = null; }
        };
    }

    function send(obj) {
        if (ws?.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify(obj)); } catch(e) {}
        }
    }

    function getSpotifyToken() {
        try {
            const t = Spicetify.Platform.AuthorizationAPI._tokenProvider?._token?.accessToken
                   || Spicetify.Platform.AuthorizationAPI._tokenProvider?.token?.accessToken
                   || Spicetify.Platform.AuthorizationAPI?.token?.accessToken
                   || Spicetify.Platform.Session?.accessToken
                   || Spicetify._token?.accessToken
                   || null;
            if (!t) console.warn("[LyricsBridge] No token found. AuthorizationAPI keys:",
                Object.keys(Spicetify.Platform?.AuthorizationAPI || {}));
            return t;
        } catch(e) {
            console.warn("[LyricsBridge] Token error:", e.message);
            return null;
        }
    }

    function getAlbumArt(item) {
        const uri = item.metadata?.["image_url"]
                 || item.metadata?.["image_xlarge_url"]
                 || item.metadata?.["image_large_url"];
        if (uri?.startsWith("spotify:image:"))
            return `https://i.scdn.co/image/${uri.replace("spotify:image:", "")}`;
        return uri || item.album?.images?.[0]?.url || "";
    }

    // ── SLObjPack decoder ────────────────────────────────────────
    // Spicy Lyrics 6.x no longer returns lyrics as a plain JSON object:
    // result.data is a packed payload [valuesList, stream] produced by the
    // extension's own SLObjPack packer. This is a faithful port of its
    // unpack() — https://github.com/Spikerko/spicy-lyrics, src/utils/objpack.ts
    // — keeping the prototype-pollution guards (safeSet/forbidden keys) and
    // the size/decode budgets intact. Not decoding this is why every Spicy
    // call looked empty and the pipeline silently fell back to Spotify.
    function slUnpack(packed) {
        const L = { depth: 512, arrayLength: 1 << 20, objectKeys: 1 << 16,
                    streamLength: 1 << 24, valuesLength: 1 << 22, decodeOps: 1 << 22 };
        const FORBIDDEN = new Set(["__proto__", "constructor", "prototype"]);
        if (!Array.isArray(packed) || packed.length !== 2) throw new Error("bad payload shell");
        const valuesList = packed[0], stream = packed[1];
        if (!Array.isArray(valuesList) || !Array.isArray(stream)) throw new Error("bad payload arrays");
        if (valuesList.length > L.valuesLength) throw new Error("valuesList too big");
        if (stream.length > L.streamLength) throw new Error("stream too big");
        for (let i = 0; i < valuesList.length; i++) {
            const v = valuesList[i]; if (v === null) continue; const t = typeof v;
            if (t === "string" || t === "boolean") continue;
            if (t === "number") { if (!Number.isFinite(v)) throw new Error("non-finite@" + i); continue; }
            throw new Error("bad valuesList entry@" + i + " (" + t + ")");
        }
        const streamLen = stream.length, valuesLen = valuesList.length; let cursor = 0;
        const readStream = () => { if (cursor >= streamLen) throw new Error("end of stream"); return stream[cursor++]; };
        const resolvePointer = (p) => {
            if (typeof p !== "number" || !Number.isInteger(p) || p < 0 || p >= valuesLen) throw new Error("bad ptr " + p);
            return valuesList[p];
        };
        const readKey = () => {
            const k = resolvePointer(readStream());
            if (typeof k !== "string") throw new Error("key not string");
            if (FORBIDDEN.has(k)) throw new Error("forbidden key");
            return k;
        };
        const safeSet = (o, k, v) => Object.defineProperty(o, k, { value: v, writable: true, enumerable: true, configurable: true });
        const validateCount = (n, max, label) => {
            if (typeof n !== "number" || !Number.isInteger(n) || n < 0 || n > max) throw new Error("bad " + label + " count " + n);
        };
        const requireStream = (min, label) => { if (min > streamLen - cursor) throw new Error(label + " exceeds stream"); };
        function decode(depth) {
            if (depth > L.depth) throw new Error("max depth");
            const op = readStream();
            if (typeof op !== "number" || !Number.isInteger(op)) throw new Error("bad opcode " + op);
            if (op >= 0) return resolvePointer(op);
            switch (op) {
                case -1: {
                    const nk = readStream(); validateCount(nk, L.objectKeys, "object key"); requireStream(nk * 2, "object");
                    const keys = new Array(nk); for (let i = 0; i < nk; i++) keys[i] = readKey();
                    const obj = {}; for (let i = 0; i < nk; i++) safeSet(obj, keys[i], decode(depth + 1)); return obj;
                }
                case -2: {
                    const ni = readStream(); validateCount(ni, L.arrayLength, "array item"); requireStream(ni, "array");
                    const arr = new Array(ni); for (let i = 0; i < ni; i++) arr[i] = decode(depth + 1); return arr;
                }
                case -3: {
                    const ni = readStream(); validateCount(ni, L.arrayLength, "schema item");
                    const nk = readStream(); validateCount(nk, L.objectKeys, "schema key");
                    if (ni * nk > L.decodeOps) throw new Error("schema budget");
                    requireStream(nk + ni * nk, "schema array");
                    const keys = new Array(nk); for (let i = 0; i < nk; i++) keys[i] = readKey();
                    const arr = new Array(ni);
                    for (let i = 0; i < ni; i++) { const obj = {}; for (let k = 0; k < nk; k++) safeSet(obj, keys[k], decode(depth + 1)); arr[i] = obj; }
                    return arr;
                }
                case -4: return [];
                case -5: return [decode(depth + 1)];
                case -6: return {};
                default: throw new Error("unknown opcode " + op);
            }
        }
        const result = decode(0);
        if (cursor !== streamLen) throw new Error("extra data (" + cursor + "/" + streamLen + ")");
        return result;
    }

    // Parse a DECODED Spicy lyrics object ({ Type, Content, ... }) into the
    // { mode, synced, plain } shape the RPC loop consumes.
    function parseSpicyLyrics(result) {
        if (!result?.Content?.length) return null;

        const type    = result.Type;
        const content = result.Content;
        const synced  = [];

        if (type === "Syllable") {
            // Each Content entry is one display line.
            // Priority for reconstructing line text:
            //   1. entry.Text      — full line text Spicy attaches to the Content entry
            //   2. lead.Text       — full line text on the Lead object (less common)
            //   3. syllable join   — concatenate syllable Text fields with a space between
            //                        each one (simple, reliable, avoids smashed words)
            for (const entry of content) {
                if (entry.Type !== "Vocal") continue;
                const lead = entry.Lead;
                if (!lead?.Syllables?.length) continue;

                // 1. Content-level text (most reliable)
                let text = (entry.Text || "").trim();

                // 2. Lead-level text
                if (!text) text = (lead.Text || "").trim();

                // 3. Join syllables — no space between syllables of the same word.
                // Spotify marks the last syllable of each word with IsPartOfWord=false
                // or a trailing space in the Text field. We detect a word boundary by
                // checking if the syllable's Text ends with a space, or if the API flag
                // IsPartOfWord is explicitly false (varies by API version).
                if (!text) {
                    const parts = [];
                    for (let i = 0; i < lead.Syllables.length; i++) {
                        const syl  = lead.Syllables[i];
                        const raw  = syl.Text || "";
                        // A trailing space in the raw text signals end-of-word
                        const endsWord = raw.endsWith(" ") ||
                                         syl.IsPartOfWord === false ||
                                         syl.isPartOfWord === false ||
                                         i === lead.Syllables.length - 1;
                        parts.push(raw.trimEnd());
                        if (endsWord && i < lead.Syllables.length - 1) {
                            parts.push(" ");
                        }
                    }
                    text = parts.join("").trim();
                }

                if (!text || text === "♪") continue;
                const startMs = Math.round((lead.StartTime || lead.Syllables[0]?.StartTime || 0) * 1000);
                synced.push({ startMs, words: text });
            }
        } else if (type === "Line") {
            for (const entry of content) {
                if (entry.Type !== "Vocal") continue;
                const text = (entry.Text || "").trim();
                if (!text || text === "♪") continue;
                const startMs = Math.round((entry.StartTime || 0) * 1000);
                synced.push({ startMs, words: text });
            }
        }

        if (!synced.length) return null;
        console.log(`[LyricsBridge] Parsed ${synced.length} lines (${type}) from Spicy Lyrics`);
        return { mode: "synced", synced, plain: [] };
    }

    async function fetchSpicyLyrics(trackUri) {
        const trackId = trackUri.split(":").pop();
        const token   = getSpotifyToken();
        lastSpicyError = "";
        if (!token) {
            lastSpicyError = "no Spotify auth token yet";
            console.warn("[LyricsBridge] No Spotify token.");
            send({ type: "lyrics_debug", message: "Spicy skipped — no Spotify auth token yet" });
            return null;
        }
        try {
            const resp = await fetch("https://api.spicylyrics.org/query", {
                method:  "POST",
                headers: {
                    "Content-Type":        "application/json",
                    "SpicyLyrics-Version": SPICY_VERSION,
                    "X-mode":              "2",
                    "SpicyLyrics-WebAuth": `Bearer ${token}`,
                },
                body: JSON.stringify({
                    queries: [{ operation: "lyrics", variables: { id: trackId, auth: "SpicyLyrics-WebAuth" }}],
                    client:  { version: SPICY_VERSION }
                })
            });
            if (resp.status !== 200) {
                lastSpicyError = `HTTP ${resp.status}`;
                // 400/403/426 here almost always means SPICY_VERSION is stale.
                const hint = (resp.status === 400 || resp.status === 403 || resp.status === 426)
                    ? ` — SPICY_VERSION ${SPICY_VERSION} may be outdated`
                    : "";
                console.warn("[LyricsBridge] Spicy API returned status:", resp.status);
                send({ type: "lyrics_debug", message: `Spicy API HTTP ${resp.status}${hint}` });
                return null;
            }
            const data = await resp.json();

            // The response envelope changed in Spicy Lyrics 6.x, in two ways
            // that each independently broke the old parser:
            //   1. queries[0] is now a legal "_notice" string object, so the
            //      lyrics result is no longer at a fixed index — it must be
            //      found by its operation name.
            //   2. result.data is no longer a plain { Type, Content } object;
            //      it is an SLObjPack packed payload [valuesList, stream] that
            //      has to be unpacked first.
            // On failure the query still answers HTTP 200, carrying the real
            // status per-query as result.httpStatus / result.error instead of
            // a packed payload.
            const query = Array.isArray(data?.queries)
                ? data.queries.find(x => x && x.operation === "lyrics")
                : null;
            const qres  = query?.result;
            const inner = qres?.httpStatus;
            const err   = qres?.error || qres?.data?.error;
            if (!qres || err || (inner && inner !== 200)) {
                lastSpicyError = err
                    ? `${inner || "error"}: ${err}`
                    : (inner ? `inner ${inner}` : "no lyrics query in response");
                // 401/403 here is an auth problem, NOT a missing-lyrics one.
                const hint = (inner === 401 || inner === 403)
                    ? " — auth rejected, not a missing-lyrics problem"
                    : "";
                console.warn("[LyricsBridge] Spicy query failed:", trackId, inner, err);
                send({ type: "lyrics_debug",
                       message: `Spicy ${inner || "error"} for ${trackId}: ${err || lastSpicyError}${hint}` });
                return null;
            }

            // Decode the packed payload back into the lyrics object.
            let lyricsObj;
            try {
                lyricsObj = slUnpack(qres.data);
            } catch (e) {
                lastSpicyError = `decode failed: ${e.message}`;
                console.warn("[LyricsBridge] SLObjPack decode failed:", trackId, e.message);
                send({ type: "lyrics_debug", message: `Spicy decode failed for ${trackId}: ${e.message}` });
                return null;
            }

            const result = parseSpicyLyrics(lyricsObj);
            if (!result) {
                // Genuinely nothing to parse — this really is a catalogue miss.
                console.warn("[LyricsBridge] Spicy returned no content:", trackId,
                    "type:", lyricsObj?.Type);
                lastSpicyError = "no lyrics in Spicy catalogue";
                send({ type: "lyrics_debug",
                       message: `Spicy: no lyrics for ${trackId} (type: ${lyricsObj?.Type || "none"})` });
            }
            return result;
        } catch(e) {
            lastSpicyError = e.message || "network error";
            console.warn("[LyricsBridge] Spicy fetch failed:", e.message);
            send({ type: "lyrics_debug", message: `Spicy fetch error: ${e.message}` });
            return null;
        }
    }

    async function fetchSpotifyLyrics(trackUri) {
        const trackId = trackUri.split(":").pop();
        try {
            const res      = await Spicetify.CosmosAsync.get(
                `https://spclient.wg.spotify.com/color-lyrics/v2/track/${trackId}?format=json&market=from_token`
            );
            const lines    = res?.lyrics?.lines;
            const syncType = res?.lyrics?.syncType;
            if (!lines?.length) {
                send({ type: "lyrics_debug", message: `Spotify: no lyrics for ${trackId} (syncType: ${syncType || "none"})` });
                return null;
            }
            if (syncType === "LINE_SYNCED") {
                return {
                    mode:   "synced",
                    synced: lines
                        .map(l => ({ startMs: parseInt(l.startTimeMs||0), words: (l.words||"").trim() }))
                        .filter(l => l.words && l.words !== "♪"),
                    plain: []
                };
            } else {
                return {
                    mode:   "plain",
                    synced: [],
                    plain:  lines.map(l => (l.words||"").trim()).filter(w => w && w !== "♪")
                };
            }
        } catch(e) {
            send({ type: "lyrics_debug", message: `Spotify lyrics error: ${e.message}` });
            return null;
        }
    }

    // Track which URIs are currently being fetched to avoid duplicate requests
    const fetchingUris = new Set();

    // How many times to look for lyrics before giving up, and how long to wait
    // between tries. Spotify's auth token often isn't ready in the first few
    // seconds after startup, and the Spicy API needs a valid token too.
    const LYRIC_ATTEMPTS  = 2;
    const LYRIC_RETRY_MS  = 3000;

    // One pass over both lyric sources. Returns { lyrics, source } or null.
    async function fetchLyricsOnce(trackUri) {
        // Try Spicy first; fall back to Spotify's own color-lyrics API if Spicy
        // returns nothing (song not in their catalogue, network error, etc.).
        let lyrics = await fetchSpicyLyrics(trackUri);
        if (lyrics) return { lyrics, source: "Spicy" };

        // Never downgrade silently. Falling back to Spotify used to be
        // invisible, so a permanently broken Spicy path looked like normal
        // operation for months.
        send({ type: "lyrics_debug",
               message: `Spicy unavailable (${lastSpicyError || "unknown"}) — falling back to Spotify` });
        lyrics = await fetchSpotifyLyrics(trackUri);
        return lyrics ? { lyrics, source: "Spotify (Spicy fallback)" } : null;
    }

    async function sendTrackAndLyrics(item) {
        const trackUri = item.uri || "";
        if (!trackUri) return;

        // Guard against duplicate concurrent fetches for the same track.
        if (fetchingUris.has(trackUri)) {
            console.log("[LyricsBridge] Already fetching:", trackUri);
            return;
        }
        fetchingUris.add(trackUri);

        try {
            const artist   = item.metadata?.["artist_name"] || item.artists?.map(a=>a.name).join(", ") || "";
            const title    = item.metadata?.["title"] || item.name || "";
            const albumArt = getAlbumArt(item);
            const durMs    = parseInt(item.metadata?.["duration"] || item.duration_ms || 0);

            // track_change is sent EXACTLY ONCE per call. The retry used to be
            // a recursive re-entry into this whole function, so a track whose
            // lyrics needed a second attempt emitted a second track_change —
            // and Statusify treats that as a brand new song: it counted twice
            // in the session stats and reset lyrics_mode/synced/plain back to
            // empty. The retry now loops over the lyric fetch alone.
            console.log("[LyricsBridge] Sending track_change:", title, "—", artist);
            send({ type: "track_change", artist, title, track_uri: trackUri,
                   album_art: albumArt, duration_ms: durMs });

            let found = null;
            for (let attempt = 0; attempt < LYRIC_ATTEMPTS; attempt++) {
                if (attempt > 0) {
                    console.log(`[LyricsBridge] No lyrics yet, retrying in ${LYRIC_RETRY_MS}ms...`);
                    send({ type: "lyrics_debug",
                           message: `No lyrics for "${title}" — retrying in ${LYRIC_RETRY_MS / 1000}s` });
                    await new Promise(r => setTimeout(r, LYRIC_RETRY_MS));
                    // The user may have skipped on while we waited.
                    if (Spicetify.Player.data?.item?.uri !== trackUri) {
                        console.log("[LyricsBridge] Track changed during retry — abandoning");
                        return;
                    }
                }
                found = await fetchLyricsOnce(trackUri);
                if (found) break;
            }

            if (!found) {
                send({ type: "lyrics_debug",
                       message: `No lyrics found for "${title}" after ${LYRIC_ATTEMPTS} attempts` });
            }

            // Store lyrics in memory for the skip_instrumental feature.
            currentLyrics = found ? found.lyrics : null;
            if (found) {
                console.log(`[LyricsBridge] Stored ${found.lyrics.synced?.length || 0} synced lyrics in memory`);
            }

            send({ type: "lyrics", track_uri: trackUri,
                   source: found ? found.source : "none",
                   ...(found ? found.lyrics : { mode: "none", synced: [], plain: [] }) });
        } finally {
            // finally, so a throw anywhere above can't leave the URI wedged in
            // the set — which would block every future fetch for that track.
            fetchingUris.delete(trackUri);
        }
    }

    async function tick() {
        const data = Spicetify.Player.data;
        if (!data?.item) return;

        const item     = data.item;
        const trackUri = item.uri || "";
        const posMs    = Spicetify.Player.getProgress();
        const durMs    = parseInt(item.metadata?.["duration"] || item.duration_ms || 0);
        const playing  = Spicetify.Player.isPlaying();

        if (!playing) { send({ type: "paused" }); return; }

        // New track (or just connected) — send full track+lyrics
        if (trackUri !== lastTrackUri) {
            lastTrackUri = trackUri;
            sendTrackAndLyrics(item);  // async, don't await so tick stays fast
        }

        send({ type: "position", position_ms: posMs, duration_ms: durMs, is_playing: playing });
    }

    setInterval(tick, 500);
    Spicetify.Player.addEventListener("songchange", () => { lastTrackUri = ""; currentLyrics = null; });
    connect();
})();