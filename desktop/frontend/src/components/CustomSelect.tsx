import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "./CustomSelect.css";

export interface SelectOption<T extends string | number> {
  value: T;
  label: string;
  disabled?: boolean;
  hint?: string;
}

interface CustomSelectProps<T extends string | number> {
  value: T;
  options: readonly SelectOption<T>[];
  onChange: (value: T) => void;
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
  compact?: boolean;
  menuMaxHeight?: number;
}

interface MenuPosition {
  top: number;
  left: number;
  width: number;
  maxHeight: number;
  side: "top" | "bottom";
}

export function CustomSelect<T extends string | number>({
  value,
  options,
  onChange,
  ariaLabel,
  className = "",
  disabled = false,
  compact = false,
  menuMaxHeight = 240,
}: CustomSelectProps<T>) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [position, setPosition] = useState<MenuPosition | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const typeahead = useRef({ query: "", at: 0 });
  const listboxId = `select-${useId().replaceAll(":", "")}`;
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value));
  const selected = options[selectedIndex];

  const findEnabled = (start: number, direction: 1 | -1) => {
    if (options.length === 0) return -1;
    for (let offset = 0; offset < options.length; offset += 1) {
      const index = (start + direction * offset + options.length) % options.length;
      if (!options[index]?.disabled) return index;
    }
    return -1;
  };

  const moveActive = (direction: 1 | -1) => {
    const next = findEnabled(activeIndex + direction, direction);
    if (next >= 0) setActiveIndex(next);
  };

  const openList = (preferredIndex = selectedIndex) => {
    if (disabled || options.length === 0) return;
    const next = findEnabled(preferredIndex, 1);
    setActiveIndex(next >= 0 ? next : 0);
    setOpen(true);
  };

  const choose = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    setOpen(false);
    trigger.current?.focus();
  };

  useEffect(() => {
    if (!open) return;

    const updatePosition = () => {
      const rect = trigger.current?.getBoundingClientRect();
      if (!rect) return;
      const gutter = 8;
      const gap = 6;
      const width = Math.min(Math.max(rect.width, 168), window.innerWidth - gutter * 2);
      const estimatedHeight = Math.min(options.length * 36 + 10, menuMaxHeight);
      const roomBelow = window.innerHeight - rect.bottom - gutter;
      const roomAbove = rect.top - gutter;
      const side = roomBelow < estimatedHeight && roomAbove > roomBelow ? "top" : "bottom";
      const maxHeight = Math.max(92, Math.min(menuMaxHeight, (side === "bottom" ? roomBelow : roomAbove) - gap));
      const left = Math.min(Math.max(gutter, rect.left), window.innerWidth - width - gutter);
      const top = side === "bottom"
        ? rect.bottom + gap
        : Math.max(gutter, rect.top - gap - Math.min(estimatedHeight, maxHeight));
      setPosition({ top, left, width, maxHeight, side });
    };
    const closeOnOutside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!root.current?.contains(target) && !menu.current?.contains(target)) setOpen(false);
    };

    updatePosition();
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("scroll", updatePosition, true);
    window.addEventListener("resize", updatePosition);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("scroll", updatePosition, true);
      window.removeEventListener("resize", updatePosition);
    };
  }, [open, options.length, menuMaxHeight]);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`)?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        const edge = event.key === "ArrowDown" ? selectedIndex : options.length - 1;
        openList(edge);
      } else {
        moveActive(event.key === "ArrowDown" ? 1 : -1);
      }
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      if (!open) return;
      event.preventDefault();
      const edge = event.key === "Home" ? 0 : options.length - 1;
      const next = findEnabled(edge, event.key === "Home" ? 1 : -1);
      if (next >= 0) setActiveIndex(next);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (open) choose(activeIndex);
      else openList();
      return;
    }
    if (event.key === "Escape" && open) {
      event.preventDefault();
      setOpen(false);
      return;
    }
    if (event.key === "Tab") {
      setOpen(false);
      return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const now = Date.now();
      const query = `${now - typeahead.current.at > 600 ? "" : typeahead.current.query}${event.key.toLocaleLowerCase()}`;
      typeahead.current = { query, at: now };
      const start = open ? activeIndex + 1 : selectedIndex + 1;
      const match = [...options, ...options].slice(start, start + options.length).findIndex((option) => !option.disabled && option.label.toLocaleLowerCase().startsWith(query));
      if (match >= 0) {
        const next = (start + match) % options.length;
        if (open) setActiveIndex(next);
        else choose(next);
      }
    }
  };

  return (
    <div className={`custom-select ${compact ? "compact" : ""} ${open ? "open" : ""} ${className}`.trim()} ref={root}>
      <button
        ref={trigger}
        type="button"
        className="custom-select-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        disabled={disabled}
        onClick={() => open ? setOpen(false) : openList()}
        onKeyDown={onKeyDown}
      >
        <span className="custom-select-value">{selected?.label ?? "请选择"}</span>
        <svg className="custom-select-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
      </button>
      {open && createPortal(
        <div
          ref={menu}
          id={listboxId}
          className="custom-select-menu"
          role="listbox"
          aria-label={ariaLabel}
          data-side={position?.side ?? "bottom"}
          style={position ? { top: position.top, left: position.left, width: position.width, maxHeight: position.maxHeight } : undefined}
        >
          {options.map((option, index) => (
            <button
              key={String(option.value)}
              type="button"
              className={`custom-select-option ${index === activeIndex ? "active" : ""} ${option.value === value ? "selected" : ""} ${option.disabled ? "unavailable" : ""}`.trimEnd()}
              role="option"
              aria-selected={option.value === value}
              aria-disabled={option.disabled || undefined}
              tabIndex={option.disabled ? -1 : undefined}
              title={option.hint}
              data-index={index}
              onPointerEnter={() => { if (!option.disabled) setActiveIndex(index); }}
              onClick={() => choose(index)}
            >
              <span>{option.label}</span>
              <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8 3 3 6-6" /></svg>
            </button>
          ))}
        </div>,
        document.body,
      )}
    </div>
  );
}
