import { createRoot } from "react-dom/client";
// styles.css is the base sheet and must be emitted before the per-page sheets that
// theme on top of it: bundlers order CSS by module-graph position, so importing it
// after App.tsx let its legacy hard-coded dark colours win over the themed rules in
// LibraryPage.css and friends, which is what broke light mode.
import "./styles.css";
// 设计系统（字号/间距/圆角/语义色 + 按钮、标签）紧跟 base sheet，压过其中的旧规则。
import "./design-system.css";
import App from "./app/App.tsx";
import OnboardingWindowApp from "./app/OnboardingWindowApp.tsx";
import { installWindowsTitlebar } from "./bridge/window.ts";
import { installScrollbarHover } from "./app/scrollbars.ts";
import { installDragGuard } from "./app/dragGuard.ts";

const root = document.getElementById("root");
if (!root) throw new Error("Nonoka X root element is missing");

const isOnboardingWindow = new URLSearchParams(window.location.search).get("window") === "onboarding";
if (isOnboardingWindow) {
  document.body.classList.add("onboarding-window");
}

createRoot(root).render(isOnboardingWindow ? <OnboardingWindowApp /> : <App />);

installScrollbarHover();
installDragGuard();
void installWindowsTitlebar();
