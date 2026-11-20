import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
  type TextareaHTMLAttributes,
} from "react";
import { createPortal } from "react-dom";
import { mountProceduralLumnoBackground } from "../lib/lumno-background.js";

export type LumnoTheme = "light" | "dark";

export function LumnoBackgroundStage({ theme }: { theme: LumnoTheme }) {
  const stageRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const handle = mountProceduralLumnoBackground(el, {
      preset: theme === "dark" ? "nocturne" : "coast",
      mode: "oil-veil",
      seed: 2026,
    });
    return () => {
      handle.destroy();
    };
  }, [theme]);

  return (
    <div
      ref={stageRef}
      className="lumno-oil-stage"
      aria-hidden="true"
    />
  );
}

export interface LumnoButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement> {
  ref?: Ref<HTMLButtonElement>;
  variant?: "primary" | "secondary" | "ghost";
  compact?: boolean;
  busy?: boolean;
  icon?: string;
  trailingIcon?: string;
  children?: ReactNode;
}

export function LumnoButton({
  ref,
  variant = "secondary",
  compact = false,
  busy = false,
  icon,
  trailingIcon,
  className = "",
  disabled,
  type = "button",
  children,
  ...rest
}: LumnoButtonProps) {
  const classes = [
    "lumno-btn",
    "lumno-pill",
    `lumno-btn--${variant}`,
    compact ? "lumno-btn--compact" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button
      ref={ref}
      type={type}
      className={classes}
      disabled={disabled || busy}
      aria-busy={busy ? "true" : undefined}
      {...rest}
    >
      <span className="lumno-busy-label">
        {icon ? <i className={`ri-icon ${icon}`} aria-hidden="true" /> : null}
        {children !== undefined ? <span>{children}</span> : null}
        {trailingIcon ? (
          <i className={`ri-icon ${trailingIcon}`} aria-hidden="true" />
        ) : null}
      </span>
      {busy ? <span className="lumno-busy-spinner" aria-hidden="true" /> : null}
    </button>
  );
}

export interface LumnoSwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  ariaLabel: string;
  compact?: boolean;
  disabled?: boolean;
}

export function LumnoSwitch({
  checked,
  onChange,
  ariaLabel,
  compact = false,
  disabled = false,
}: LumnoSwitchProps) {
  return (
    <label className={`lumno-switch ${compact ? "lumno-switch--compact" : ""}`}>
      <input
        type="checkbox"
        role="switch"
        aria-label={ariaLabel}
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="lumno-switch-slider" data-corner="round" />
    </label>
  );
}

