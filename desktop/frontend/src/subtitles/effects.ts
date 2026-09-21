import type {
  Lang, SubtitleEffectBinding, SubtitleEffectParam, SubtitleEffectTarget,
} from './types.ts';

export const DEFAULT_EFFECT_TRACK_ID = "default";

interface EffectParamBase {
  key: string;
  label: string;
}

export interface EffectNumberParam extends EffectParamBase {
  control?: "number";
  min: number;
  max: number;
  step: number;
  defaultValue: number;
  suffix: string;
}

/** 颜色参数存 #rrggbb；空串 = 跟随样式（该处不写颜色标签） */
export interface EffectColorParam extends EffectParamBase {
  control: "color";
  defaultValue: string;
  hint?: string;
}

export type EffectParamDefinition = EffectNumberParam | EffectColorParam;

export const isColorParam = (param: EffectParamDefinition): param is EffectColorParam =>
  param.control === "color";

export interface EffectTemplateDefinition {
  id: string;
  name: string;
  description: string;
  kind: "transform" | "generator";
  /** 需要逐字（K 轴）时间；没有 K 轴时退回按字数均分，编辑器据此弹出 K 轴面板 */
  needsKaraoke?: boolean;
  params: EffectParamDefinition[];
}

/** 内置模板是声明式白名单；外部 ASS 中的 Lua/template 行绝不直接执行。 */
export const EFFECT_TEMPLATES: EffectTemplateDefinition[] = [
  {
    id: "fade",
    name: "渐入渐出",
    description: "整句透明度渐入、渐出，适合对白和普通歌词。",
    kind: "transform",
    params: [
      { key: "inMs", label: "渐入", min: 0, max: 10_000, step: 50, defaultValue: 200, suffix: "ms" },
      { key: "outMs", label: "渐出", min: 0, max: 10_000, step: 50, defaultValue: 200, suffix: "ms" },
    ],
  },
  {
    id: "character-particle",
    name: "逐字粒子",
    description: "逐字缩放显现，并在每个字周围生成旋转散射的矢量碎片。",
    kind: "generator",
    params: [
      { key: "staggerMs", label: "逐字间隔", min: 0, max: 1000, step: 10, defaultValue: 80, suffix: "ms" },
      { key: "enterMs", label: "入场时长", min: 50, max: 3000, step: 50, defaultValue: 300, suffix: "ms" },
      { key: "scaleFrom", label: "起始缩放", min: 10, max: 150, step: 5, defaultValue: 70, suffix: "%" },
      { key: "particleCount", label: "每字粒子", min: 0, max: 25, step: 1, defaultValue: 10, suffix: "个" },
      { key: "particleDurationMs", label: "粒子时长", min: 100, max: 5000, step: 50, defaultValue: 900, suffix: "ms" },
      { key: "scatterPx", label: "散射距离", min: 0, max: 600, step: 10, defaultValue: 180, suffix: "px" },
    ],
  },
  {
    id: "karaoke-particle",
    name: "K轴逐字粒子",
    description: "整行在句首前逐字淡入，每个字在自己的音节时刻脉冲放大，并甩出一片旋转飘散的星形碎片；碎片在音节结束后仍会飘一阵再散尽。脉冲单程涨落默认压在 280ms 以内；「脉冲上限」设 0 则改为跟着音节长短走（Aegisub 模板的口径，长音会涨得很慢）。没有 K 轴的句子退回按字数均分。",
    kind: "generator",
    needsKaraoke: true,
    params: [
      { key: "entryStaggerMs", label: "入场错峰", min: 0, max: 500, step: 10, defaultValue: 50, suffix: "ms" },
      { key: "fadeInMs", label: "入场渐显", min: 0, max: 2000, step: 50, defaultValue: 300, suffix: "ms" },
      { key: "popScale", label: "脉冲峰值", min: 100, max: 250, step: 5, defaultValue: 120, suffix: "%" },
      { key: "popMs", label: "脉冲上限", min: 0, max: 5000, step: 10, defaultValue: 280, suffix: "ms" },
      { key: "holdMs", label: "音节后停留", min: 0, max: 20_000, step: 100, defaultValue: 400, suffix: "ms" },
      { key: "particleCount", label: "每字粒子", min: 0, max: 25, step: 1, defaultValue: 20, suffix: "个" },
      { key: "spawnMs", label: "粒子错峰", min: 0, max: 3000, step: 50, defaultValue: 400, suffix: "ms" },
      { key: "particleLifeMs", label: "粒子余生", min: 100, max: 6000, step: 100, defaultValue: 1500, suffix: "ms" },
      { key: "particleScale", label: "粒子大小", min: 20, max: 400, step: 5, defaultValue: 110, suffix: "%" },
      { key: "spreadPx", label: "横向散布", min: 0, max: 900, step: 10, defaultValue: 300, suffix: "px" },
      { key: "driftPx", label: "纵向抖动", min: 0, max: 400, step: 5, defaultValue: 60, suffix: "px" },
      { key: "swapPrimary", label: "收尾主色", control: "color", defaultValue: "#aec8dc",
        hint: "字落回原大小时主色渐变到这里；留空则保持样式主色" },
      { key: "swapOutline", label: "收尾描边色", control: "color", defaultValue: "#ffffff",
        hint: "同一段渐变里的描边色；留空则保持样式描边色" },
      { key: "particleOutline", label: "粒子描边色", control: "color", defaultValue: "",
        hint: "留空 = 跟随样式描边色" },
    ],
  },
];

export const EFFECT_TEMPLATE_MAP = Object.fromEntries(EFFECT_TEMPLATES.map(item => [item.id, item]));

export function defaultEffectParams(templateId: string): Record<string, SubtitleEffectParam> {
  const definition = EFFECT_TEMPLATE_MAP[templateId];
  return Object.fromEntries((definition?.params ?? []).map(param => [param.key, param.defaultValue]));
}

