请根据 system 指令完成全局准备任务：$task_summary。

用户提供的额外信息（可能为空）：
<extra_info>
$extra_info
</extra_info>

用户备注中的 URL 经本地深度提取后的页面内容（去重后最多 8 个 URL；可能为空）：
<note_url_extracts>
$note_url_extracts
</note_url_extracts>
$knowledge_inputs
带窗口标记的 ASR 字幕文本：
<transcript>
$transcript
</transcript>

最后提醒（读完以上全部输入后）：先以 `<reasoning>` 块开头；$closing_reminder；不能因输出较长省略必需标签，必须严格按要求格式完整输出；除上述块外不要输出任何其他文字。
