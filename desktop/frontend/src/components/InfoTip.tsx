import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "./InfoTip.css";

interface InfoTipProps {
  text: string;
  label?: string;
}

interface Position {
  top: number;
  left: number;
  placement: "top" | "bottom";
}

const VIEWPORT_MARGIN = 12;
const GAP = 8;

/** Portal-rendered hover/focus tooltip so cards, scroll panes and modals never clip it. */
export function InfoTip({ text, label = "说明" }: InfoTipProps) {
  const trimmed = text.trim();
  const triggerRef = useRef<HTMLSpanElement | null>(null);
  const bubbleRef = useRef<HTMLSpanElement | null>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<Position | null>(null);

  const reposition = useCallback(() => {
    const trigger = triggerRef.current;
    const bubble = bubbleRef.current;
    if (!trigger || !bubble) return;
    const rect = trigger.getBoundingClientRect();
    const bRect = bubble.getBoundingClientRect();
    const vw = window.innerWidth;

    const spaceAbove = rect.top - VIEWPORT_MARGIN;
    const placement: "top" | "bottom" = spaceAbove >= bRect.height + GAP ? "top" : "bottom";
    const top = placement === "top" ? rect.top - bRect.height - GAP : rect.bottom + GAP;
    const idealLeft = rect.left + rect.width / 2 - bRect.width / 2;
    const left = Math.max(VIEWPORT_MARGIN, Math.min(idealLeft, vw - bRect.width - VIEWPORT_MARGIN));
    setPos({ top, left, placement });
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    reposition();
  }, [open, reposition, trimmed]);

  useEffect(() => {
    if (!open) return;
    const update = () => reposition();
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [open, reposition]);

  if (!trimmed) return null;

  return (
    <>
      <span
        ref={triggerRef}
        className="info-tip"
        tabIndex={0}
        role="note"
        aria-label={`${label}：${trimmed}`}
        onClick={(event) => event.stopPropagation()}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="8" cy="8" r="6.4" />
          <path d="M8 7.2v3.6" />
          <circle className="info-tip-dot" cx="8" cy="5.1" r="0.75" />
        </svg>
      </span>
      {open && typeof document !== "undefined" && createPortal(
        <span
          ref={bubbleRef}
          className={`info-tip-portal ${pos ? "visible" : ""} ${pos?.placement ?? "top"}`.trim()}
          style={pos ? { top: `${pos.top}px`, left: `${pos.left}px` } : { top: "-9999px", left: "-9999px" }}
          role="tooltip"
        >
          {trimmed}
        </span>,
        document.body,
      )}
    </>
  );
}
