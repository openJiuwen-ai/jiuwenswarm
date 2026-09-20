/**
 * Privacy disclosure modal — explains what stays local and what is sent to the
 * configured JiuwenSwarm server.
 */

import { t } from "@shared/i18n";

const privacyEl = document.getElementById("privacy")!;
const privacyBody = document.getElementById("privacy-body")!;
const privacyClose = document.getElementById("privacy-close")!;

export function openPrivacy(): void {
  privacyBody.textContent = t("privacy.body");
  privacyEl.classList.add("open");
}

export function closePrivacy(): void {
  privacyEl.classList.remove("open");
}

privacyClose.addEventListener("click", closePrivacy);
