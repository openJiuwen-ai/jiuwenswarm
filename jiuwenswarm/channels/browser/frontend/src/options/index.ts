/**
 * Options page entry point.
 * Loads settings from storage, binds form fields, saves on submit.
 */

import { loadSettings, saveSettings } from "@shared/storage";
import { createLogger } from "@shared/logger";
import { initI18n, applyStaticI18n, t } from "@shared/i18n";

const log = createLogger("options");

const hostInput = document.getElementById("host") as HTMLInputElement;
const portInput = document.getElementById("port") as HTMLInputElement;
const autoExtractCheck = document.getElementById("auto-extract") as HTMLInputElement;
const autoSummarizeCheck = document.getElementById("auto-summarize") as HTMLInputElement;
const saveBtn = document.getElementById("save-btn")!;
const statusMsg = document.getElementById("status-msg")!;

initI18n();
applyStaticI18n();

async function load(): Promise<void> {
  const settings = await loadSettings();
  hostInput.value = settings.host;
  portInput.value = String(settings.port);
  autoExtractCheck.checked = settings.autoExtract;
  autoSummarizeCheck.checked = settings.autoSummarizeOnPin;
  log.debug("settings loaded", settings);
}

saveBtn.addEventListener("click", async () => {
  const port = parseInt(portInput.value, 10);
  if (isNaN(port) || port < 1 || port > 65535) {
    alert(t("options.portError"));
    return;
  }
  await saveSettings({
    host: hostInput.value.trim() || "127.0.0.1",
    port,
    autoExtract: autoExtractCheck.checked,
    autoSummarizeOnPin: autoSummarizeCheck.checked,
  });
  statusMsg.classList.add("visible");
  setTimeout(() => statusMsg.classList.remove("visible"), 2000);
  log.info("settings saved");
});

load().catch((e) => log.error("failed to load settings", e));
