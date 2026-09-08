// 取出打包时内联进页面的 assets/*.md。
//
// 页面在运行时读不到包里任何别的文件 —— srcdoc 没有 origin 也没有 base URL，
// CSP 又是 connect-src 'none'。所以 build.mjs 会把每份 md 塞进一个
// <script type="text/plain">，这里从 DOM 里取回来。

/**
 * 取一份 prompt。没内联就报错 —— 静默用空 prompt 去调模型是最难查的那种错：
 * 模型会认真地返回一堆莫名其妙的东西，而你完全看不出 prompt 丢了。
 */
function promptAsset(id: string, file: string): string {
  const node = document.getElementById(id);
  const text = (node?.textContent ?? "").trim();
  // 没内联时这里还是原样的标记
  if (!text || text.startsWith("<!" + "--INLINE:")) {
    throw new Error(`${file} 没有被内联进页面，跑一次 node build.mjs`);
  }
  // build.mjs 会把正文里的 `</script` 拆成 `<\/script`（不然 HTML 解析器会就地
  // 截断那个元素），这里还原回去
  return text.split("<\\/script").join("<" + "/script");
}
