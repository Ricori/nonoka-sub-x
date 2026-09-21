import { shallowEqual } from '../../home/lib/createStore';
import { gotoNext, gotoPrev } from '../lib/edits';
import { setPlaying, setUserRate } from '../lib/playback';
import { playStore } from '../store/playStore';
import { fmtView, viewOutDur, viewStore } from '../store/viewStore';
import { fmt } from '../utils';

const RATES = [0.5, 0.75, 1, 1.5];

export function Transport() {
  const { t, playing, rate } = playStore.use(s => s, shallowEqual);
  // 进出聚焦、剪辑都会改总时长与时间码基准
  viewStore.use(s => ({ t0: s.t0, t1: s.t1, pieces: s.pieces, focus: s.focus }), shallowEqual);

  return (
    <div className="transport">
      <button className="tbtn" id="btn-prev" title="上一句 (↑)" onClick={gotoPrev}>
        <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
          <path d="M3 2h2v10H3zM12 2v10L5.5 7z" />
        </svg>
      </button>
      <button className="tbtn play" id="btn-play" title="播放/暂停 (Space)" onClick={() => setPlaying(!playing)}>
        <svg id="ic-play" width="14" height="14" viewBox="0 0 14 14" fill="currentColor"
          style={{ display: playing ? "none" : undefined }}>
          <path d="M3.5 2l9 5-9 5z" />
        </svg>
        <svg id="ic-pause" width="14" height="14" viewBox="0 0 14 14" fill="currentColor"
          style={{ display: playing ? undefined : "none" }}>
          <path d="M3 2h3v10H3zM8 2h3v10H8z" />
        </svg>
      </button>
      <button className="tbtn" id="btn-next" title="下一句 (↓)" onClick={gotoNext}>
        <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
          <path d="M9 2h2v10H9zM2 2v10l6.5-5z" />
        </svg>
      </button>
      <div className="timecode">
        <span id="tc-cur">{fmtView(t)}</span> <span className="total">/ <span id="tc-total">{fmt(viewOutDur())}</span></span>
      </div>
      <div className="spacer"></div>
      <div className="rate" id="rate">
        {RATES.map(r => (
          <button key={r} data-r={r} className={r === rate ? "on" : undefined}
            onClick={() => setUserRate(r)}>{r}×</button>
        ))}
      </div>
    </div>
  );
}