const numberParam = (value: unknown, definition: EffectNumberParam): number => {
  const numeric = Number(value);
  const finite = Number.isFinite(numeric) ? numeric : definition.defaultValue;
  const stepped = definition.step >= 1 ? Math.round(finite) : finite;
  return Math.min(definition.max, Math.max(definition.min, stepped));
};

/** 只收 #rrggbb 和空串（空串 = 跟随样式）；别的一律落回默认值 */
const colorParam = (value: unknown, definition: EffectColorParam): string => {
  const raw = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (!raw) return typeof value === "string" ? "" : String(definition.defaultValue);
  const m = /^#?([0-9a-f]{6})$/.exec(raw);
  return m ? `#${m[1]}` : String(definition.defaultValue);
};

export function normalizeEffectParams(templateId: string, value: unknown): Record<string, SubtitleEffectParam> {
  const raw = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const definition = EFFECT_TEMPLATE_MAP[templateId];
  if (!definition) return {};
  return Object.fromEntries(definition.params.map(param => [param.key,
    isColorParam(param) ? colorParam(raw[param.key], param) : numberParam(raw[param.key], param)]));
}

function normalizeTarget(value: unknown): SubtitleEffectTarget | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (raw.scope === "all") return { scope: "all" };
  if (raw.scope === "track" && typeof raw.trackId === "string" && raw.trackId)
    return { scope: "track", trackId: raw.trackId };
  if (raw.scope === "lane" && typeof raw.trackId === "string" && raw.trackId
    && (raw.lang === "ja" || raw.lang === "zh"))
    return { scope: "lane", trackId: raw.trackId, lang: raw.lang };
  return null;
}

export function normalizeEffectBindings(value: unknown): SubtitleEffectBinding[] {
  if (!Array.isArray(value)) return [];
  const byKey = new Map<string, SubtitleEffectBinding>();
  value.forEach((item, index) => {
    if (!item || typeof item !== "object") return;
    const raw = item as Record<string, unknown>;
    const templateId = typeof raw.templateId === "string" ? raw.templateId : "";
    const target = normalizeTarget(raw.target);
    if (!templateId || !target || !EFFECT_TEMPLATE_MAP[templateId]) return;
    const binding: SubtitleEffectBinding = {
      id: typeof raw.id === "string" && raw.id ? raw.id : `effect-${index + 1}`,
      templateId,
      enabled: raw.enabled !== false,
      target,
      params: normalizeEffectParams(templateId, raw.params),
    };
    // 插件或旧版本可能写入重复绑定；与运行时解析一致，后出现的那条覆盖前一条。
    byKey.set(effectBindingKey(templateId, target), binding);
  });
  return [...byKey.values()];
}

export function effectTargetKey(target: SubtitleEffectTarget): string {
  if (target.scope === "all") return "all";
  if (target.scope === "track") return `track:${target.trackId}`;
  return `lane:${target.trackId}:${target.lang}`;
}

export function effectBindingKey(templateId: string, target: SubtitleEffectTarget): string {
  return `${templateId}|${effectTargetKey(target)}`;
}

/** 这个模板会不会把整句拆成逐字事件（同一条 lane 上只能有一个） */
export const isGeneratorTemplate = (templateId: string): boolean =>
  EFFECT_TEMPLATE_MAP[templateId]?.kind === "generator";

/** 两个作用范围会不会落到同一条 lane 上 */
export function targetsOverlap(a: SubtitleEffectTarget, b: SubtitleEffectTarget): boolean {
  if (a.scope === "all" || b.scope === "all") return true;
  if (a.trackId !== b.trackId) return false;
  if (a.scope === "track" || b.scope === "track") return true;
  return a.lang === b.lang;
}

/** 同一模板命中多个范围时，lane 覆盖 track，track 覆盖 all，避免重复套用。 */
export function resolveLaneEffects(bindings: SubtitleEffectBinding[] | undefined,
  trackId: string, lang: Lang): SubtitleEffectBinding[] {
  const chosen = new Map<string, { specificity: number; binding: SubtitleEffectBinding }>();
  for (const binding of bindings ?? []) {
    if (!binding.enabled) continue;
    const target = binding.target;
    let specificity = 0;
    if (target.scope === "all") specificity = 1;
    else if (target.scope === "track" && target.trackId === trackId) specificity = 2;
    else if (target.scope === "lane" && target.trackId === trackId && target.lang === lang) specificity = 3;
    if (!specificity) continue;
    const current = chosen.get(binding.templateId);
    if (!current || specificity >= current.specificity)
      chosen.set(binding.templateId, { specificity, binding });
  }
  // 生成型模板各自把整句拆成逐字事件，叠加只会互相盖住：一条 lane 只留一个，
  // 范围更具体的赢，同样具体则按文档里的先后取先出现的那个（结果可复现）。
  const out: SubtitleEffectBinding[] = [];
  let generator: { specificity: number; binding: SubtitleEffectBinding } | null = null;
  for (const item of chosen.values()) {
    if (!isGeneratorTemplate(item.binding.templateId)) out.push(item.binding);
    else if (!generator || item.specificity > generator.specificity) generator = item;
  }
  if (generator) out.push(generator.binding);
  return out;
}

export function targetLabel(target: SubtitleEffectTarget): string {
  if (target.scope === "all") return "全部字幕轴";
  if (target.scope === "track") return target.trackId === DEFAULT_EFFECT_TRACK_ID ? "默认轨（双语）" : "整条轨道";
  const lane = target.lang === "ja" ? "日语" : "中文";
  return target.trackId === DEFAULT_EFFECT_TRACK_ID ? `默认轨 · ${lane}` : lane;
}
