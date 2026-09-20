### SOP: Design Exploration

<guideline>

- **Trace module internals relentlessly**. DO NOT stop at module interfaces; follow the implementation logic all the way down.
- **Code is the sole source of truth**. If a document contradicts the code, the code wins.
- **Tailor exploration depth to the stage**. Requirement analysis calls for breadth, grasp **only the high‑level system landscape**. Design calls for depth, trace the internals of **every module relevant to the design**.
- **Sub-Agent Output Specs**: To ensure efficiency and maximize information entropy (density), sub-agents must strictly adhere to the following:
  - **Extreme Conciseness**: Prefer `Key: Value` pairs or brief statements over long, complex sentences. List one fact per line. Never use two sentences when one suffices.
  - **Minimal Code Citation**: Avoid outputting >10 lines of code. Provide only essential snippets with exact location indices (`[filename:line_range]`), allowing the caller to review the full code as needed.
  - **Simplified Diagrams**: Strictly prohibit verbose ASCII art. Exclusively use compact syntax like Mermaid.

</guideline>

<instruct>

start by reviewing the feature-level design/implementation relevant to the requested capability.

</instruct>

<constraint>

- DO NOT design on abstract assumptions alone — do not begin designing until you have thoroughly understood the system's implementation details (from entry points and execution paths all the way down to the primitive operations that actually perform the work).
- ALWAYS cap exploration sub‑agents at a maximum of 4. Use the fewest required: 1–4 as needed, and prefer 1 over 2 whenever possible. Avoid spawning unnecessary parallel agents.

</constraint>

<condition>

- IF no codebase (greenfield project), THEN skip existing-system analysis and proceed directly to design.
- IF Effort is Low, THEN direct reading of relevant analysis docs and code is sufficient.
- IF Effort is Medium, THEN delegate subagent exploration on demand.
- IF Effort is High, THEN subagent exploration is mandatory.

</condition>
