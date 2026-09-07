import type { ProviderID, TaskEvent } from "../providers/types.ts";

const OPERATOR_ONLY_WARNINGS = ["routing-", "srt-line-budget"];

function value(payload: Record<string, unknown>, key: string): string {
  return String(payload[key] ?? "");
}

function sameProgress(left: TaskEvent, right: TaskEvent): boolean {
  return left.type === "progress"
    && right.type === "progress"
    && value(left.payload, "stage") === value(right.payload, "stage")
    && value(left.payload, "completed") === value(right.payload, "completed")
    && value(left.payload, "total") === value(right.payload, "total")
    && value(left.payload, "unit") === value(right.payload, "unit");
}

/**
 * Cloud workers cross several containers, each of which has useful operator
 * diagnostics.  A task timeline is for users rather than operators: retain
 * lifecycle, handoffs, changed progress and actionable warnings, while hiding
 * diagnostics that remain available in Modal's own logs.
 *
 * Local logs stay unchanged because they are the only diagnostics available
 * for a process running on the user's machine.
 */
export function visibleTaskEvents(events: TaskEvent[], provider: ProviderID): TaskEvent[] {
  if (provider !== "cloud") return events;

  const visible: TaskEvent[] = [];
  for (const event of events) {
    if (event.type === "log") continue;
    // Provider/model routing is deployment telemetry, and srt-line-budget
    // reports a line the segmentation DP chose on purpose against a stricter
    // literal the SRT validator keeps to itself.  Both are intentionally
    // absent from the customer-facing cloud task log; the worker already
    // withholds them, and this also hides them in tasks recorded before it
    // did.
    if (event.type === "warning" && OPERATOR_ONLY_WARNINGS.some((code) => value(event.payload, "code").startsWith(code))) continue;

    const previous = visible.at(-1);
    if (previous && sameProgress(previous, event)) {
      // Prefer the newest heartbeat without allowing an unchanged counter to
      // consume one line every ten seconds.
      visible[visible.length - 1] = event;
      continue;
    }
    visible.push(event);
  }
  return visible;
}
