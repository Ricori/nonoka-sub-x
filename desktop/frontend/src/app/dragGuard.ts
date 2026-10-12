/** Cancels every drag that starts inside the window. Nothing in the UI uses
 *  HTML5 drag and drop, and a stray one (selected text, a link, an image) would
 *  otherwise land on the window's own file drop target. Explorer drags start
 *  outside the page and are unaffected. */
export function installDragGuard() {
  if (typeof window === "undefined") return;
  window.addEventListener("dragstart", (e) => e.preventDefault(), true);
}
