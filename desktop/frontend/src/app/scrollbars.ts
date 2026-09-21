import "./scrollbars.css";

const CLASS = "sb-hover";
let marked: Element[] = [];

function mark(target: EventTarget | null) {
  const next: Element[] = [];
  for (let el = target instanceof Element ? target : null; el; el = el.parentElement) next.push(el);
  for (const el of marked) if (!next.includes(el)) el.classList.remove(CLASS);
  for (const el of next) el.classList.add(CLASS);
  marked = next;
}

/** Tags the hovered element and its ancestors with .sb-hover so their scrollbars
 *  fade in; a class change forces the repaint that :hover does not. */
export function installScrollbarHover() {
  if (typeof document === "undefined") return;
  document.addEventListener("pointerover", (e) => mark(e.target), { passive: true });
  document.documentElement.addEventListener("pointerleave", () => mark(null));
  window.addEventListener("blur", () => mark(null));
}
