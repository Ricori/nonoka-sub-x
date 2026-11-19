import type { ReactNode } from "react";

export type MarkTone = "neutral" | "accent" | "ok" | "caution" | "danger";

/** 状态小标签。颜色只按语义分五档，见 design-system.css。 */
export function Mark({ children, tone = "neutral" }: { children: ReactNode; tone?: MarkTone }) {
  return <span className={`mark tone-${tone}`}>{children}</span>;
}
