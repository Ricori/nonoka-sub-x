import { getPlayRes, getStyleSheet } from '../ass';
import { plateOn, resetSegPos, setSegFill, setSegPos, togglePlate } from '../lib/segPos';
import { docStore } from '../store/docStore';
import { AlignGrid, NumField, PropRow, Switch } from './ui/fields';
import { ColorField } from './ui/ColorField';
import { formatAssColor, hexOf, parseAssColor, parseCssHex } from '../../subtitles/color';
import { resolveStyleIn } from '../../subtitles/styles';
import type { Align, Seg, Ti } from '../types';

/**
 * Compact placement controls for a segment that carries its own position (on-screen text).
 * Values are shown as % of the frame for x / y / width and PlayRes pixels for the font size,
 * while the document stores frame fractions.
 */

const pct = (value: number) => Math.round(value * 1000) / 10;

/** What the segment's (or track's) translation style gives when pos has no w / fs of its own */
function styleDefaults(ti: Ti, seg: Seg) {
  const { tracks, trackMeta } = docStore.get();
  const lane = ti < 0 ? trackMeta?.zh : tracks[ti]?.zh;
  const sheet = getStyleSheet();
  const styleName = seg.style?.zh || lane?.style || "";
  const style = sheet.styleMap[resolveStyleIn(sheet, styleName, "zh")];
  const X = sheet.playRes.x;
  return {
    size: style?.size ?? 70,
    width: style ? Math.max(1, X - style.ml - style.mr) / X : 1,
  };
}

export function SegPosFields({ seg, ti }: { seg: Seg; ti: Ti }) {
  const pos = seg.pos;
  const cover = seg.cover;
  if (!pos && !cover) return null;
  const { y: Y } = getPlayRes();
  const fallback = styleDefaults(ti, seg);
  const plate = plateOn(seg);
  const fill = typeof cover?.fill === "string" ? parseCssHex(cover.fill) : null;

  return (
    <div className="insp-pos">
      {pos && <>
        <div className="insp-pos-row">
          <NumField label="X" value={pct(pos.x)} min={0} max={100} step={0.1} suffix="%"
            title="锚点离左边多远" onInput={value => setSegPos(seg, { x: value / 100 })} />
          <NumField label="Y" value={pct(pos.y)} min={0} max={100} step={0.1} suffix="%"
            title="锚点离上边多远" onInput={value => setSegPos(seg, { y: value / 100 })} />
        </div>
        <div className="insp-pos-row">
          <NumField label="宽" value={pct(pos.w ?? fallback.width)} min={1} max={100} step={0.1} suffix="%"
            title="超过这个宽度就换行" onInput={value => setSegPos(seg, { w: value / 100 })} />
          <NumField label="字号" value={Math.round(pos.fs ? pos.fs * Y : fallback.size)} min={4} max={400}
            title="放不下时会自动缩小一点" onInput={value => setSegPos(seg, { fs: value / Y })} />
        </div>
        <PropRow label="对齐">
          <AlignGrid value={pos.an} onPick={value => setSegPos(seg, { an: value as Align })} />
          {cover && <button type="button" className="side-text-btn" title="放回原文字的位置"
            onClick={() => resetSegPos(seg)}>回到原位</button>}
        </PropRow>
      </>}
      {cover && (
        <PropRow label="底板" title="盖住原文字的色块。关掉后不画，但导出时照样可以抹掉原文字">
          <Switch on={plate} title={plate ? "不画底板" : "画底板"} onToggle={on => togglePlate(seg, on)} />
          {plate && <ColorField value={formatAssColor(fill ?? parseAssColor(""))} palette={[]}
            onInput={value => setSegFill(seg, hexOf(parseAssColor(value)))} />}
        </PropRow>
      )}
    </div>
  );
}