export function LumnoInput({
  ref,
  className = "",
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & { ref?: Ref<HTMLInputElement> }) {
  return <input ref={ref} className={`lumno-input ${className}`.trim()} {...rest} />;
}

export function LumnoTextarea({
  ref,
  className = "",
  ...rest
}: TextareaHTMLAttributes<HTMLTextAreaElement> & {
  ref?: Ref<HTMLTextAreaElement>;
}) {
  return (
    <textarea ref={ref} className={`lumno-textarea ${className}`.trim()} {...rest} />
  );
}

export interface LumnoSelectOption {
  value: string;
  label: string;
  sublabel?: string;
  disabled?: boolean;
}

export interface LumnoSelectProps {
  value: string;
  options: ReadonlyArray<LumnoSelectOption>;
  onChange: (value: string) => void;
  ariaLabel: string;
  placeholder?: string;
  className?: string;
  footerAction?: {
    label: string;
    icon?: string;
    onClick: () => void;
  };
  disabled?: boolean;
}

export function LumnoSelect({
  value,
  options,
  onChange,
  ariaLabel,
  placeholder = "请选择",
  className = "",
  footerAction,
  disabled = false,
}: LumnoSelectProps) {
  const [open, setOpen] = useState(false);
  const [menuPos, setMenuPos] = useState<{
    top: number;
    left: number;
    width: number;
  }>({ top: 0, left: 0, width: 220 });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const listboxId = useId();

  const selectedOption = options.find((opt) => opt.value === value);

  const reposition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const minMenuWidth = Math.max(rect.width, 228);
    const left = Math.max(
      8,
      Math.min(rect.left, window.innerWidth - minMenuWidth - 8)
    );
    const estimatedHeight = Math.min(320, options.length * 42 + 64);
    const spaceBelow = window.innerHeight - rect.bottom - 12;
    const top =
      spaceBelow < estimatedHeight && rect.top > estimatedHeight
        ? Math.max(8, rect.top - estimatedHeight - 6)
        : Math.min(window.innerHeight - 48, rect.bottom + 6);
    setMenuPos({ top, left, width: minMenuWidth });
  }, [options.length]);

  useLayoutEffect(() => {
    if (!open) return;
    reposition();
  }, [open, reposition]);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (e: PointerEvent) => {
      const target = e.target as Node | null;
      if (
        triggerRef.current?.contains(target) ||
        menuRef.current?.contains(target)
      ) {
        return;
      }
      setOpen(false);
    };
    const handleKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open, reposition]);

  const moveSelection = (delta: 1 | -1) => {
    const enabled = options.filter((o) => !o.disabled);
    if (enabled.length === 0) return;
    const idx = enabled.findIndex((o) => o.value === value);
    const nextIdx =
      idx === -1 ? 0 : (idx + delta + enabled.length) % enabled.length;
    onChange(enabled[nextIdx].value);
  };

  const handleTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
      } else {
        moveSelection(1);
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
      } else {
        moveSelection(-1);
      }
    }
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={`lumno-select-trigger ${className}`.trim()}
        data-open={open ? "true" : "false"}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        disabled={disabled}
        onClick={() => setOpen((prev) => !prev)}
        onKeyDown={handleTriggerKeyDown}
      >
        <span className="lumno-select-label">
          {selectedOption ? selectedOption.label : placeholder}
        </span>
        <i
          className="ri-icon ri-arrow-down-s-line lumno-select-caret"
          aria-hidden="true"
        />
      </button>

      {open && typeof document !== "undefined"
        ? createPortal(
            <div
              ref={menuRef}
              id={listboxId}
              role="listbox"
              aria-label={ariaLabel}
              className="lumno-select-menu"
              style={{
                top: `${menuPos.top}px`,
                left: `${menuPos.left}px`,
                width: `${menuPos.width}px`,
              }}
            >
              {options.length === 0 ? (
                <div className="lumno-select-option-sub" style={{ padding: "8px 10px" }}>
                  暂无可选项
                </div>
              ) : (
                options.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    role="option"
                    aria-selected={opt.value === value}
                    disabled={opt.disabled}
                    className="lumno-select-option"
                    onClick={() => {
                      onChange(opt.value);
                      setOpen(false);
                      triggerRef.current?.focus();
                    }}
                  >
                    <span>{opt.label}</span>
                    {opt.sublabel ? (
                      <span className="lumno-select-option-sub">
                        {opt.sublabel}
                      </span>
                    ) : null}
                  </button>
                ))
              )}

              {footerAction ? (
                <>
                  <div className="lumno-select-divider" role="presentation" />
                  <button
                    type="button"
                    className="lumno-select-option"
                    onClick={() => {
                      setOpen(false);
                      footerAction.onClick();
                    }}
                  >
                    <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                      {footerAction.icon ? (
                        <i
                          className={`ri-icon ri-icon--compact ${footerAction.icon}`}
                          aria-hidden="true"
                        />
                      ) : null}
                      <span>{footerAction.label}</span>
                    </span>
                  </button>
                </>
              ) : null}
            </div>,
            document.body
          )
        : null}
    </>
  );
}

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  count?: number;
}

export interface LumnoSegmentedControlProps<T extends string> {
  value: T;
  options: ReadonlyArray<SegmentedOption<T>>;
  onChange: (value: T) => void;
  ariaLabel: string;
  wrap?: boolean;
  className?: string;
}

