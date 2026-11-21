import { useEffect, useLayoutEffect, useRef } from 'react';
import { closeCtx, ctxStore } from '../store/uiStore';

/**
 * 通用右键 / 下拉菜单。anchor 给了就钉在那个矩形下方（贴右边缘对齐，跟主页顶栏
 * 「更多」菜单一个套路），不跟着鼠标跑；不给就开在光标处。
 */
export function ContextMenu() {
  const menu = ctxStore.use(s => s.menu);
  const ref = useRef<HTMLDivElement>(null);

  // 先挂上去量到真实尺寸，再决定往左/往上翻，免得贴边时被切掉
  useLayoutEffect(() => {
    const m = ref.current;
    if (!menu || !m) return;
    const r = m.getBoundingClientRect();
    if (menu.anchor) {
      const ar = menu.anchor;
      m.style.top = Math.min(ar.bottom + 6, innerHeight - r.height - 6) + "px";
      // 贴右边缘对齐：菜单绝不会探到按钮右侧，天然避开顶栏右上角的系统窗口按钮
      m.style.right = Math.max(6, innerWidth - ar.right) + "px";
      m.style.left = "auto";
    } else {
      const top = Math.min(menu.y, innerHeight - r.height - 6);
      // 顶栏右上角的系统窗口按钮画在合成层上，页面 z-index 再高也盖不住，
      // 所以只在可能撞进那条 48px 高的带子时收窄右边界
      let maxRight = innerWidth - 6;
      if (top < 48) {
        const tb = document.querySelector(".topbar");
        if (tb) maxRight = tb.getBoundingClientRect().right - parseFloat(getComputedStyle(tb).paddingRight || "0") - 6;
      }
      m.style.left = Math.min(menu.x, maxRight - r.width) + "px";
      m.style.top = top + "px";
      m.style.right = "auto";
    }
  }, [menu]);

  useEffect(() => {
    if (!menu) return;
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) closeCtx();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeCtx(); };
    addEventListener("pointerdown", onDown, true);
    addEventListener("blur", closeCtx);
    addEventListener("keydown", onKey);
    return () => {
      removeEventListener("pointerdown", onDown, true);
      removeEventListener("blur", closeCtx);
      removeEventListener("keydown", onKey);
    };
  }, [menu]);

  if (!menu) return null;
  return (
    <div className="ctxmenu" ref={ref}>
      {menu.items.map((it, i) => it === "-"
        ? <div className="sep" key={"sep" + i} />
        : <button key={it.label + i} className={it.danger ? "danger" : undefined}
          disabled={it.disabled}
          onClick={() => { if (it.disabled) return; closeCtx(); it.onClick(); }}>{it.label}</button>)}
    </div>
  );
}
