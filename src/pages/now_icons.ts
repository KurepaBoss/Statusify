// Glyphs of the Lyrics page, as inline SVG (currentColor), drawn on a 24 x 24
// grid from the same unit geometry as the Python app's PIL glyphs.

const S = (body: string, extra = "") =>
  `<svg viewBox="0 0 24 24" width="1em" height="1em" aria-hidden="true" ${extra}>${body}</svg>`;
const stroke = (d: string, w = 2.04) =>
  `<path d="${d}" fill="none" stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round"/>`;
const fill = (d: string) => `<path d="${d}" fill="currentColor"/>`;

export const ICONS: Record<string, string> = {
  play: S(fill("M6.7 4.3 6.7 19.7 20.2 12z")),
  pause: S(`<rect x="5.8" y="4.3" width="4.3" height="15.4" rx="1" fill="currentColor"/><rect x="13.9" y="4.3" width="4.3" height="15.4" rx="1" fill="currentColor"/>`),
  next: S(fill("M4.8 4.8 4.8 19.2 16.3 12z") + `<rect x="16.3" y="4.8" width="2.9" height="14.4" rx=".7" fill="currentColor"/>`),
  prev: S(fill("M19.2 4.8 19.2 19.2 7.7 12z") + `<rect x="4.8" y="4.8" width="2.9" height="14.4" rx=".7" fill="currentColor"/>`),
  shuffle: S(
    stroke("M2.4 7.2H7.2L14.9 16.8H18.7") + stroke("M2.4 16.8H7.2L14.9 7.2H18.7") +
      fill("M17.8 4.1 22.1 7.2 17.8 10.3z") + fill("M17.8 13.7 22.1 16.8 17.8 19.9z"),
  ),
  repeat: S(
    stroke("M14.4 6.24H16.32A4.8 4.8 0 0 1 21.12 11.04V12.96A4.8 4.8 0 0 1 16.32 17.76H13.44") +
      stroke("M9.6 17.76H7.68A4.8 4.8 0 0 1 2.88 12.96V11.04A4.8 4.8 0 0 1 7.68 6.24H10.56") +
      fill("M12 3.12 15.36 6.24 12 9.36z") + fill("M12 14.64 8.64 17.76 12 20.88z"),
  ),
  heart: S(fill("M12 21.2s-8.2-5-9.9-9.9C.8 7.4 3.1 4.2 6.6 4.2c2 0 3.7 1 5.4 3.1 1.7-2.1 3.4-3.1 5.4-3.1 3.5 0 5.8 3.2 4.5 7.1-1.7 4.9-9.9 9.9-9.9 9.9z")),
  heart_o: S(stroke("M12 20s-7.4-4.6-8.9-9c-1.1-3.3.9-6.2 3.8-6.2 1.9 0 3.4 1 5.1 3.1 1.7-2.1 3.2-3.1 5.1-3.1 2.9 0 4.9 2.9 3.8 6.2-1.5 4.4-8.9 9-8.9 9z", 1.9)),
  vol0: S(fill("M2.4 9.1H6.2L11 4.3V19.7L6.2 14.9H2.4z") + stroke("M14.9 9.1 20.6 14.9M14.9 14.9 20.6 9.1")),
  vol1: S(fill("M2.4 9.1H6.2L11 4.3V19.7L6.2 14.9H2.4z") + stroke("M13.55 15.2A4.08 4.08 0 0 0 13.55 8.8")),
  vol2: S(fill("M2.4 9.1H6.2L11 4.3V19.7L6.2 14.9H2.4z") + stroke("M13.55 15.2A4.08 4.08 0 0 0 13.55 8.8") + stroke("M15.9 18.2A7.9 7.9 0 0 0 15.9 5.8")),
  queue: S(stroke("M3.4 6.2H20.6M3.4 12H20.6M3.4 17.8H13.4") + fill("M16.3 14.9 16.3 20.6 21.6 17.8z")),
  more: S(`<circle cx="5.3" cy="12" r="2.04" fill="currentColor"/><circle cx="12" cy="12" r="2.04" fill="currentColor"/><circle cx="18.7" cy="12" r="2.04" fill="currentColor"/>`),
  moon: S(fill("M20.4 14.6A8.6 8.6 0 1 1 9.4 3.6 6.9 6.9 0 0 0 20.4 14.6z")),
  close: S(stroke("M5.8 5.8 18.2 18.2M5.8 18.2 18.2 5.8")),
  search: S(`<circle cx="9.5" cy="9.5" r="6.4" fill="none" stroke="currentColor" stroke-width="2.04"/>` + stroke("M14.4 14.4 20.6 20.6")),
  full: S(stroke("M2.9 10.1V2.9H10.1M13.9 2.9H21.1V10.1M21.1 13.9V21.1H13.9M10.1 21.1H2.9V13.9", 2.6)),
  unfull: S(stroke("M8.6 2.9V8.6H2.9M15.4 2.9V8.6H21.1M21.1 15.4H15.4V21.1M2.9 15.4H8.6V21.1", 2.6)),
  minus: S(stroke("M6 12H18", 2.4)),
  plus: S(stroke("M6 12H18M12 6V18", 2.4)),
  note: S(fill("M9 18.5a3 3 0 1 1-1.5-2.6V5.5L19 3.2v11.3a3 3 0 1 1-1.5-2.6V7.3L9 8.9z")),
};

export function icon(name: string): string {
  return ICONS[name] ?? "";
}
