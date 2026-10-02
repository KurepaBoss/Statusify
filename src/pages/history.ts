// History page (owner: history agent). Port of the Python app's History page.
// eslint-disable-next-line @typescript-eslint/no-unused-vars
import { call, onSnapshot } from "../api";
void call; void onSnapshot;

export function mount(root: HTMLElement) {
  root.innerHTML = '<div class="placeholder">History — not ported yet</div>';
}

export function show() {}
export function hide() {}
