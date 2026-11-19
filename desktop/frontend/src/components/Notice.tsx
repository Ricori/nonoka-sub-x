import { useCallback, useEffect, useRef, useState } from "react";
import "./Notice.css";

const defaultDismissDelayMs = 6000;

/** Successful actions read as success; anything else keeps the warning look. */
export type NoticeTone = "warn" | "success";

type NoticeProps = {
  message: string;
  /** Keeps the existing per-site styling (.library-message, .keys-message, …). */
  className?: string;
  tone?: NoticeTone;
  /** 0 disables the timer, leaving the notice up until it is closed by hand. */
  autoDismissMs?: number;
  /** Clears the caller's message state. Omit for notices derived from live state,
      which can only be hidden locally. */
  onDismiss?: () => void;
};

export function Notice({ message, className = "", tone = "warn", autoDismissMs = defaultDismissDelayMs, onDismiss }: NoticeProps) {
  const [dismissed, setDismissed] = useState("");
  const [held, setHeld] = useState(false);
  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;

  const close = useCallback(() => {
    setHeld(false);
    setDismissed(message);
    onDismissRef.current?.();
  }, [message]);

  const hidden = !message || dismissed === message;

  useEffect(() => {
    // The parent clears controlled notices after close/auto-dismiss. Forget the
    // locally dismissed text at that boundary so the exact same error can be
    // shown again when a retry fails with the same message.
    if (!message && dismissed) setDismissed("");
  }, [dismissed, message]);

  useEffect(() => {
    // Hiding unmounts the <p> before onMouseLeave/onBlur can fire; clear held
    // so a subsequent notice still auto-dismisses.
    if (hidden && held) setHeld(false);
  }, [held, hidden]);

  useEffect(() => {
    // Pointer or keyboard focus inside the notice holds the timer: a message the
    // reader is still working through should not disappear underneath them.
    if (hidden || held || autoDismissMs <= 0) return;
    const timer = window.setTimeout(close, autoDismissMs);
    return () => window.clearTimeout(timer);
  }, [hidden, held, autoDismissMs, close]);

  if (hidden) return null;

  return (
    <p
      className={`notice notice-${tone} ${className}`.trim()}
      role="status"
      aria-live="polite"
      onMouseEnter={() => setHeld(true)}
      onMouseLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={() => setHeld(false)}
    >
      <span className="notice-text">{message}</span>
      <button className="notice-close" type="button" aria-label="关闭提示" title="关闭提示" onClick={close}>
        <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" /></svg>
      </button>
    </p>
  );
}
