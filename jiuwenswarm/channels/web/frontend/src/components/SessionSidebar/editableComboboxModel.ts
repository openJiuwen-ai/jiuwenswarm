export interface EditableComboboxOption {
  value: string;
  label: string;
}

export function filterEditableComboboxOptions(options: EditableComboboxOption[], query: string, selectedLabel: string): EditableComboboxOption[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  if (!normalizedQuery || normalizedQuery === selectedLabel.trim().toLocaleLowerCase()) {
    return options;
  }
  return options.filter(option => {
    const value = option.value.toLocaleLowerCase();
    const label = option.label.toLocaleLowerCase();
    return value.includes(normalizedQuery) || label.includes(normalizedQuery);
  });
}

export function resolveEditableComboboxCommit(
  options: EditableComboboxOption[],
  input: string,
): string | null {
  const normalizedInput = input.trim();
  if (!normalizedInput) return null;
  const normalizedLookup = normalizedInput.toLocaleLowerCase();
  const exactOption = options.find(option =>
    option.value.trim().toLocaleLowerCase() === normalizedLookup
    || option.label.trim().toLocaleLowerCase() === normalizedLookup
  );
  return exactOption?.value ?? normalizedInput;
}
