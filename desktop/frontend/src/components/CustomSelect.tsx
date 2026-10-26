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
  /** Long lists (fonts) get a filter box at the top of the menu */
  searchable?: boolean;
}

interface MenuPosition {
  // Opening upwards anchors the menu's bottom edge, so filtering the list
  // shrinks it towards the trigger instead of leaving it floating.
  top?: number;
  bottom?: number;
  left: number;
  width: number;
  maxHeight: number;
  side: "top" | "bottom";
}

const matches = (option: SelectOption<string | number>, query: string) =>
  option.label.toLocaleLowerCase().includes(query) || String(option.value).toLocaleLowerCase().includes(query);

export function CustomSelect<T extends string | number>({
  value,
  options,
  onChange,
  ariaLabel,
  className = "",
  disabled = false,
  compact = false,
  menuMaxHeight = 240,
  searchable = false,
}: CustomSelectProps<T>) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [position, setPosition] = useState<MenuPosition | null>(null);
  const [query, setQuery] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const typeahead = useRef({ query: "", at: 0 });
  const listboxId = `select-${useId().replaceAll(":", "")}`;
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value));
  const selected = options[selectedIndex];
  // Indices below (activeIndex, findEnabled, choose) refer to this list, which is
  // the full list whenever the filter is empty.
  const needle = query.trim().toLocaleLowerCase();
  const visible = searchable && needle ? options.filter((option) => matches(option, needle)) : options;

  const findEnabled = (start: number, direction: 1 | -1, list = visible) => {
    if (list.length === 0) return -1;
    for (let offset = 0; offset < list.length; offset += 1) {
      const index = (start + direction * offset + list.length) % list.length;
      if (!list[index]?.disabled) return index;
    }
    return -1;
  };

  const moveActive = (direction: 1 | -1) => {
    const next = findEnabled(activeIndex + direction, direction);
    if (next >= 0) setActiveIndex(next);
  };

  const openList = (preferredIndex = selectedIndex, initialQuery = "") => {
    if (disabled || options.length === 0) return;
    setQuery(initialQuery);
    if (initialQuery) {
      const lowered = initialQuery.toLocaleLowerCase();
      const next = findEnabled(0, 1, options.filter((option) => matches(option, lowered)));
      setActiveIndex(Math.max(0, next));
    } else {
      const next = findEnabled(preferredIndex, 1, options);
      setActiveIndex(next >= 0 ? next : 0);
    }
    setOpen(true);
  };

  const choose = (index: number) => {
    const option = visible[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    setOpen(false);
    trigger.current?.focus();
  };

  const filter = (next: string) => {
    setQuery(next);
    const lowered = next.trim().toLocaleLowerCase();
    const list = lowered ? options.filter((option) => matches(option, lowered)) : options;
    // An empty filter goes back to the current value, a typed one to the first hit
    const start = lowered ? 0 : selectedIndex;
    setActiveIndex(Math.max(0, findEnabled(start, 1, list)));
  };

  useEffect(() => {
    if (!open) return;

    const updatePosition = () => {
      const rect = trigger.current?.getBoundingClientRect();
      if (!rect) return;
      const gutter = 8;
      const gap = 6;
      const width = Math.min(Math.max(rect.width, 168), window.innerWidth - gutter * 2);
      const estimatedHeight = Math.min(options.length * 36 + 10 + (searchable ? 44 : 0), menuMaxHeight);
      const roomBelow = window.innerHeight - rect.bottom - gutter;
      const roomAbove = rect.top - gutter;
      const side = roomBelow < estimatedHeight && roomAbove > roomBelow ? "top" : "bottom";
      const maxHeight = Math.max(92, Math.min(menuMaxHeight, (side === "bottom" ? roomBelow : roomAbove) - gap));
      const left = Math.min(Math.max(gutter, rect.left), window.innerWidth - width - gutter);
      setPosition(side === "bottom"
        ? { top: rect.bottom + gap, left, width, maxHeight, side }
        : { bottom: window.innerHeight - rect.top + gap, left, width, maxHeight, side });
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
  }, [open, options.length, menuMaxHeight, searchable]);

  useEffect(() => {
    if (open && searchable) search.current?.focus();
  }, [open, searchable]);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`)?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open, query]);

  /** Arrow keys, Enter, Escape: shared by the trigger and the search box */
  const navigate = (event: React.KeyboardEvent<HTMLElement>): boolean => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        const edge = event.key === "ArrowDown" ? selectedIndex : options.length - 1;
        openList(edge);
      } else {
        moveActive(event.key === "ArrowDown" ? 1 : -1);
      }
      return true;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (open) choose(activeIndex);
      else openList();
      return true;
    }
    if (event.key === "Escape" && open) {
      event.preventDefault();
      setOpen(false);
      trigger.current?.focus();
      return true;
    }
    if (event.key === "Tab") {
      setOpen(false);
      return true;
    }
    return false;
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (navigate(event)) return;
    if (event.key === "Home" || event.key === "End") {
      if (!open) return;
      event.preventDefault();
      const edge = event.key === "Home" ? 0 : visible.length - 1;
      const next = findEnabled(edge, event.key === "Home" ? 1 : -1);
      if (next >= 0) setActiveIndex(next);
      return;
    }
    if (event.key === " ") {
      event.preventDefault();
      if (open) choose(activeIndex);
      else openList();
      return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      // A searchable list opens straight into its filter with the typed character
      if (searchable) {
        event.preventDefault();
        openList(selectedIndex, event.key);
        return;
      }
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

  const onSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    // Enter confirms an IME composition; it must not pick an option as well
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Tab") {
      // The menu lives in a portal at the end of <body>; hand focus back to the field
      event.preventDefault();
      setOpen(false);
      trigger.current?.focus();
      return;
    }
    navigate(event);
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
          className={`custom-select-menu ${searchable ? "searchable" : ""}`.trimEnd()}
          role="listbox"
          aria-label={ariaLabel}
          data-side={position?.side ?? "bottom"}
          style={position ? { top: position.top, bottom: position.bottom, left: position.left, width: position.width, maxHeight: position.maxHeight } : undefined}
        >
          {searchable && (
            <div className="custom-select-search">
              <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5" /><path d="m10.5 10.5 3 3" /></svg>
              <input
                ref={search}
                type="text"
                value={query}
                placeholder="搜索"
                aria-label={ariaLabel ? `搜索${ariaLabel}` : "搜索"}
                aria-controls={listboxId}
                spellCheck={false}
                autoComplete="off"
                onChange={(event) => filter(event.target.value)}
                onKeyDown={onSearchKeyDown}
              />
            </div>
          )}
          {visible.map((option, index) => (
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
          {visible.length === 0 && <div className="custom-select-empty">没有匹配项</div>}
        </div>,
        document.body,
      )}
    </div>
  );
}
