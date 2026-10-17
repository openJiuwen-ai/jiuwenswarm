/**
 * Form assistance — fills form fields on instruction from the agent.
 *
 * The agent tool `fill_form` sends a `fill_form` message with a map of
 * label-or-id → value pairs. This module resolves the fields and fills them.
 */

import { MSG } from "@shared/constants";
import { FillFormMsg } from "@shared/types";

export function startFormAssist(): void {
  chrome.runtime.onMessage.addListener((msg: FillFormMsg | { action: string }) => {
    if (msg.action === MSG.FILL_FORM) {
      fillForm((msg as FillFormMsg).fields);
    }
    return false;
  });
}

function fillForm(fields: Record<string, string>): void {
  for (const [labelOrId, value] of Object.entries(fields)) {
    const input = findInput(labelOrId);
    if (!input) continue;

    // Trigger React / Vue synthetic events by setting native value property
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value"
    )?.set;
    if (nativeInputValueSetter) {
      nativeInputValueSetter.call(input, value);
    } else {
      input.value = value;
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
}

function findInput(labelOrId: string): HTMLInputElement | HTMLTextAreaElement | null {
  // Try by id / name first
  const byId = document.getElementById(labelOrId) as HTMLInputElement | null;
  if (byId && (byId.tagName === "INPUT" || byId.tagName === "TEXTAREA")) return byId;

  const byName = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(
    `input[name="${CSS.escape(labelOrId)}"], textarea[name="${CSS.escape(labelOrId)}"]`
  );
  if (byName) return byName;

  // Try matching label text
  for (const label of document.querySelectorAll<HTMLLabelElement>("label")) {
    if (label.innerText.trim().toLowerCase() === labelOrId.toLowerCase()) {
      const forId = label.getAttribute("for");
      if (forId) {
        const el = document.getElementById(forId) as HTMLInputElement | null;
        if (el) return el;
      }
      // Label wrapping the input
      const wrapped = label.querySelector<HTMLInputElement | HTMLTextAreaElement>(
        "input, textarea"
      );
      if (wrapped) return wrapped;
    }
  }

  return null;
}
