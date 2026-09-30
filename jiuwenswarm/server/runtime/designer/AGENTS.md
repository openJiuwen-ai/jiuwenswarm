# Designer code conventions

- Keep chat orchestration, candidate transformation, validation, and persistence in separate functions. Candidate preparation must not write files or start generation.
- Validate external model responses at the boundary. Inside the module, use explicit types and established invariants instead of repeated type checks or silent defaults.
- Preserve stable node IDs when identifying shots. Treat shot numbers and timelines as editable attributes.
- Use deterministic code for structural facts and model output for prose. Do not repair natural-language omissions with case-specific substitutions.
- Keep failures explicit. Avoid silent fallbacks, automatic retry loops, or partial success that hides an unsuccessful edit.
- Prefer small functions, descriptive names, and comments that explain constraints. Keep refactoring and formatting within the changed behavior.