export function LumnoSegmentedControl<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
  wrap = false,
  className = "",
}: LumnoSegmentedControlProps<T>) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const indicatorRef = useRef<HTMLSpanElement | null>(null);
  const readyRef = useRef(false);

  const syncIndicator = useCallback(() => {
    const container = containerRef.current;
    const indicator = indicatorRef.current;
    if (!container || !indicator) return;
    const active = container.querySelector<HTMLButtonElement>(
      '[data-active="true"]'
    );
    if (!active) return;

    if (wrap) {
      indicator.style.width = `${active.offsetWidth}px`;
      indicator.style.height = `${active.offsetHeight}px`;
      indicator.style.transform = `translate(${active.offsetLeft - 1}px, ${active.offsetTop - 1}px)`;
    } else {
      indicator.style.width = `${active.offsetWidth}px`;
      indicator.style.height = "calc(100% - 2px)";
      indicator.style.transform = `translateX(${active.offsetLeft - 1}px)`;
    }

    if (!readyRef.current) {
      indicator.style.transition = "none";
      indicator.dataset.ready = "true";
      void indicator.offsetWidth;
      indicator.style.transition = "";
      readyRef.current = true;
    }
  }, [wrap]);

  useLayoutEffect(() => {
    syncIndicator();
  }, [value, options, syncIndicator]);

  useEffect(() => {
    const onResize = () => syncIndicator();
    window.addEventListener("resize", onResize);
    if (document.fonts?.ready) {
      void document.fonts.ready.then(syncIndicator);
    }
    return () => window.removeEventListener("resize", onResize);
  }, [syncIndicator]);

  const handleKeyDown = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const step =
      e.key === "ArrowRight" || e.key === "ArrowDown"
        ? 1
        : e.key === "ArrowLeft" || e.key === "ArrowUp"
          ? -1
          : 0;
    if (!step && e.key !== "Home" && e.key !== "End") {
      return;
    }
    e.preventDefault();
    const targetIdx =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? options.length - 1
          : (index + step + options.length) % options.length;
    onChange(options[targetIdx].value);
    const buttons =
      containerRef.current?.querySelectorAll<HTMLButtonElement>(".lumno-seg-option");
    buttons?.[targetIdx]?.focus();
  };

  return (
    <div
      ref={containerRef}
      role="radiogroup"
      aria-label={ariaLabel}
      className={`lumno-seg ${wrap ? "lumno-seg--wrap" : ""} ${className}`.trim()}
    >
      <span
        ref={indicatorRef}
        className="lumno-seg-indicator"
        data-ready="false"
        aria-hidden="true"
      />
      {options.map((opt, idx) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={active}
            data-active={active ? "true" : "false"}
            tabIndex={active ? 0 : -1}
            className="lumno-seg-option"
            onClick={() => onChange(opt.value)}
            onKeyDown={(e) => handleKeyDown(e, idx)}
          >
            <span>{opt.label}</span>
            {opt.count !== undefined ? (
              <span className="wb-tabular" style={{ opacity: 0.78 }}>
                {opt.count}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

export interface LumnoTooltipProps {
  content: ReactNode;
  children: ReactNode;
}

export function LumnoTooltip({ content, children }: LumnoTooltipProps) {
  const [visible, setVisible] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number }>({
    top: 0,
    left: 0,
  });
  const hostRef = useRef<HTMLSpanElement | null>(null);

  const updatePos = useCallback(() => {
    const el = hostRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const left = Math.max(
      12,
      Math.min(rect.left + rect.width / 2 - 140, window.innerWidth - 340)
    );
    const top =
      rect.bottom + 84 < window.innerHeight ? rect.bottom + 10 : Math.max(12, rect.top - 72);
    setPos({ top, left });
  }, []);

  useEffect(() => {
    if (!visible) return;
    updatePos();
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") setVisible(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visible, updatePos]);

  return (
    <span
      ref={hostRef}
      style={{ display: "inline-flex", alignItems: "center" }}
      onMouseEnter={() => {
        updatePos();
        setVisible(true);
      }}
      onMouseLeave={() => setVisible(false)}
      onFocus={() => {
        updatePos();
        setVisible(true);
      }}
      onBlur={() => setVisible(false)}
    >
      {children}
      {visible && typeof document !== "undefined"
        ? createPortal(
            <div
              role="tooltip"
              className="lumno-tooltip"
              style={{ top: `${pos.top}px`, left: `${pos.left}px` }}
            >
              {content}
            </div>,
            document.body
          )
        : null}
    </span>
  );
}

export function LumnoInfoButton({
  ariaLabel,
  tooltip,
}: {
  ariaLabel: string;
  tooltip: ReactNode;
}) {
  return (
    <LumnoTooltip content={tooltip}>
      <button type="button" className="lumno-info-button" aria-label={ariaLabel}>
        <i className="ri-icon ri-icon--compact ri-information-line" aria-hidden="true" />
      </button>
    </LumnoTooltip>
  );
}

export interface LumnoPopconfirmProps {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  requireReason?: boolean;
  reasonPlaceholder?: string;
  defaultReason?: string;
  onConfirm: (reason: string) => Promise<void> | void;
  children: (props: {
    onClick: () => void;
    ref: (node: HTMLButtonElement | null) => void;
    "aria-expanded": boolean;
  }) => ReactNode;
}

export function LumnoPopconfirm({
  title,
  description,
  confirmLabel = "确认执行",
  cancelLabel = "取消",
  requireReason = false,
  reasonPlaceholder = "请输入确认理由（2–500 字符）",
  defaultReason = "",
  onConfirm,
  children,
}: LumnoPopconfirmProps) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState(defaultReason);
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pos, setPos] = useState<{ top: number; right: number }>({
    top: 40,
    right: 16,
  });
  const anchorElRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const reposition = useCallback(() => {
    const anchor = anchorElRef.current;
    if (!anchor) return;
    const rect = anchor.getBoundingClientRect();
    const right = Math.max(12, window.innerWidth - rect.right);
    const top = Math.min(window.innerHeight - 190, rect.bottom + 6);
    setPos({ top, right });
  }, []);

  useLayoutEffect(() => {
    if (open) {
      reposition();
    }
  }, [open, reposition]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node | null;
      if (
        anchorElRef.current?.contains(target) ||
        panelRef.current?.contains(target)
      ) {
        return;
      }
      setOpen(false);
    };
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        anchorElRef.current?.focus();
      }
    };
    window.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", reposition);
    };
  }, [open, reposition]);

  const handleConfirm = async () => {
    const trimmed = reason.trim();
    if (requireReason && (trimmed.length < 2 || trimmed.length > 500)) {
      setReasonError("请填写 2–500 字符说明理由");
      return;
    }
    setBusy(true);
    try {
      await onConfirm(trimmed);
      setOpen(false);
      setReasonError(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {children({
        onClick: () => {
          setReason(defaultReason);
          setReasonError(null);
          setOpen((prev) => !prev);
        },
        ref: (node) => {
          anchorElRef.current = node;
        },
        "aria-expanded": open,
      })}

      {open && typeof document !== "undefined"
        ? createPortal(
            <div
              ref={panelRef}
              role="dialog"
              aria-label={title}
              className="lumno-popconfirm"
              style={{ top: `${pos.top}px`, right: `${pos.right}px` }}
            >
              <div className="lumno-popconfirm-title">{title}</div>
              {description ? (
                <div className="lumno-popconfirm-desc">{description}</div>
              ) : null}

              {requireReason ? (
                <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                  <LumnoInput
                    value={reason}
                    placeholder={reasonPlaceholder}
                    aria-label={reasonPlaceholder}
                    onChange={(e) => {
                      setReason(e.target.value);
                      if (e.target.value.trim().length >= 2) {
                        setReasonError(null);
                      }
                    }}
                  />
                  {reasonError ? (
                    <span className="wb-field-error">{reasonError}</span>
                  ) : null}
                </div>
              ) : null}

              <div className="lumno-popconfirm-actions">
                <LumnoButton
                  variant="secondary"
                  compact
                  onClick={() => {
                    setOpen(false);
                    anchorElRef.current?.focus();
                  }}
                >
                  {cancelLabel}
                </LumnoButton>
                <LumnoButton
                  variant="primary"
                  compact
                  busy={busy}
                  onClick={() => void handleConfirm()}
                >
                  {confirmLabel}
                </LumnoButton>
              </div>
            </div>,
            document.body
          )
        : null}
    </>
  );
}

export function LumnoModal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || typeof document === "undefined") {
    return null;
  }

  return createPortal(
    <div
      className="lumno-modal-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="lumno-modal-panel"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="lumno-modal-header">
          <h2 className="lumno-modal-title">{title}</h2>
          <LumnoButton
            variant="ghost"
            compact
            icon="ri-close-line"
            aria-label="关闭弹窗"
            onClick={onClose}
          />
        </div>
        <div className="lumno-modal-body">{children}</div>
        {footer ? <div className="lumno-modal-footer">{footer}</div> : null}
      </div>
    </div>,
    document.body
  );
}

export interface ToastItem {
  id: number;
  message: string;
  tone: "info" | "error";
}

export function LumnoToastHost({ toasts }: { toasts: ReadonlyArray<ToastItem> }) {
  if (toasts.length === 0 || typeof document === "undefined") {
    return null;
  }

  return createPortal(
    <div className="lumno-toast-host" aria-live="polite" role="status">
      {toasts.map((t) => (
        <div
          key={t.id}
          className="lumno-toast lumno-pill"
          data-tone={t.tone === "error" ? "error" : undefined}
        >
          {t.tone === "error" ? (
            <i className="ri-icon ri-icon--compact ri-error-warning-line" aria-hidden="true" />
          ) : (
            <i className="ri-icon ri-icon--compact ri-check-line" aria-hidden="true" />
          )}
          <span>{t.message}</span>
        </div>
      ))}
    </div>,
    document.body
  );
}
