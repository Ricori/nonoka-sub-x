import { useEffect } from 'react';
import { getStyleNames, resolveStyle } from '../ass';
import { bindStyle, renameTarget } from '../lib/edits';
import { docStore } from '../store/docStore';
import { closeTrackPop, modalStore } from '../store/uiStore';
import type { Lang } from '../types';

/**
 * 样式下拉。这个视频的样式表里没有的绑定值一律显示成回退后的 origin/cn——预览和导出就是
 * 这么出的（见 ass.ts::resolveStyle），下拉再显示那个查无此人的名字只会让两边对不上。
 */
function effStyle(value: string | null | undefined, lang: Lang) {
  const v = value || "";
  if (v && getStyleNames().includes(v)) return v;
  return resolveStyle(v, lang);
}

/**
 * 下拉最后那一项的值。样式名是从 Style 行里逐行解析出来的，不可能含换行，
 * 拿它开头就不会跟任何真样式撞名——选中它只开样式模板，绝不落到绑定上。
 */
const NEW_STYLE = "\n新增样式";

function StyleSelect({ id, value, lang, onPick }: {
  id: string; value: string | null; lang: Lang; onPick(v: string): void;
}) {
  return (
    <select id={id} value={effStyle(value, lang)} onChange={e => {
      // 样式模板是全屏弹窗，弹层留在后面也会被第一次点击关掉，不如自己先收干净
      if (e.target.value === NEW_STYLE) { closeTrackPop(); modalStore.set({ tplOpen: true }); return; }
      onPick(e.target.value);
    }}>
      {getStyleNames().map(n => <option key={n} value={n}>{n}</option>)}
      <option value={NEW_STYLE}>＋ 新增样式</option>
    </select>
  );
}

/** 轨道设置弹层：改轨道名 / 绑当前 lane 的 ASS 样式。改动即时生效 */
export function TrackPopover() {
  const pop = modalStore.use(s => s.trkPop);
  docStore.use(s => s.version);   // 改名/换绑后要跟着刷新
  const { tracks, trackMeta } = docStore.get();

  // 点弹层外关闭
  useEffect(() => {
    if (!pop) return;
    const onDown = (e: PointerEvent) => {
      const el = e.target instanceof Element ? e.target : null;
      if (el?.closest("#trk-pop") || el?.closest(".lbtn.gear")) return;
      closeTrackPop();
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [pop]);

  if (!pop) return <div className="trk-pop" id="trk-pop" hidden />;

  const { target, rect } = pop;
  const isTrack = target.kind === "track";
  const tr = isTrack ? tracks[target.ti] : null;
  if (isTrack && !tr) return <div className="trk-pop" id="trk-pop" hidden />;

  const { lang } = target;
  const name = isTrack ? (tr!.name || "") : (trackMeta?.name || "默认轨");
  const styleTarget = isTrack ? { kind: "track" as const, ti: target.ti } : { kind: "default" as const };
  const style = isTrack ? tr![lang].style : (trackMeta?.[lang].style ?? null);

  return (
    <div className="trk-pop" id="trk-pop" style={{
      left: Math.min(rect.left, window.innerWidth - 236) + "px",
      bottom: (window.innerHeight - rect.top + 6) + "px",
      top: "auto",
    }}>
      <div id="tp-name-wrap">
        <label>轨道名称</label>
        {/* 不能在 onChange 里 trim：受控输入会把刚敲下的空格立刻吃掉，名字里就打不出空格 */}
        <input id="tp-name" spellCheck={false} value={name}
          onChange={e => renameTarget(e.target.value, styleTarget)}
          onBlur={e => renameTarget(e.target.value.trim(), styleTarget)} />
      </div>
      {/* 只绑齿轮所在那一行（原文 / 译文）的样式 */}
      <div className="tp-lane">
        <label>{lang === "ja" ? "原文样式" : "译文样式"}</label>
        <StyleSelect id="tp-style" lang={lang} value={style}
          onPick={v => bindStyle(lang, v, styleTarget)} />
      </div>
    </div>
  );
}
