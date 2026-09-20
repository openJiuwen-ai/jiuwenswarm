/**
 * Session picker dropdown — shows available research sessions
 * and lets the user switch between them or create a new one.
 * Supports full keyboard navigation (↑/↓ move, Enter select, Esc close).
 */

import { ResearchSession } from "@shared/types";

export class SessionPicker {
  private _el: HTMLElement;
  private _label: HTMLElement;
  private _sessions: ResearchSession[] = [];
  private _activeId: string | null = null;
  private _onSelect: (id: string) => void;
  private _open = false;
  private _focusIndex = 0;

  constructor(
    pickerEl: HTMLElement,
    labelEl: HTMLElement,
    onSelect: (id: string) => void
  ) {
    this._el = pickerEl;
    this._label = labelEl;
    this._onSelect = onSelect;

    labelEl.addEventListener("click", () => this.toggle());
    labelEl.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " " || ev.key === "ArrowDown") {
        ev.preventDefault();
        this.open();
      }
    });
    document.addEventListener("click", (ev) => {
      if (!this._el.contains(ev.target as Node) && ev.target !== labelEl) {
        this.close();
      }
    });
    this._el.addEventListener("keydown", (ev) => {
      if (!this._open) return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        this._move(1);
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        this._move(-1);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        const id = this._sessions[this._focusIndex]?.id;
        if (id) {
          this._onSelect(id);
          this.close();
        }
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        this.close();
      }
    });
  }

  update(sessions: ResearchSession[], activeId: string | null): void {
    this._sessions = sessions;
    this._activeId = activeId;
    const active = sessions.find((s) => s.id === activeId);
    this._label.textContent = active?.title ?? "No session";
    this._label.title = active?.title ?? "";
    if (this._open) this._render();
  }

  toggle(): void {
    if (this._open) {
      this.close();
    } else {
      this.open();
    }
  }

  open(): void {
    this._open = true;
    this._render();
    this._el.classList.add("open");
    this._focusIndex = Math.max(0, this._sessions.findIndex((s) => s.id === this._activeId));
    this._focus();
  }

  close(): void {
    this._open = false;
    this._el.classList.remove("open");
  }

  private _move(dir: 1 | -1): void {
    if (this._sessions.length === 0) return;
    this._focusIndex = (this._focusIndex + dir + this._sessions.length) % this._sessions.length;
    this._focus();
  }

  private _focus(): void {
    const item = this._el.children[this._focusIndex] as HTMLElement | undefined;
    item?.focus();
  }

  private _render(): void {
    this._el.innerHTML = "";
    this._el.setAttribute("role", "listbox");
    this._el.setAttribute("aria-label", "Research sessions");
    for (let i = 0; i < this._sessions.length; i++) {
      const session = this._sessions[i];
      const item = document.createElement("div");
      item.className = "session-item" + (session.id === this._activeId ? " active" : "");
      item.textContent = session.title;
      item.title = `Mode: ${session.mode} | Created: ${new Date(session.createdAt).toLocaleDateString()}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(session.id === this._activeId));
      item.tabIndex = -1;
      item.addEventListener("click", () => {
        this._onSelect(session.id);
        this.close();
      });
      this._el.appendChild(item);
    }
  }
}
