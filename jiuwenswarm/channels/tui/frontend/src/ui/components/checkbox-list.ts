import chalk from "chalk";
import { matchesKey } from "@mariozechner/pi-tui/dist/keys.js";
import { visibleWidth, wrapTextWithAnsi } from "@mariozechner/pi-tui";
import { padToWidth } from "../rendering/text.js";

/**
 * CheckboxList — pi-tui Component for grouped multi-select with checkbox items.
 *
 * Navigation: ↑/↓ moves focus, Space toggles checked state, Enter confirms, Esc cancels.
 * Group headers are non-selectable separator lines.
 */
export interface CheckboxItem {
  label: string;
  value: string;
  checked: boolean;
  description?: string;
}

export interface CheckboxGroup {
  name: string;
  items: CheckboxItem[];
}

interface FlatItem {
  type: "header" | "item";
  groupIndex: number;
  itemIndex?: number;
  label: string;
  value?: string;
  checked?: boolean;
  description?: string;
}

interface CheckboxTheme {
  header: (text: string) => string;
  checkedMark: string;
  uncheckedMark: string;
  focusMarker: string;
  focus: (text: string) => string;
  dim: (text: string) => string;
  hint: (text: string) => string;
}

const defaultTheme: CheckboxTheme = {
  header: (t) => chalk.bold.cyan(`── ${t} ──`),
  checkedMark: "x",
  uncheckedMark: " ",
  focusMarker: "›",
  focus: (t) => chalk.bold(t),
  dim: (t) => chalk.dim(t),
  hint: (t) => chalk.dim(t),
};

export class CheckboxList {
  private groups: CheckboxGroup[];
  private flatItems: FlatItem[];
  private selectedIndex: number = 0;
  private maxVisible: number;
  private theme: CheckboxTheme;

  onSelect: (selectedValues: string[]) => void = () => {};
  onCancel: () => void = () => {};

  constructor(groups: CheckboxGroup[], maxVisible: number = 10, theme?: CheckboxTheme) {
    this.groups = groups;
    this.maxVisible = maxVisible;
    this.theme = theme ?? defaultTheme;

    // Flatten groups into a flat list of selectable items + header separators
    this.flatItems = [];
    for (let gi = 0; gi < groups.length; gi++) {
      this.flatItems.push({
        type: "header",
        groupIndex: gi,
        label: groups[gi].name,
      });
      for (let ii = 0; ii < groups[gi].items.length; ii++) {
        this.flatItems.push({
          type: "item",
          groupIndex: gi,
          itemIndex: ii,
          label: groups[gi].items[ii].label,
          value: groups[gi].items[ii].value,
          checked: groups[gi].items[ii].checked,
          description: groups[gi].items[ii].description,
        });
      }
    }

    // Set initial focus to first selectable item
    for (let i = 0; i < this.flatItems.length; i++) {
      if (this.flatItems[i].type === "item") {
        this.selectedIndex = i;
        break;
      }
    }
  }

