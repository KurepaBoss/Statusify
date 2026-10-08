// Shared frontend API. Every page/window talks to the backend through this.
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export type Line = { startMs: number; words: string };
export type Lyrics = { mode: "synced" | "plain" | "none" | string; synced: Line[]; plain: string[]; source: string };
export type Track = { uri: string; artist: string; title: string; album: string; album_art: string; blacklisted: boolean };
export type Snapshot = {
  track: Track | null;
  position_ms: number;
  /** epoch ms when position_ms was reported; extrapolate with position() */
  position_at_ms: number;
  duration_ms: number;
  is_playing: boolean;
  lyrics: Lyrics;
  bridge_connected: boolean;
  discord_user: string | null;
  note: string;
  /** feature data pushed with Engine::set_extra */
  extras: Record<string, any>;
};

/** Call a backend feature module: features/<feature>.rs, Feature::call. */
export function call<T = any>(feature: string, action: string, args: Record<string, any> = {}): Promise<T> {
  return invoke<T>("call", { feature, action, args });
}

export const player = (action: string) => invoke<boolean>("player", { action });
export const seek = (positionMs: number) => invoke<boolean>("seek", { positionMs });

let latest: Snapshot | null = null;
const subs = new Set<(s: Snapshot) => void>();

/** Subscribe to state changes; called immediately with the latest snapshot. */
export function onSnapshot(cb: (s: Snapshot) => void): () => void {
  subs.add(cb);
  if (latest) cb(latest);
  return () => subs.delete(cb);
}

export function snapshot(): Snapshot | null {
  return latest;
}

/** Song position now, extrapolated between bridge updates. */
export function position(s: Snapshot): number {
  const p = s.position_ms + (s.is_playing ? Date.now() - s.position_at_ms : 0);
  return s.duration_ms > 0 ? Math.min(p, s.duration_ms) : p;
}

function publish(s: Snapshot) {
  latest = s;
  subs.forEach((cb) => cb(s));
}

/** The clock part of a snapshot: what a bridge position message changes. */
export type PositionUpdate = Pick<Snapshot, "position_ms" | "position_at_ms" | "duration_ms" | "is_playing">;

listen<Snapshot>("snapshot", (e) => publish(e.payload));
// Twice a second while playing the backend sends only the clock (four
// fields), not the whole snapshot; it is folded into the latest one.
listen<PositionUpdate>("position", (e) => {
  if (latest) publish({ ...latest, ...e.payload });
});
invoke<Snapshot>("snapshot").then(publish);
