// Settings page (owner: shell agent). Port of the Python app's Settings page.
// eslint-disable-next-line @typescript-eslint/no-unused-vars
import { call, onSnapshot } from "../api";
void call; void onSnapshot;

export function mount(root: HTMLElement) {
  root.innerHTML = '<div class="placeholder">Settings — not ported yet</div>';
}

export function show() {}
export function hide() {}
