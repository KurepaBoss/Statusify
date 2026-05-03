(async function lyricsBridge() {
    while (!Spicetify?.Player || !Spicetify?.CosmosAsync) {
        await new Promise(r => setTimeout(r, 300));
    }
    await new Promise(r => setTimeout(r, 1000));
    console.log("[LyricsBridge] Loaded.");

    let ws             = null;
    let reconnectTimer = null;
    let lastTrackUri   = "";   // tracks what we last sent a track_change for

    function connect() {
        if (ws?.readyState === WebSocket.OPEN) return;
        const wsUrl = "ws://127.0.0.1:8765";
        ws = new WebSocket(wsUrl);
        console.log("[LyricsBridge] Connecting:", wsUrl);

        ws.onopen = async () => {
            console.log("[LyricsBridge] Connected.");
            clearTimeout(reconnectTimer);
            fetchingUris.clear();
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
                    fetchingUris.clear();
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

    function parseSpicyLyrics(data) {
        const result = data?.queries?.[0]?.result?.data;
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
        if (!token) { console.warn("[LyricsBridge] No Spotify token."); return null; }
        try {
            const resp = await fetch("https://api.spicylyrics.org/query", {
                method:  "POST",
                headers: {
                    "Content-Type":        "application/json",
                    "SpicyLyrics-Version": "5.19.12",
                    "SpicyLyrics-WebAuth": `Bearer ${token}`,
                },
                body: JSON.stringify({
                    queries: [{ operation: "lyrics", variables: { id: trackId, auth: "SpicyLyrics-WebAuth" }}],
                    client:  { version: "5.19.12" }
                })
            });
            const data   = await resp.json();
            const result = parseSpicyLyrics(data);
            if (!result) {
                const raw = data?.queries?.[0];
                console.warn("[LyricsBridge] Spicy returned no content:", trackId,
                    "status:", raw?.result?.status, "type:", raw?.result?.data?.Type);
            }
            return result;
        } catch(e) {
            console.warn("[LyricsBridge] Spicy fetch failed:", e.message);
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
            if (!lines?.length) return null;
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
        } catch(e) { return null; }
    }

    // Track which URIs are currently being fetched to avoid duplicate requests
    const fetchingUris = new Set();

    async function sendTrackAndLyrics(item) {
        const trackUri = item.uri || "";
        if (!trackUri) return;

        const artist   = item.metadata?.["artist_name"] || item.artists?.map(a=>a.name).join(", ") || "";
        const title    = item.metadata?.["title"] || item.name || "";
        const albumArt = getAlbumArt(item);
        const durMs    = parseInt(item.metadata?.["duration"] || item.duration_ms || 0);

        // Guard against duplicate concurrent fetches for the same track.
        // Also skip the track_change if we already sent one for this URI.
        if (fetchingUris.has(trackUri)) {
            console.log("[LyricsBridge] Already fetching:", trackUri);
            return;
        }
        fetchingUris.add(trackUri);

        console.log("[LyricsBridge] Sending track_change:", title, "—", artist);
        send({ type: "track_change", artist, title, track_uri: trackUri,
               album_art: albumArt, duration_ms: durMs });

        // Try Spicy first; fall back to Spotify's own color-lyrics API if Spicy
        // returns nothing (song not in their catalogue, network error, etc.).
        let lyrics = await fetchSpicyLyrics(trackUri);
        let source = "Spicy";
        if (!lyrics) {
            lyrics = await fetchSpotifyLyrics(trackUri);
            source = "Spotify";
        }
        send({ type: "lyrics", track_uri: trackUri, source,
               ...(lyrics || { mode: "none", synced: [], plain: [] }) });

        fetchingUris.delete(trackUri);
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
    Spicetify.Player.addEventListener("songchange", () => { lastTrackUri = ""; });
    connect();
})();
