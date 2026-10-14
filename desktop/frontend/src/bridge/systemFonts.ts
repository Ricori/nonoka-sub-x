import { Service as SystemFontsService } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/sysfonts/index.js";

let cached: Promise<string[]> | null = null;

/** 本机已安装的字体族名（Go 端扫字体目录读 name 表，中文名优先）。每个窗口只扫一次 */
export function systemFonts(): Promise<string[]> {
  if (!cached) {
    cached = (SystemFontsService.List() as Promise<string[] | null>)
      .then(list => list ?? [])
      .catch(() => { cached = null; return []; });
  }
  return cached;
}

/** 与 Go 端 sysfonts.RoutePath 一致：资源服务器上按族名取字体文件的路由 */
const FONT_ROUTE = "/__sysfonts";

/**
 * 按族名取系统字体文件（中英文名、全名都认，不分大小写）。没装就是 null。
 * 预览用的 libass 跑在 WASM 里看不到系统字体，得这样把文件喂给它。
 */
export async function systemFontFile(name: string): Promise<Uint8Array<ArrayBuffer> | null> {
  const url = new URL(FONT_ROUTE, document.baseURI);
  url.searchParams.set("family", name);
  const response = await fetch(url).catch(() => null);
  if (!response?.ok) return null;
  return new Uint8Array(await response.arrayBuffer());
}
