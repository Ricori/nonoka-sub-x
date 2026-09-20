export const CLOUD_MAX_DURATION_SECONDS = 2 * 60 * 60;

export function cloudAcceptsDuration(duration: number): boolean {
  return Number.isFinite(duration) && duration > 0 && duration <= CLOUD_MAX_DURATION_SECONDS;
}
