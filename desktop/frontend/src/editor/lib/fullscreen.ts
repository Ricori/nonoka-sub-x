/** 预览区（画面 + 播放条）全屏。WebView2 的元素全屏由 Wails 接住，顺带把窗口也切成全屏；
 *  退出走 Esc 或再点一次按钮，两条路都会触发 fullscreenchange */
export function previewPane(): HTMLElement | null {
  return document.getElementById("preview-pane");
}

export function isPreviewFullscreen(): boolean {
  const pane = previewPane();
  return !!pane && document.fullscreenElement === pane;
}

export function togglePreviewFullscreen(): void {
  if (document.fullscreenElement) {
    void document.exitFullscreen().catch(() => {});
    return;
  }
  const pane = previewPane();
  if (pane) void pane.requestFullscreen().catch(() => {});
}