  render(width: number): string[] {
    const lines: string[] = [];

    // Calculate visible range with scrolling
    const selectableIndices = this.flatItems
      .map((_, i) => i)
      .filter((i) => this.flatItems[i].type === "item");

    const totalSelectable = selectableIndices.length;
    const halfVisible = Math.floor(this.maxVisible / 2);

    let startIdx = 0;
    let endIdx = this.flatItems.length;

    if (totalSelectable > this.maxVisible) {
      const currentSelectableIdx = selectableIndices.indexOf(this.selectedIndex);
      const scrollStart = Math.max(0, currentSelectableIdx - halfVisible);
      const scrollEnd = Math.min(totalSelectable, scrollStart + this.maxVisible);

      const firstVisible = selectableIndices[scrollStart];
      const lastVisible = selectableIndices[scrollEnd - 1];

      startIdx = firstVisible;
      endIdx = lastVisible + 1;
    }

    for (let i = startIdx; i < endIdx; i++) {
      const item = this.flatItems[i];

      if (item.type === "header") {
        const headerWidth = Math.max(1, width - 6);
        for (const headerLine of wrapTextWithAnsi(item.label, headerWidth)) {
          lines.push(padToWidth(this.theme.header(headerLine), width));
        }
        continue;
      }

      const isFocused = i === this.selectedIndex;
      const marker = isFocused ? this.theme.focusMarker : " ";
      const checkMark = item.checked
        ? this.theme.checkedMark
        : this.theme.uncheckedMark;

      const prefix = `  ${marker}[${checkMark}] `;
      const continuationPrefix = " ".repeat(visibleWidth(prefix));
      const bodyWidth = Math.max(1, width - visibleWidth(prefix));
      const description = item.description?.replace(/[\r\n]+/g, " ").trim() ?? "";
      const itemLines: string[] = [];

      if (
        description &&
        visibleWidth(item.label) + 2 + visibleWidth(description) <= bodyWidth
      ) {
        itemLines.push(`${prefix}${item.label}  ${description}`);
      } else {
        const labelLines = wrapTextWithAnsi(item.label, bodyWidth);
        for (let lineIndex = 0; lineIndex < labelLines.length; lineIndex++) {
          const linePrefix = lineIndex === 0 ? prefix : continuationPrefix;
          itemLines.push(`${linePrefix}${labelLines[lineIndex]}`);
        }
        if (description) {
          const descriptionPrefix = `${continuationPrefix}  `;
          const descriptionWidth = Math.max(1, width - visibleWidth(descriptionPrefix));
          for (const descriptionLine of wrapTextWithAnsi(description, descriptionWidth)) {
            itemLines.push(`${descriptionPrefix}${descriptionLine}`);
          }
        }
      }

      for (const itemLine of itemLines) {
        const styledLine = isFocused
          ? this.theme.focus(itemLine)
          : !item.checked
            ? this.theme.dim(itemLine)
            : itemLine;
        lines.push(padToWidth(styledLine, width));
      }
    }

    // Scroll indicator
    if (totalSelectable > this.maxVisible) {
      const currentSelectableIdx = selectableIndices.indexOf(this.selectedIndex);
      lines.push(
        padToWidth(
          this.theme.hint(`  (${currentSelectableIdx + 1}/${totalSelectable})`),
          width,
        ),
      );
    }

    // Hint line
    lines.push(
      padToWidth(
        this.theme.hint("↑/↓ 导航 · 空格切换 · Enter 确认 · Esc 取消"),
        width,
      ),
    );

    return lines;
  }

  handleInput(data: string): void {
    if (matchesKey(data, "up") || data === "k") {
      this._moveUp();
    } else if (matchesKey(data, "down") || data === "j") {
      this._moveDown();
    } else if (matchesKey(data, "space")) {
      this._toggleChecked();
    } else if (matchesKey(data, "enter") || matchesKey(data, "return")) {
      this._confirm();
    } else if (matchesKey(data, "escape")) {
      this.onCancel();
    }
  }

  invalidate(): void {
    // No-op (pi-tui Component interface)
  }

  getCheckedValues(): string[] {
    return this.flatItems
      .filter((item) => item.type === "item" && item.checked)
      .map((item) => item.value!);
  }

  private _moveUp(): void {
    for (let i = this.selectedIndex - 1; i >= 0; i--) {
      if (this.flatItems[i].type === "item") {
        this.selectedIndex = i;
        return;
      }
    }
    // Wrap to bottom
    for (let i = this.flatItems.length - 1; i > this.selectedIndex; i--) {
      if (this.flatItems[i].type === "item") {
        this.selectedIndex = i;
        return;
      }
    }
  }

  private _moveDown(): void {
    for (let i = this.selectedIndex + 1; i < this.flatItems.length; i++) {
      if (this.flatItems[i].type === "item") {
        this.selectedIndex = i;
        return;
      }
    }
    // Wrap to top
    for (let i = 0; i < this.selectedIndex; i++) {
      if (this.flatItems[i].type === "item") {
        this.selectedIndex = i;
        return;
      }
    }
  }

  private _toggleChecked(): void {
    const item = this.flatItems[this.selectedIndex];
    if (item.type === "item") {
      item.checked = !item.checked;
      // Also update the backing group data
      if (item.groupIndex !== undefined && item.itemIndex !== undefined) {
        this.groups[item.groupIndex].items[item.itemIndex].checked = item.checked;
      }
    }
  }

  private _confirm(): void {
    this.onSelect(this.getCheckedValues());
  }
}
