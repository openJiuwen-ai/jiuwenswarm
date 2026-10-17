## SOP: Design

<guideline>

DO NOT start from "How do we implement the requirements?" — start from "What does the existing system look like? What changes does it actually need? Are there alternative capability paths that achieve the same effect?"

</guideline>

<instruct>

### Codebase Grounding

- What are the upstream callers and downstream dependents of every module being touched — does the change introduce new dependency edges, cycles, or layering violations?
- What frameworks, middleware, and infrastructure constraints does the existing stack impose, and do any new runtime dependencies conflict with them?
- What core data models are involved, and what are the version evolution and compatibility strategies?

### Requirements Mapping

For each requirement, answer the following three questions explicitly.
- Which features demand **net-new modules** — and why can't existing modules absorb them?
- Which existing modules **require modification** — and what is the precise scope of change?
- Which existing modules **must remain frozen** — either _protected_ (touched by the change but modification prohibited) or _out-of-scope_ (not involved at all)? State the rationale for each.

### Architecture Design

Before touching any implementation, complete the design across three dimensions: overall architecture, module decomposition, and inter-module interfaces.
- What is the macro-level layering and module composition of the system? How are the responsibility boundaries of each module defined?
- What interface contracts are needed between new modules and existing ones? Should these contracts reuse existing interfaces or introduce new ones — and on what basis?
- How are the data flows and control flows between modules organized? Are there any single-point bottlenecks or excessive coupling?

### Runtime Analysis

Answer two questions: Can the newly added or modified functionality truly enter the runtime? Have all possible paths that can trigger its behavior been explicitly identified, and are there any uncontrolled bypasses?
- Verify the new module's startup dependencies, initialization sequence, and its actual integration point within the existing runtime. Confirm the complete activation chain from process startup to the service-ready state, and identify and eliminate implicit blocking or silent failures.
- Exhaustively enumerate all entry points that can trigger the target behavior, including direct/indirect APIs, background/asynchronous tasks, and equivalent behaviors achieved through lower-level abstractions (such as bypassing public interfaces and directly accessing storage or the network). For each path, clarify parameter determinacy and the earliest feasible interception point, ensuring that no uncontrolled alternative triggering methods exist.

### Design Pattern & DFx Strategy

- Which design patterns are **already present** in the codebase? The new design must align with or consciously diverge from them — and must say which.
- Which **new patterns** are being proposed, and what specific problem in this design justifies their introduction?
- What DFx concerns are in play: **availability, reliability, performance, security, usability/extensibility/testability/maintainability/upgradability/etc.**? Each concern must be named and addressed — not left as a future consideration.

</instruct>

<constraint>

- NEVER generate any document or output any results from this step. Delegate all findings to the subsequent verification stage.

</constraint>
