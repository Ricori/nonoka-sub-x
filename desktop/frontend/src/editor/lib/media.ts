// <video> 与字幕画布的元素注册处：组件挂载时登记，命令式逻辑（播放、擦洗音、
// 字幕渲染）从这里拿元素，免得到处传 ref 或退回 getElementById。
//
// 舞台上叠着两个 <video>，同一个源：一个在前台播（video()），另一个藏在后面待命
// （standbyVideo()）。成片播放要跨过删掉的部分时，待命的那个提前跳到下一段起点、解好
// 第一帧，到点直接对调（swapVideo）——不用当场 seek，拼接处就不会卡一下。

let videoEls: [HTMLVideoElement | null, HTMLVideoElement | null] = [null, null];
let active = 0;
let subCanvasEl: HTMLCanvasElement | null = null;

/** 前台那个显示、出声；待命那个透明、静音（见 .stage video.standby） */
function applyRoles() {
  videoEls.forEach((el, k) => {
    if (!el) return;
    const on = k === active;
    el.classList.toggle("standby", !on);
    el.muted = !on;
  });
}

export const setVideoEls = (a: HTMLVideoElement | null, b: HTMLVideoElement | null) => {
  videoEls = [a, b];
  active = 0;
  applyRoles();
};
export const video = () => videoEls[active];
export const standbyVideo = () => videoEls[1 - active];
export const isActiveVideo = (el: HTMLVideoElement) => el === videoEls[active];

/** 前后台对调：调用方负责让新的前台接着播、旧的停下 */
export function swapVideo() {
  active = 1 - active;
  applyRoles();
}

export const setSubCanvasEl = (el: HTMLCanvasElement | null) => { subCanvasEl = el; };
export const subCanvas = () => subCanvasEl;

/** 视频可能还没就绪，写 currentTime 会抛，统一咽掉 */
export function safeSeekVideo(t: number) {
  const v = video();
  if (!v) return;
  try { v.currentTime = t; } catch { /* 未就绪 */ }
}
