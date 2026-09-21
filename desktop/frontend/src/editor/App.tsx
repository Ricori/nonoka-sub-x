import { useEffect, useState } from 'react';
import { Events } from '@wailsio/runtime';
import { ClipTip } from './components/ClipTip';
import { ContextMenu } from './components/ContextMenu';
import { Inspector } from './components/Inspector';
import { SegList } from './components/SegList';
import { StatusBar } from './components/StatusBar';
import { Timeline } from './components/Timeline';
import { Toast } from './components/Toast';
import { TopBar } from './components/TopBar';
import { TrackPopover } from './components/TrackPopover';
import { Transport } from './components/Transport';
import { StyleBar } from './components/stage/StyleBar';
import { VideoStage } from './components/VideoStage';
import { AskModal } from './components/modals/AskModal';
import { CloseModal } from './components/modals/CloseModal';
import { EffectsModal } from './components/modals/EffectsModal';
import { KaraokeModal } from './components/modals/KaraokeModal';
import { TemplateModal } from './components/modals/TemplateModal';
import { useDesktopEvents } from './hooks/useDesktopEvents';
import { useShortcuts } from './hooks/useShortcuts';
import { runBootSequence } from './lib/boot';
import { splitHandler } from './lib/split';
import { destroySubtitles } from './lib/subtitles';
import { getVid, initSession } from './session';
import { layoutStore, saveLayout } from './store/layoutStore';
import { modalStore } from './store/uiStore';

export function EditorApp() {
  const bootDone = modalStore.use(s => s.bootDone);
  const sideW = layoutStore.use(s => s.sideW);
  const [started, setStarted] = useState(false);

  useDesktopEvents();
  useShortcuts();

  // 原生编辑器窗口先保持隐藏；主题和字幕布局提交到 DOM 后再通知后端切换窗口。
  useEffect(() => {
    if (!bootDone) return;
    void Events.Emit("editor:ready", getVid());
  }, [bootDone]);

  // 配置与运行时信息就绪后才开始拉字幕：BACKEND / taskKey / vid 都从那儿来
  useEffect(() => {
    let alive = true;
    initSession().then(ok => {
      if (!ok || !alive || started) return;
      setStarted(true);
      void runBootSequence();
    });
    return () => {
      alive = false;
      destroySubtitles();
      document.body.classList.remove("ctx-open");
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <div className="app editor-app">
        <TopBar />

        <div className="main" style={{ gridTemplateColumns: `minmax(0,1fr) 6px ${sideW}px` }}>
          <section className="preview-pane">
            <VideoStage />
            {/* 选中画面里的字幕才出现。它在流内而不是浮层：舞台会变矮，fitStage 的
                ResizeObserver 自动重适配，于是字幕本身永远不会被盖住 */}
            <StyleBar />
            <Transport />
          </section>

          <div className="vsplit" id="vsplit" title="拖动调整侧栏宽度"
            onPointerDown={splitHandler(() => layoutStore.get().sideW, (v0, dx) => {
              layoutStore.set({ sideW: Math.min(Math.max(v0 - dx, 280), 640) });
              saveLayout();
            })} />

          <aside className="side">
            <Inspector />
            <SegList />
          </aside>
        </div>

        {/* 拖动改轨道区可视高度（单条行高另有行间手柄）；轨道总高超过它就纵向滚动 */}
        <div className="hsplit" id="hsplit" title="拖动调整时间轴高度"
          onPointerDown={splitHandler(() => layoutStore.get().tlViewH, (v0, _dx, dy) => {
            // 高度落到 DOM 之后才有新的 scrollHeight，指标同步交给 Timeline 的 layout effect
            layoutStore.set({ tlViewH: Math.min(Math.max(v0 - dy, 150), 900) });
            saveLayout();
          })} />

        <Timeline />
        <StatusBar />
      </div>

      <CloseModal />
      <AskModal />
      <TemplateModal />
      <EffectsModal />
      <KaraokeModal />
      <TrackPopover />
      <ContextMenu />
      <ClipTip />
      <Toast />
    </>
  );
}
