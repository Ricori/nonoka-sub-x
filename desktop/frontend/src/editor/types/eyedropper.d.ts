// TS 5.9 的 lib.dom 还没有 EyeDropper（Chromium 95+，WebView2 Evergreen 支持）。
// 它取的是 OS 级的屏幕像素，所以不受同源策略约束——这也是预览画面唯一可行的取色方式：
// <video> 来自跨域的环回服务（画布会被污染），字幕画布则被 JASSUB
// transferControlToOffscreen 掉了，两层都读不出像素。

interface EyeDropperOpenOptions {
  signal?: AbortSignal;
}

interface EyeDropperResult {
  /** "#rrggbb" */
  sRGBHex: string;
}

declare class EyeDropper {
  constructor();
  /** 用户按 Esc 取消时 reject（AbortError）。需要用户手势才能调用 */
  open(options?: EyeDropperOpenOptions): Promise<EyeDropperResult>;
}

interface Window {
  EyeDropper?: typeof EyeDropper;
}
