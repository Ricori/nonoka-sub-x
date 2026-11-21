export function StatusBar() {
  return (
    <footer className="statusbar">
      <span><kbd>Space</kbd>播放/暂停</span>
      <span><kbd>←</kbd><kbd>→</kbd>步进</span>
      <span><kbd>↑</kbd><kbd>↓</kbd>上/下一句</span>
      <span><kbd>I</kbd><kbd>O</kbd>设入点/出点到当前位置</span>
      <span><kbd>N</kbd>在当前位置新建字幕</span>
      <span><kbd>D</kbd>在当前位置拆分</span>
      <span><kbd>V</kbd>延长本句</span>
      <span><kbd>Del</kbd>删除选中句</span>
      <span><kbd>Ctrl</kbd><kbd>C/X/V</kbd>复制/剪切/粘贴</span>
      <span><kbd>Ctrl</kbd><kbd>Enter</kbd>文本框内确认</span>
      <span><kbd>Ctrl</kbd>+滚轮 缩放时间轴</span>
      <span><kbd>Alt</kbd>+滚轮 纵向滚轨道</span>
      <span className="spacer"></span>
      <span className="stats" id="stats"></span>
    </footer>
  );
}
