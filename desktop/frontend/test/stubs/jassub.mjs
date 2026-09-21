// libass 的 WASM 渲染器。测试跑的是排版复算与样式表读写这一侧，画面不参与，
// 而 lib/subtitles.ts 只在 preloadSubtitles() 里才 new 它——单元测试永远走不到那儿。
export default class JASSUB {
  constructor() { throw new Error("JASSUB is not available under node --test"); }
}
