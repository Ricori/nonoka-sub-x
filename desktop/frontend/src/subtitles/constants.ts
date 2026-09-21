// ASS 骨架常量：样式表解析、编辑器预览、导出和插件宿主共用一份。
// 与旧工程 vod/core/subtitles.py 的常量保持一致，改这里等于改所有出片路径。

// 与 vod/core/subtitles.py 的 _ASS_SCRIPT_INFO / _ASS_EVENTS_HEAD 保持一致
export const ASS_SCRIPT_INFO = "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\n"
  + "PlayResY: 1080\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n";
export const ASS_EVENTS_HEAD = "\n[Events]\nFormat: Layer, Start, End, Style, Name,"
  + " MarginL, MarginR, MarginV, Effect, Text\n";

/** [V4+ Styles] 的标准 Format 行（23 字段）。本地样式表一律按它规范化输出 */
export const ASS_STYLE_FORMAT = "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
  + " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
  + " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding";

/**
 * 写死的两个默认样式。默认轨的原文/译文绑的就是它们，样式编辑器里不展示，也删不掉：
 * 文档绑到自己样式表里没有的样式时，回退的正是这两个（见 ass.ts::resolveStyle）。
 * 文档的样式表里出现同名样式时以文档那份为准——「合并」而不是「锁死」。
 */
export const BUILTIN_ASS_STYLES = `Style: JP,方正准圆_GBK,70,&H00FFF9FD,&HF0000000,&H00EF9320,&H30633306,0,0,0,0,100,100,7,0,1,2,2,8,10,10,30,1
Style: CN,方正准圆_GBK,70,&H00FFF9FD,&HF0000000,&H00EF9320,&H30633306,0,0,0,0,100,100,7,0,1,2,2,2,10,10,30,1`;

/** 新文档的种子：旧工程内置模板里除 JP/CN 之外的那几个样式 */
export const DEFAULT_USER_ASS_STYLES = `Style: 注释,思源黑体 Heavy,60,&H00FFFFFF,&H000000FF,&H00000000,&H00737375,0,0,0,0,100,100,0,0,1,2,2,7,30,30,30,1
Style: 优花,荆南波波黑,90,&H00D59B57,&H000000FF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,5,0,2,10,10,30,1
Style: haru,荆南波波黑,90,&H002E0C9B,&H000000FF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,5,0,2,10,10,30,1
Style: nana,荆南波波黑,90,&H00B9A7F0,&H000000FF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,5,0,2,10,10,30,1
Style: saya,荆南波波黑,90,&H0091DF82,&H000000FF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,5,0,2,10,10,30,1`;

/** 没有本机默认模板时的兜底种子（不含写死的 JP/CN） */
export const DEFAULT_STYLE_SHEET = `[V4+ Styles]
${ASS_STYLE_FORMAT}
${DEFAULT_USER_ASS_STYLES}
`;

/** Format 行缺省时的标准 23 字段 */
export const ASS_FMT_DEFAULT = ASS_STYLE_FORMAT.slice(7).toLowerCase().split(",").map(s => s.trim());
