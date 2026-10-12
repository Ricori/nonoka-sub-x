import { createStore } from '../../home/lib/createStore';

/**
 * 视频区的占位卡与角标。视频挂载走「缓存副本 → 原文件还在原处 → 从 R2 取回 →
 * 让用户重新定位」这条解析链，②③④ 都不阻塞字幕编辑：视频区显示占位卡，
 * 字幕/波形/时间轴照常可用。
 */
interface VideoState {
  /** 已挂上的 media:// 地址；空串 = 还没挂 */
  src: string;
  fallbackOpen: boolean;
  collapsed: boolean;
  /** 正在从云端取回：必须和「请选择本地文件」明确区分开 */
  retrieving: boolean;
  /** 取回进度文案，如 "42%（120 MB / 280 MB）" */
  retrievePct: string;
  /** 正在转码成 H.264：和取回一样占着占位卡，只是不给「暂不加载」 */
  transcoding: boolean;
  /** 转码进度文案，如 "42%（00:12 / 00:30）" */
  transcodePct: string;
  /** 播放失败（多半是编码不支持）时才给转码按钮 */
  canTranscode: boolean;
  /** 占位卡当前的说明文案（折叠/展开时复用，不被默认文案覆盖） */
  fbMsg: string;
  warn: string;
  /** 校验没过时「仍然使用」要挂的文件路径 */
  usePath: string | null;
  badge: string | null;
  /** 字幕预览渲染器的忙碌/失败提示 */
  subBusy: string | null;
  /** 纯音频素材：舞台只有字幕没有画面，缩略图条和内嵌字幕导出都不可用 */
  audioOnly: boolean;
}

export const videoStore = createStore<VideoState>({
  src: "", fallbackOpen: false, collapsed: false, retrieving: false, retrievePct: "",
  transcoding: false, transcodePct: "", canTranscode: false,
  fbMsg: "", warn: "", usePath: null, badge: null, subBusy: null, audioOnly: false,
});
