// 阶段三：把模型返回的 JSON 解析成表，并拼出能直接粘进 Excel 的 TSV。
//
// 字段和形状由 assets/to_excel.md 规定：
//   { "sheet_date": "YYYYMMDD", "rows": [ {start,end,category,detail,highlight,editor} ] }
// 单元格内换行一律用 <br>，rows 按 start 升序。

const FIELDS = ["start", "end", "category", "detail", "highlight", "editor"] as const;
const HEADERS = ["开始", "结束", "分类", "详情", "高能切片", "剪辑"];

type Field = (typeof FIELDS)[number];
type Row = Record<Field, string>;

interface Sheet {
  date: string;
  rows: Row[];
}

/**
 * 从模型输出里挖出 JSON。
 *
 * 就算 prompt 写死「只输出 JSON」，模型也经常裹一层 ```json 围栏或者前后加句
 * 客套话，直接 JSON.parse 会炸。取第一个 { 到最后一个 } 之间的内容，比要求
 * 用户手动清理可靠得多。
 */
function extractJSON(raw: string): unknown {
  const text = raw.trim();
  const open = text.indexOf("{");
  const close = text.lastIndexOf("}");
  if (open < 0 || close <= open) {
    // 把实际回了什么带进错误里。只说「没有 JSON」的话，你无从判断是模型
    // 输出了 Markdown 表格、拒答、还是压根返回了空 —— 这三种的对策完全不同。
    logLine(`汇总的原始输出（前 400 字）：\n${text.slice(0, 400) || "（空）"}`);
    throw new Error(`模型没有返回 JSON 对象，它回的是：${preview(text)}`);
  }
  return JSON.parse(text.slice(open, close + 1));
}

/** 错误信息里塞一小段原文，够判断是哪类失败就行。 */
function preview(text: string): string {
  if (!text) return "空内容";
  const head = text.replace(/\s+/g, " ").trim().slice(0, 80);
  return `「${head}${text.length > 80 ? "…" : ""}」（共 ${text.length} 字）`;
}

/**
 * 校验并整形。缺字段补空串而不是整份失败 —— 模型偶尔漏一个 editor，
 * 不该让整场直播的结果作废。
 */
function parseSummary(raw: string): Sheet {
  const data = extractJSON(raw) as { sheet_date?: unknown; rows?: unknown };
  if (!Array.isArray(data.rows)) throw new Error("JSON 里没有 rows 数组");
  if (data.rows.length === 0) throw new Error("rows 是空的");

  const rows = data.rows.map((item, index) => {
    if (item === null || typeof item !== "object") throw new Error(`第 ${index + 1} 行不是对象`);
    const source = item as Record<string, unknown>;
    const row = {} as Row;
    for (const key of FIELDS) row[key] = source[key] == null ? "" : String(source[key]);
    return row;
  });

  return { date: String(data.sheet_date ?? "").trim(), rows };
}

/**
 * 渲染到某个 tbody。
 *
 * @param compact 页面内那张是压缩预览：行高封顶、长内容截断，一屏能看到更多行。
 *                浮层里那张不截断，用来通读。两张表共用这一个函数，免得列的
 *                顺序或换行处理在两处走样。
 */
function renderInto(id: string, rows: Row[], compact: boolean): void {
  el(id).replaceChildren(...rows.map((row) => {
    const tr = document.createElement("tr");
    for (const key of FIELDS) {
      const td = document.createElement("td");
      if (key === "start" || key === "end") {
        td.className = "num";
        td.textContent = row[key];
        tr.appendChild(td);
        continue;
      }
      td.className = "wrap";
      // 表里按 <br> 断行显示，跟贴进 Excel 之后看到的样子一致
      const text = row[key].replace(/<br\s*\/?>/gi, "\n");
      if (compact) {
        // 截断必须套一层 div：-webkit-line-clamp 要配 display:-webkit-box，
        // 直接加在 td 上会把它的 display:table-cell 顶掉 —— 那一格就不再是
        // 单元格，内容会脱离列的网格铺成整行宽的块。
        const box = document.createElement("div");
        box.className = "clamp";
        box.textContent = text;
        td.appendChild(box);
      } else {
        td.textContent = text;
      }
      tr.appendChild(td);
    }
    return tr;
  }));
}

/**
 * 一个单元格在 TSV 里长什么样。
 *
 * Excel 按制表符分列、按换行分行，所以单元格里的真实换行必须整格加引号
 * （内部引号翻倍），否则一行会被拆成好几行。不想要引号就把 <br> 原样留着，
 * 粘进去之后在 Excel 里查找替换。
 */
function excelCell(value: string, realNewline: boolean): string {
  const flat = value.replace(/\t/g, " ");
  if (!realNewline) return flat.replace(/[\r\n]+/g, " ").trim();

  const text = flat.replace(/<br\s*\/?>/gi, "\n").replace(/\r/g, "");
  if (text.includes("\n") || text.includes('"')) return `"${text.split('"').join('""')}"`;
  return text.trim();
}

/**
 * `<br>` 一律转成单元格内真实换行（整格加引号）。
 *
 * 原来这是个勾选框，但它问的是一个用户答不上来的问题 —— 要判断该勾不该勾，
 * 你得先知道 Excel 的 TSV 解析规则。加引号的那种才是 Excel 粘贴多行单元格的
 * 正确形式，所以固定成它，不再问。
 */
const CELL_REAL_NEWLINE = true;

function tableTSV(rows: Row[]): string {
  const body = rows.map((row) => FIELDS.map((key) => excelCell(row[key], CELL_REAL_NEWLINE)).join("\t"));
  return [HEADERS.join("\t"), ...body].join("\n");
}

/** 当前这张表。复制和预览都从这里取。 */
let sheet: Sheet = { date: "", rows: [] };

function showSummary(raw: string): void {
  sheet = parseSummary(raw);
  renderInto("table-rows", sheet.rows, true);
  el("result").hidden = false;

  const noTime = sheet.rows.filter((row) => !row.start || !row.end).length;
  const dateNote = sheet.date ? `　直播日期 ${sheet.date}` : "";
  el("result-head").textContent = `${sheet.rows.length} 行${dateNote}`;
  logLine(`解析出 ${sheet.rows.length} 行${noTime > 0 ? `，其中 ${noTime} 行缺时间` : ""}`);
}

/** 开新一轮时收起上一轮的表，免得旧结果和新日志同时挂在界面上。 */
function hideSummary(): void {
  sheet = { date: "", rows: [] };
  el("result").hidden = true;
  closePreview();
}

function copyTable(): void {
  if (sheet.rows.length === 0) {
    say("还没有表格。", true);
    return;
  }
  void copyText(tableTSV(sheet.rows), `已复制 ${sheet.rows.length} 行，去 Excel 里 Ctrl+V。`);
}

/* ---------- 全表预览浮层 ---------- */

/** 行是开浮层时才建的：不这么做就要维护两份 DOM，页面内那张改了这张容易忘。 */
function openPreview(): void {
  if (sheet.rows.length === 0) {
    say("还没有表格。", true);
    return;
  }
  renderInto("overlay-rows", sheet.rows, false);
  el("overlay-title").textContent = `${sheet.rows.length} 行`
    + (sheet.date ? `　直播日期 ${sheet.date}` : "");
  el("overlay").hidden = false;
  el("overlay-wrap").scrollTop = 0;
}

function closePreview(): void {
  el("overlay").hidden = true;
  // 关掉就把行丢掉。几百行的表留着不看，白占内存也白占一份可能过期的副本。
  el("overlay-rows").replaceChildren();
}
