import { Service as PreferencesService } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/preferences/index.js";
import type { State as PreferencesState } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/preferences/models.js";
import type { Custom as ProxyCustom, Status as ProxyStatus } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/netproxy/models.js";

export type { PreferencesState, ProxyCustom, ProxyStatus };

export const desktopPreferences = {
  get: PreferencesService.Get,
  save: PreferencesService.Save,
  proxyStatus: PreferencesService.ProxyStatus,
};
