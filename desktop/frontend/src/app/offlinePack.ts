import { Browser } from "@wailsio/runtime";

/**
 * Where people download the offline packages (the layer .tar.zst archives) when the online
 * download is slow, e.g. a mainland netdisk share. Empty: every link to it stays hidden.
 */
export const OFFLINE_PACK_URL: string = "";

export function openOfflinePackPage(): void {
  if (!OFFLINE_PACK_URL) return;
  void Browser.OpenURL(OFFLINE_PACK_URL).catch(() => window.open(OFFLINE_PACK_URL, "_blank", "noopener,noreferrer"));
}
