"""阶段 4 的扩展效果（`flowocr.extensions` 的 `fx` 种类）放这里的是内置的；现在没有，底板、编号、逐字显出在 `core` 里。

一个效果模块：

* `NAME`：它在字幕稿 Effect 字段 `fo:…` 里的参数名，源行带了这个参数才调它；
* `elements(line, value) -> list[core.Element]`（可选）：第 2 步，读排好版的 `core.Line`（只读），加元素；
* `tags(line, value, element) -> None`（可选）：第 4 步，改每个元素的 tag（`element.tail` / `element.tail2` / `element.body`）；
  在动的行按轨迹拆成几段时，每一段的元素定稿之后各调一次。

两个挂点至少有一个。元素的位置按条目框左上角排，第 3 步和底板、正文一起按轨迹走。例子见 `examples/fx_minimal.py`。
元素的 `kind` 里 `"plate"`（底板）和 `"text"`（正文）是内置的：底板取色（`plate_color`）只给前者取画面色、只改后者的字色，
扩展自己的元素用别的名字（例子里是 `"frame"`），取色不碰它。
"""
