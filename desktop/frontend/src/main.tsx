import { createRoot } from "react-dom/client";
// styles.css is the base sheet and must be emitted before the per-page sheets that
// theme on top of it: bundlers order CSS by module-graph position, so importing it
// after App.tsx let its legacy hard-coded dark colours win over the themed rules in
// LibraryPage.css and friends, which is what broke light mode.
import "./styles.css";
import App from "./app/App.tsx";
import { installWindowsTitlebar } from "./bridge/window.ts";
import { installScrollbarHover } from "./app/scrollbars.ts";

const root = document.getElementById("root");
if (!root) throw new Error("Nonoka X root element is missing");

createRoot(root).render(<App />);

installScrollbarHover();
void installWindowsTitlebar();
