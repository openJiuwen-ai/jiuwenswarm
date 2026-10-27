# Team Organization Expert and Summary Team Configuration

This document describes AgentGroup integration with Team Organization and final delivery in `HIERARCHICAL` and `SUMMARY_TEAM` modes. Dependency installation, the experimental switch, and runtime configuration are documented in the [User Guide](TeamOrganization.md#2-setup).

For a first test, use Example 1 in the User Guide: install the Business Planning, Technology Insight, and Software Architecture groups, then enter the Owner Team from Business Planning. No custom templates are needed. Sections 1.1 and 1.2 below provide manual setup materials when suitable templates are unavailable; section 1.3 explains Owner entry paths and capability matching. Startup commands, prompts, and acceptance checks remain in the User Guide.

## 1. AgentGroup discovery

JiuwenSwarm discovers templates through the existing installation registry and package resolver. Marketplace resource packages are installed as built-in packages. A package is discoverable only when it is registered as installed, has a valid `manifest.json`, `package_type` is `agent_group`, the manifest name matches the safe single-component directory name, no other source contains the same name, and the AgentGroup bundle passes validation.

Scanning returns template descriptors only; it does not create Teams. The launcher assembles and activates an instance when `org_create_and_invite_expert_team` is called.

Direct messages to a running expert Team use the same `interact` channel as ordinary Teams. Acceptance means the input was delivered to the Leader runtime, not that its task has completed. Replies continue through the existing Team stream and are saved in that Team's conversation history. A paused expert still resumes through the existing execution path. Organization-internal task and Leader-message scheduling is unchanged.

### 1.1 Create experts manually in Web

Initialize the workspace, configure models, and start Web as described in the [User Guide](TeamOrganization.md). Prefer suitable installed marketplace groups. When a template is missing, open Expert Management and choose **Manual Creation → Create Expert**. The next section lists Leaders and teammates for the investment-screening example. That example only requires legal, technical, and market groups; a financial group is optional.

For each expert, use the name and responsibility from the tables below for its name and capability description. Adapt this shared persona by replacing the placeholders:

```text
You are [expert name]. Your responsibility is [responsibility].
Follow the current user's limits on public sources, length, and deliverables.
Use only material actually accessed; distinguish confirmed facts, inferences, and items to verify.
Do not invent revenue, valuation, customer counts, financing terms, interviews, or security conclusions.
As a teammate, accept assignments from your Leader, submit source-backed results, and wait when no new work is assigned.
As a Leader, coordinate your teammates, check evidence, and submit a formal, reviewable Team result.
```

Select skills and connectors only when needed, and ensure the runtime can access the required webpages and code files. Confirm creation and installation under **My** before selecting experts for a group. Do not use an external standalone `AGENT.md` template directly as an ordinary teammate; manually created experts receive the required files from the system.

### 1.2 Create expert groups manually in Web

After creating and installing the experts, choose **Create Expert Group**, fill in its name, capability description, and group introduction, select a Leader and at least one teammate, and add domain tags. The Leader cannot also be a teammate. Successful creation generates and installs the package; no package-generation script or handwritten installation registry is required. In the investment-screening example, the ordinary Owner handles investment and finance; the financial template below is optional.

| Group | Leader | Teammates |
|-------|--------|-----------|
| Investment and Finance (optional) | Technology Investment Leader | Financial Modeling Analyst; Transaction Structure Analyst |
| Legal and Compliance | Technology Transaction Lawyer | Open-source and IP Lawyer; Data Privacy and AI Compliance Lawyer |
| Technical Due Diligence and Coding | CTO Due Diligence Leader | Rust Coding Auditor; AI Editor Architect; Software Supply-chain and Security Engineer |
| Market and Commercial | Developer-tools Market Leader | Developer Customer Researcher; Competition and GTM Analyst |

Use the following responsibilities as capability descriptions. For personas, specify the name and responsibility, require evidence from accessed sources, mark missing information as unverified, and require reviewable results within the user's source and length limits.

| Expert | Responsibility |
|--------|----------------|
| Technology Investment Leader | Coordinate domains, review results, and deliver the investment recommendation; retain responsibility for the root and pass all required accepted results to independent aggregation before closing it |
| Financial Modeling Analyst | Identify investment premises and required financial documents without inventing financial data |
| Transaction Structure Analyst | Identify transaction conditions and next-round checks without inventing financing terms |
| Technology Transaction Lawyer | Coordinate legal analysis and submit a short, publicly sourced opinion, without presenting formal legal advice |
| Open-source and IP Lawyer | Verify official license text and intellectual-property risks |
| Data Privacy and AI Compliance Lawyer | Assess public privacy and AI compliance information; mark missing contracts and policies for verification |
| CTO Due Diligence Leader | Coordinate technical analysis and separate engineering facts from unverified risks |
| Rust Coding Auditor | Inspect README, license files, and Cargo.toml, citing file paths |
| AI Editor Architect | Identify architectural and AI-function facts and uncertainty from public files |
| Software Supply-chain and Security Engineer | Identify dependency and license risks without claiming a completed security audit |
| Developer-tools Market Leader | Coordinate market analysis and submit product opportunities and commercial uncertainty |
| Developer Customer Researcher | Identify target users and needs from product descriptions without inventing interviews |
| Competition and GTM Analyst | Identify commercialization assumptions and checks without expanding competitor research |

Use the following group responsibilities for capability descriptions. Repeat them in group introductions and add instructions to respect source and length limits, claim only matching work, save and submit results, review completion notifications, aggregate all required accepted results, and display the final content without repeated polling or waiting-only replies.

| Group | Responsibility | Suggested custom domain tags |
|-------|----------------|------------------------------|
| Investment and Finance (optional) | Identify investment premises, required financial documents, and transaction risks from public company information, without inventing valuation or revenue | `finance`, `valuation`, `transaction` |
| Legal and Compliance | Check open-source licenses, IP, and privacy risks; separate license facts from unverified issues | `legal`, `compliance`, `privacy`, `ip`, `open_source_license` |
| Technical Due Diligence and Coding | Inspect key public repository files and provide file-backed engineering findings without a full audit or build | `coding`, `rust`, `technical_due_diligence`, `security`, `ai_platform` |
| Market and Commercial | Assess user needs and business opportunities only from public product information, without inventing customers, revenue, or competitor financing | `market`, `customer_research`, `go_to_market`, `developer_tools` |

Add each tag separately rather than entering a comma-separated list as one tag. Skills and recommended questions are optional; this example requires no additional skills or connectors. Configure models and basic webpage access in the runtime. Summary Team is created on demand and needs no manual template.

### 1.3 Marketplace installation, tags, and capabilities

Installation prepares a template; it does not start a Team. There are two Owner entry paths:

- Click **Use** on an installed group to enter a new Team session with that template selected. Its first message builds the runtime instance.
- Create a new task session, select Team mode, and leave the template unselected to build an ordinary Team from the default configuration.

In either case, the Leader becomes Organization Owner after creating the Organization. Other groups only need installation; the Owner creates and invites instances within the current Organization and session. It does not take over Teams running in other sessions. Organization ownership and root-task responsibility are separate; the root still needs an explicit claim.

Capability matching follows these rules:

- Explicit `capabilities` takes priority, including an explicitly empty list.
- Without that field, template `tags` supply capabilities. Tag IDs take priority; legacy localized tags use Chinese text before English, and string tags use their text.
- New Web-created groups save stable preset tag IDs; custom tag text becomes its ID. These IDs do not change with interface language.
- Matching uses actual catalog capability values, without translation or synonym matching. Display names, descriptions, and marketplace `canUse` permissions are not domain capabilities. Task tools continue to use `required_capabilities`.

Manually created groups normally live under `.agent_teams/agent_groups/local/<package-id>/` in the user workspace; bundled marketplace groups install under `built_in/<package-id>/`. The generated package ID need not match the display name. Use the active `JIUWENSWARM_DATA_DIR` when configured. Existing Teams retain their assembled template snapshot; use a new session after editing a template or its source experts.

## 2. Expert Team configuration inheritance

Expert Teams take Leader and member definitions, prompts, skills, and capabilities from the AgentGroup, while inheriting model routing, storage and shared `TeamDatabase`, transport, spawn mode, workspace, system-operation support, session, and Organization context from the current Team configuration. Multiple instances can use the same template. Read each unique runtime `team_id` from `org_view_organization`; a package name is not a runtime Team ID.

## 3. Aggregation modes

The user can explicitly request `HIERARCHICAL` or `SUMMARY_TEAM` in the task prompt. When no mode is specified, the Agent responsible for the root task can decide autonomously from task requirements and each Team's actual delivery scope. Honor an explicit user choice within runtime constraints. In either case, the root's responsible Leader sets the mode through the tool.

After claiming the root and before decomposition, its responsible Leader explicitly selects the mode with `org_update_task(action='set_aggregation_mode', aggregation_mode=...)`. The choice is persisted on the root. The current runtime does not allow switching to a different mode after selection; do not start decomposition and then change the mode based on workload.

### 3.1 Differences and selection

| Aspect | `HIERARCHICAL` | `SUMMARY_TEAM` |
|--------|----------------|----------------|
| Delivery relationship | One specialist Team owns the complete root deliverable and its final output itself satisfies the root; other Teams provide inputs, components, or validation | Teams provide parallel independent domain conclusions, with no specialist Team owning the complete root deliverable; the conclusions require collection, organization, or synthesis |
| Integration | Upstream Leaders review and integrate results along the task tree; the root's responsible Team handles final delivery | The root's responsible Team reviews source results, then an independent Summary Team aggregates them |
| Summary Team execution | No Summary Execution or independent Summary Team is started for this root | Explicitly create a Summary Execution, starting the Organization's shared Summary Team when needed |
| Root completion | The root's responsible Leader integrates accepted results and calls `org_update_task(action='complete')` | Summary Team formally submits through `org_summary_complete`; completion of its Summary Task also completes the root, which the responsible Leader cannot complete directly |

When the user has not specified a mode, the Agent selects it from root requirements and actual Team delivery scopes, not Team count. When one specialist Team's final output already satisfies the complete root, choose `HIERARCHICAL` even if the other Teams' domains are orthogonal. When no such Team exists and parallel domain conclusions require synthesis, choose `SUMMARY_TEAM`.

Do not choose `HIERARCHICAL` because the output is short, the task is small, conflicts are absent, time or overhead can be reduced, collection is simple, or the Owner can compile the results. Nominal Owner aggregation is not complete specialist delivery; do not invent a final integrator.

### 3.2 HIERARCHICAL: Integrate along the task tree

The root's responsible Team coordinates review and final delivery. An invited specialist Team can do the main delivery work; the Owner need not perform every specialist task itself. After downstream completion notifications, upstream Leaders inspect and explicitly accept results. Handle revisions through the existing task relationships without skipping unfinished or unaccepted children.

Once all required children are completed and accepted, the root's responsible Leader integrates existing artifacts, components, or validation findings, checks the root's output requirements, and submits the root result. This mode does not call `org_create_summary_execution` or generate an additional independent Summary Team report in place of an existing deliverable.

### 3.3 SUMMARY_TEAM: Aggregate with an independent Team

The fixed Summary Team contains:

| Member | Responsibility |
|--------|----------------|
| `summary-leader` | Validates sources, coordinates drafting, and submits the result |
| `source-integrator` | Creates a source-attributed outline from the read-only snapshot |
| `delivery-drafter` | Produces the user-facing deliverable |

It uses the configured default Team models but does not copy user Team members, tools, or template prompts. Transport and spawning use `inprocess`; members use `build_mode`. Intermediate work stays in its Team workspace; final artifacts belong under the Organization `summary/` directory. One persistent Summary Team is lazily created per Organization and reused across root tasks; finishing one summary does not stop the shared Team.

Summary inputs include `root_task` (ID, title, description and output specification) alongside the existing bound sources. Root and Summary Task requirements determine format and acceptance; factual conclusions still come only from bound sources. Preserve disagreements, limitations and accepted artifact locations instead of always producing another report. To publish an existing artifact as the final file, copy it unchanged into `summary/`; `result_uri` must still point there, with the actual `result_type`.

`WAITING_SOURCES` means aggregation has not started. Required sources must be both `COMPLETED` and `ACCEPTED`, including contributions executed by the Owner Team itself. The creation tool returns the current task status and `blocked_sources` while waiting; the Owner should address unfinished work or pending reviews before waiting for delivery. Accepted sources continue to trigger the existing activation path.

Summary delivery follows these steps:

1. Claim the root, set `SUMMARY_TEAM`, and call `org_create_summary_execution` to create a Summary Execution bound to source children. A prompt naming the mode alone does not create the execution.
2. Complete and explicitly accept the required sources. A completed source with a pending review still blocks aggregation.
3. Summary Team reads the bound snapshot, prepares the outline and draft without new domain research, and resolves its internal work. After checking the deliverable, abstract, and path, `summary-leader` formally submits through `org_summary_complete`. A draft file or a reply saying "complete" is insufficient.
4. Formal Summary Task completion also completes the root. Its responsible Team receives the result and handles user-facing delivery. In the User Guide examples, the Owner displays the report in its conversation without directly calling the root completion operation again.

Summary roles inherit finite `max_iterations` budgets from the corresponding default Team roles; invalid budgets fall back to 200. Exhausting the budget or reporting an execution error does not complete the summary. Check execution state, existing drafts, and error logs rather than only increasing the budget.

### 3.4 Task-state rules shared by both modes

Both modes require explicit acceptance of child results. For an Organization child assigned to its own Team, the Leader should call `org_update_task(action='start')` before execution or internal dispatch. When teammates finish, the Leader checks their results and promptly completes the corresponding Organization child; its creator Team still reviews it explicitly. Internal task completion does not automatically complete or accept the Organization child.

## 4. Lifecycle and troubleshooting

Chat messages, timeline headers and tool groups resolve the Leader identity within the selected Team's conversation. Without a saved identity, they use that Team's roster or display name. Runtime Teams such as Summary do not inherit the Owner's name or avatar, and this display resolution does not change AgentGroup bindings.

Task completion and Web execution status have separate lifecycles. When a background Organization turn ends, the Web client closes that Team's current reasoning and streaming output without ending other Teams' work. A passive `attach_only` subscription does not keep the session busy; real execution and requests waiting for user input still do. Check Summary Execution and task states to confirm completion, rather than treating an intermediate `chat.final` segment as the end of the turn.

New expert Teams wait paused until Organization events wake them. If joining fails after startup, the launcher stops the temporary Team. Summary Team lifecycle and Summary Execution state are persisted separately. With the experimental switch enabled, the same session, persistent storage, and reactivated Teams, process restart recovery can restore the shared instance and re-evaluate source readiness.

Dissolving an Organization stops temporary expert and shared Summary Teams while retaining the Owner for chat or another Organization. Independently existing invited Teams are only unbound. The Web selector returns to the Owner and removes stopped temporary Teams. Dissolution preserves installed templates, saved messages, and written artifacts, but deletes the Organization's tasks, reviews, and summary-execution records.

For diagnosis, check catalog and launcher logs, Organization progress events, runtime state, and whether participating Teams share the same database instance.

## Importing packages

Imports validate skills before saving or installing the package. String entries in `skills` must be skill names, such as `"docx-pro"`; mappings use package-relative directories, such as `{"dir": "skills/docx-pro", "mode": "all"}`. Absolute POSIX and Windows paths are rejected, including Windows paths uploaded to a non-Windows server. Referenced skill directories must remain inside the package and contain `SKILL.md`.
