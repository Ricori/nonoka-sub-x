import type { ExportSource, Range } from '../../bindings/github.com/Ricori/nonoka-x/desktop/internal/library/models.ts';

// 体积估算。x264 CRF 是恒定质量，真实码率取决于画面复杂度，只能事先猜个量级：
// 一路按像素量 × 经验 bpp 算（不看内容），一路按源视频码率换算（反映内容复杂度，
// 但源本身压得多狠不知道），两者取几何平均，互相抵消各自偏得离谱的情况。

const FULL_HD = 1920 * 1080;
/** 1080p30、CRF 23、medium 下的经验每像素比特数 */
const REF_BPP = 0.05;
/** 把源视频当作大约这个 CRF 的画质来换算 */
const SOURCE_CRF = 20;
/** 不知道源音频码率时的假设值（与 Go 端 assumedAudioKbps 一致） */
const ASSUMED_AUDIO_KBPS = 160;
/** 预设越慢同画质越小；相对 medium 的码率倍数 */
const PRESET_FACTOR: Record<string, number> = {
  ultrafast: 2.0, superfast: 1.6, veryfast: 1.25, faster: 1.1, fast: 1.05,
  medium: 1, slow: 0.95, slower: 0.92, veryslow: 0.9,
};

export interface EstimateInput {
  source: ExportSource;
  pieces: Range[] | null;
  crf: number;
  preset: string;
  scaleH: number;
  abr: string;
  joined: boolean;
}

/** 估算导出文件的字节数；源分辨率未知或时长为零时返回 null */
export function estimateExportBytes(input: EstimateInput): number | null {
  const { source } = input;
  const duration = (input.pieces ?? []).reduce((sum, piece) => sum + Math.max(0, piece.t1 - piece.t0), 0);
  if (duration <= 0 || source.width <= 0 || source.height <= 0) return null;

  // 与 Go 端的参数兜底保持一致
  const crf = Number.isFinite(input.crf) && input.crf >= 1 && input.crf <= 51 ? input.crf : 21;
  const presetFactor = PRESET_FACTOR[input.preset] ?? 1;
  const scaleH = input.scaleH >= 240 && input.scaleH <= 4320 ? input.scaleH : 0;
  const height = scaleH || source.height;
  const width = scaleH ? Math.round(source.width * scaleH / source.height / 2) * 2 : source.width;
  const pixels = width * height;
  const fps = source.fps > 0 ? source.fps : 30;

  // 分辨率越高、帧率越高，每像素每帧需要的比特越少：码率大致随像素量的 0.75 次方、
  // 帧率的 0.6 次方增长
  const byPixels = pixels * 30 * (fps / 30) ** 0.6 * REF_BPP * (pixels / FULL_HD) ** -0.25
    * 2 ** ((23 - crf) / 6) * presetFactor / 1000;
  let videoKbps = byPixels;
  if (source.videoKbps > 0) {
    const bySource = source.videoKbps * (pixels / (source.width * source.height)) ** 0.75
      * 2 ** ((SOURCE_CRF - crf) / 6) * presetFactor;
    videoKbps = Math.sqrt(byPixels * bySource);
  }

  let audioKbps = 0;
  if (source.hasAudio) {
    // 多段拼接时“复制源音轨”不可用，Go 端会退回 192k
    const abr = input.abr === 'copy' && input.joined ? '192k' : input.abr;
    audioKbps = abr === 'copy' ? (source.audioKbps || ASSUMED_AUDIO_KBPS) : (parseInt(abr, 10) || 192);
  }

  // 约 1% 的 MP4 封装开销
  return (videoKbps + audioKbps) * 1000 / 8 * duration * 1.01;
}

export function formatBytes(bytes: number): string {
  const mb = bytes / 1024 ** 2;
  if (mb >= 1024) return `${(mb / 1024).toFixed(mb >= 10 * 1024 ? 1 : 2)} GB`;
  return `${mb >= 10 ? mb.toFixed(0) : mb.toFixed(1)} MB`;
}
