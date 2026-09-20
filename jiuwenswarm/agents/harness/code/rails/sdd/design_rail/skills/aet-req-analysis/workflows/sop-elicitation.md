## SOP: Requirements Elicitation (Inversion Pattern)

<guideline>

### Behavior Specification

The fundamental purpose is to **support capability derivation**: by exhaustively reasoning through scenarios, derive the supporting capabilities the system must possess, especially identifying capabilities users may not have considered but are essential for normal system operation or robustness.

- **Happy Path**: Given valid input, the output or state change the system should produce.
- **Alternative Paths**: Different behaviors under different conditions (e.g., VIP user discount). Users easily overlook such scenarios.
- **Critical Errors & Exception Handling**: How the system must react under invalid input, timeouts, or dependency failures.

### Key Specification Design

Based on identified user or system behaviors, analyze affected requirement specifications, paying special attention to blind spots.

- Traverse all behavior paths (main success scenario, alternative branches, exception/failure scenarios).
- For each path, identify whether the following three types of specifications are affected:
  - **Functional requirements** (capabilities that must be supported)
  - **Non-functional requirements** (DFx: availability & reliability, performance, maintainability, etc.)
  - **Breaking changes** (if incompatible changes exist, they must be explicitly marked)

**Core Principle**: DO NOT just list the obvious requirement points. Success criterion = uncover as many **blind spots** as possible — situations the user did not explicitly mention but the system must handle, or details easily missed in a scenario.

### Complexity Assessment

|Dimension|Low|Medium|High|
|-|-|-|-|
|Code volume|<100 lines|100-500 lines|>500 lines|
|Non-functional constraints|Loose targets|Moderate targets|Stringent targets|
|Architectural change|Single module|Cross-module|Core/foundation modules|
|Codebase familiarity|Familiar stack|Moderate familiarity|Unfamiliar/niche stack|

</guideline>

<instruct>

### 1. Initial Understanding

**Completion criterion: All ambiguities are fully resolved.**

#### Step 1: Lightweight Codebase Scan

If the project codebase is accessible, perform a lightweight scan of key components to establish preliminary technical context. Use this context to formulate questions.

Focus on **what** the project and requirements are, not **how** to implement them. Read just enough code to grasp the background and current state.  
DO NOT explore deeply at this stage — prioritize breadth over depth.

#### Step 2: Socratic Dialogue

Based on the ambiguities that currently exist, issue a **single batch** of confirmation questions to the user, including at least two types:

> "我目前的理解是：[1, 2, ...]"
> **Q1**: "这样理解是否正确？"
>
> "目前还不够明确的是：1 [xxx unclear, my understanding is (1a) xxx (1b) xxx]; 2 [...]; ..."
> **Q2**: "请你确认更接近哪一种：[option A] \ [option B] \ ..."

If still leaves gaps, **keep drilling down** with another round of questions. Grill the user until every ambiguity is thoroughly eliminated.

**Completion: DO NOT proceed until all ambiguities resolved and user confirms.**

#### Step 3: Background Confirmation

After clarifying requirements, confirm the background:

> "我识别到的业务背景与动机为: [specific challenges or pain points, business value and success criteria]"
> **Q1**: "背景与动机是否有偏差？"

### 2. Identify Behavior

**Completion: 挖掘出用户难以察觉的场景，确保所有关键潜在情况都被考虑到**

For each identified scenario, specify: Happy Path, Alternative Paths, and Critical Errors & Exception Handling. Issue the following confirmation questions:

> "我识别到的主成功场景为：[Who → under what circumstances → did what → how the system responds → final result]"
> **Q1**: "主成功场景是否准确？"
>
> "我识别到的扩展、备选和异常场景如下：[1, 2, ...]"
> **Q2**: "扩展、备选和异常场景是否准确且全面？若有遗漏，请补充。"
>
> "我理解的需求边界不做：[1, 2, ...]"
> **Q3**: "需求边界是否准确？"

### 3. Key Specification Design

**Completion: functional requirements, non-functional requirements, breaking changes identified; complexity assessed; user confirmed**

Based on the identified user or system behaviors, analyze affected requirement specifications comprehensively, paying special attention to easily overlooked blind spots.

#### Step 1: Traverse All Paths

Traverse all behavior paths (main success scenario, alternative branches, exception/failure scenarios). For each path, identify: Functional requirements, Non-functional requirements, Breaking changes.

Issue the following confirmation questions:

> **Q1**: "预估实现需求难度为：[Low (simplified execution, review, deliverables) / Medium (balanced execution, deliverables, and review) / High (more effort to ensure thorough design)]，是否需要修改？"
>
> **Q2**: "挖掘到的主要功能需求为：[1, 2, ...]，是否有遗漏？"
>
> **Q3** (Effort=Low & NO DFx impact, Skip): "挖掘到的非功能性需求为：[Availability & Reliability: 1, 2, ...; Performance: 3, 4, ...; ...]，是否合理或有遗漏？"

### 4. Inversion Completion Check

**Completion: all conditions below are verified as satisfied**

- No unresolved ambiguities or conflicts remain
- User has confirmed all questions
- Key specification design has been comprehensively considered
- No implementation-related questions have been asked (technology selection, architecture, module partitioning)

</instruct>

<constraint>

- DO NOT read project details: code reading serves only to grasp requirement context, not implementation.
- DO NOT generate any document until inversion completion check passes.
- NEVER ask the user implementation-related questions (technology selection, architecture, module partitioning).

</constraint>
