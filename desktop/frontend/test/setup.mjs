import { registerHooks } from "node:module";
import { existsSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, resolve as resolvePath } from "node:path";

// src/subtitles 一律写全 `.ts` 后缀，所以那部分 Node 直接就能跑。编辑器那半边用的是
// 打包器风格的无后缀相对导入（`from '../store/docStore'`），Vite 认，Node ESM 不认。
//
// 与其给几百处导入补后缀，不如在测试里补一个解析钩子：无后缀的相对路径依次试
// .ts / .tsx / .js / index.ts。这样 store、lib 这些真正有逻辑的模块也能进单元测试，
// 而生产构建一个字都不用改。

const CANDIDATES = [".ts", ".tsx", ".js", ".mjs", "/index.ts", "/index.tsx"];
const STUB_ASSET = new URL("./stubs/asset-url.mjs", import.meta.url).href;
const STUB_JASSUB = new URL("./stubs/jassub.mjs", import.meta.url).href;
const STUB_WAILS = new URL("./stubs/wails-runtime.mjs", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    // Vite 专属的导入形式：?url / ?worker&url 拿到的是打包后的资源地址，
    // 而 jassub 是 WASM 渲染器。两者都换成占位模块——测的是排版与样式表，画面不参与。
    if (specifier.includes("?")) return { url: STUB_ASSET, shortCircuit: true };
    if (specifier === "jassub") return { url: STUB_JASSUB, shortCircuit: true };
    if (specifier === "@wailsio/runtime") return { url: STUB_WAILS, shortCircuit: true };
    if (specifier.startsWith("./") || specifier.startsWith("../")) {
      const parent = context.parentURL && context.parentURL.startsWith("file:")
        ? dirname(fileURLToPath(context.parentURL))
        : null;
      // 生成的 Wails bindings 按 TS 的习惯写成 `./index.js`，磁盘上却是 .ts
      const stem = specifier.endsWith(".js") ? specifier.slice(0, -3) : specifier;
      if (parent && (stem !== specifier || !/\.[a-z]+$/i.test(specifier))) {
        for (const suffix of CANDIDATES) {
          const candidate = resolvePath(parent, stem + suffix);
          if (existsSync(candidate)) {
            return { url: pathToFileURL(candidate).href, shortCircuit: true };
          }
        }
      }
    }
    return nextResolve(specifier, context);
  },
});

// 编辑器的本机偏好（layoutStore）在模块初始化时就读 localStorage 和 window.innerHeight。
// 给一份最小的浏览器全局，让 store 和 lib 能在 node --test 里被导入。
//
// document 只给到「能取到 baseURI、拿不到 2D 上下文」这个程度：src/subtitles/metrics.ts
// 拿不到 canvas 上下文就一律走兜底字宽比例，于是排版结果是确定的，断言才写得死。
if (typeof globalThis.localStorage === "undefined") {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => { store.set(key, String(value)); },
    removeItem: (key) => { store.delete(key); },
    clear: () => store.clear(),
  };
}

if (typeof globalThis.document === "undefined") {
  // 够用就行，不是 DOM 实现：只覆盖模块初始化时真会碰的那几处
  // （constants.ts 量滚动条高度、metrics.ts 量字宽、lib/subtitles.ts 拼字体 URL）。
  const element = () => ({
    style: {},
    offsetHeight: 0,
    clientHeight: 0,
    getContext: () => null,   // 量不到字宽 → metrics 用兜底比例，排版结果确定
    appendChild() {},
    remove() {},
  });
  globalThis.document = {
    baseURI: "file:///stub/",
    body: { ...element(), classList: { add() {}, remove() {} } },
    createElement: element,
    addEventListener() {},
    removeEventListener() {},
  };
}

if (typeof globalThis.window === "undefined") {
  globalThis.window = {
    innerWidth: 1600,
    innerHeight: 900,
    localStorage: globalThis.localStorage,
    addEventListener() {},
    removeEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  };
}
