/**
 * First-run tour — a short overlay that explains the pin → ask → act loop.
 * Shown once on first open; replayable from the ⋯ menu.
 */

import { t } from "@shared/i18n";
import { hasSeenTour, markTourSeen } from "@shared/storage";

const tourEl = document.getElementById("tour")!;
const tourTitle = document.getElementById("tour-title")!;
const tourBody = document.getElementById("tour-body")!;
const tourNext = document.getElementById("tour-next") as HTMLButtonElement;
const tourPrev = document.getElementById("tour-prev") as HTMLButtonElement;
const tourSkip = document.getElementById("tour-skip") as HTMLButtonElement;
const tourDots = document.getElementById("tour-dots")!;

const TOUR_STEPS = [
  { title: t("tour.1.title"), body: t("tour.1.body") },
  { title: t("tour.2.title"), body: t("tour.2.body") },
  { title: t("tour.3.title"), body: t("tour.3.body") },
];

let _tourStep = 0;

function renderTourStep(): void {
  const step = TOUR_STEPS[_tourStep];
  tourTitle.textContent = step.title;
  tourBody.textContent = step.body;
  tourNext.textContent = _tourStep === TOUR_STEPS.length - 1 ? t("tour.gotit") : t("tour.next");
  tourPrev.style.visibility = _tourStep === 0 ? "hidden" : "visible";
  tourDots.innerHTML = TOUR_STEPS.map(
    (_, i) => `<span class="dot${i === _tourStep ? " active" : ""}"></span>`
  ).join("");
}

export function openTour(): void {
  _tourStep = 0;
  renderTourStep();
  tourEl.classList.add("open");
}

export function closeTour(): void {
  tourEl.classList.remove("open");
}

export async function maybeShowTour(): Promise<void> {
  if (await hasSeenTour()) return;
  await markTourSeen();
  openTour();
}

tourNext.addEventListener("click", () => {
  if (_tourStep < TOUR_STEPS.length - 1) {
    _tourStep += 1;
    renderTourStep();
  } else {
    closeTour();
  }
});

tourPrev.addEventListener("click", () => {
  if (_tourStep > 0) {
    _tourStep -= 1;
    renderTourStep();
  }
});

tourSkip.addEventListener("click", () => {
  markTourSeen().catch(() => {});
  closeTour();
});
