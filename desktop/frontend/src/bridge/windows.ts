import { WindowService } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/app/index.js";

export const desktopWindows = {
  openEditor: WindowService.OpenEditor,
  closeEditor: WindowService.CloseEditor,
  openVideoExport: WindowService.OpenVideoExport,
  videoExportDraft: WindowService.VideoExportDraft,
  runVideoExport: WindowService.RunVideoExport,
  closeVideoExport: WindowService.CloseVideoExport,
};
