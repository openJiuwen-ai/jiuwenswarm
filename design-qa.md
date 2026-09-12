# Expert collaboration graph design QA

## Evidence and normalization

- Reference: `/var/folders/ww/t29y04_s14vb4bks9nrb3rmh0000gn/T/codex-clipboard-2cd4f995-0513-4cd7-bb6a-0d80100cbd3b.png`, 2640 × 1072 PNG. It shows Symphony's Skill graph with counters, a left control/list rail, a relationship canvas, threshold control, zoom controls, and selected-node emphasis.
- Implementation baseline: `/Users/wujianyu/PycharmProjects/jiuwenswarm_xiaoyi_expert_graph_runtime/validation-artifacts/design-qa/expert-graph-implementation.jpg`, 1280 × 720 JPEG, JFIF density 1 × 1.
- Final implementation: `/Users/wujianyu/PycharmProjects/jiuwenswarm_xiaoyi_expert_graph_runtime/validation-artifacts/design-qa/expert-graph-interaction-final.png`, 1280 × 720 browser capture. The screenshot bytes are JPEG/JFIF density 1 × 1 even though the browser helper preserved the requested `.png` suffix.
- Full-frame comparison: `/Users/wujianyu/PycharmProjects/jiuwenswarm_xiaoyi_expert_graph_runtime/validation-artifacts/design-qa/expert-graph-reference-comparison.png`, 2560 × 760.
- Focused graph-region comparison: `/Users/wujianyu/PycharmProjects/jiuwenswarm_xiaoyi_expert_graph_runtime/validation-artifacts/design-qa/expert-graph-focused-comparison.png`, 1960 × 570.
- Test state: Xiaoyi Work `专家 → 协作图谱`, 39 experts, 367 relationships, 12 visible team candidates, 50% edge threshold, selected `PPT大纲导演`.

The supplied reference and implementation use different products, datasets, viewports, and aspect ratios: 93 Skills / 470 relations versus 39 Experts / 367 relations. They are therefore compared directionally at the same visible-state level, not claimed as pixel-identical. The combined files put the reference and implementation in one image before visual judgement; the focused comparison removes most surrounding chrome so the information architecture and graph treatment can be compared at a similar visual scale.

## Fidelity review

### Layout

The implementation follows the reference's core information model: graph summary counters above, a left control/list rail, an interactive relationship canvas in the centre, and selected-node evidence beside the graph. Threshold and zoom controls remain adjacent to the graph. Xiaoyi Work keeps an explicit right-side inspector because expert source, type, reuse status, original Skills, and in/out counts are needed for product verification; this narrows the canvas but avoids a second navigation step.

The first implementation allowed the 39-item expert list to determine the CSS Grid row's min-content height. The workspace grew to about 1755 px and `fitView` centred the graph below the initial viewport, so the canvas appeared empty. The final CSS bounds desktop graph height to 540–820 px, uses `minmax(0, 1fr)` for the shared row and control column, and makes the expert list scroll internally. At 901–1050 px it uses an explicit 540 px row; at 900 px and below the controls/details return to content-driven rows while the canvas retains 540 px.

No pre-fix screenshot is presented as evidence. The defect is supported by measured DOM/CSS height, the bounded-layout code change, and the relationship-layout regression tests.

### Typography

Heading hierarchy, counter emphasis, labels, helper copy, and inspector terms are visually consistent with the existing Xiaoyi Work typography. The design does not copy Symphony's font metrics literally; it preserves the host product's type scale and weights while matching Symphony's hierarchy and density.

### Colour and state

The implementation uses Xiaoyi Work semantic foreground, border, surface, primary, muted, and warning tokens. Selected and directly related nodes receive stronger semantic primary emphasis; unrelated nodes recede. Relationship legends distinguish `可交接`, `需适配`, and `能力互补` without introducing hard-coded business colours.

### Assets and controls

Page, graph, search, zoom, and rebuild controls use the product's existing icon library and buttons. No emoji, handcrafted SVG, CSS-art icon, or placeholder asset was introduced. The graph itself remains a real interactive canvas rather than a static mock.

### Copy

Reference concepts were translated to experts rather than copied blindly: `专家节点`, `协作关系`, `专家团候选`, `原始 Skills`, and reuse status describe the actual Xiaoyi Work domain. The explanatory copy makes the runtime rule explicit: the graph finds candidate combinations, while the leader dynamically selects members for each Query.

## Interaction QA

Verified in the live 1280 × 720 environment:

- Initial build completed and rendered 39 nodes / 367 relations / 12 visible candidates.
- Searching `PPT大纲导演` reduced the visible graph to the matching expert and directly connected neighbours.
- Selecting `PPT大纲导演` from the list focused the graph node and updated the right inspector to the correct description, `humanize-ppt` Skill, reuse status, and input/output counts.
- Clearing search restored 39 nodes / 367 relations.
- `适配图谱视图` returned the relationship canvas to a readable overview.
- Zoom in, zoom out, threshold filtering, node selection, canvas dragging, and rebuild remain reachable; their code paths are also covered by the targeted frontend tests.
- No visible error banner, modal, broken control, overlap, or cropped primary action appeared after the above interaction sequence.

The in-app browser interface used for this QA does not expose a historical page-console buffer. Its own `ab.chatgpt.com` Statsig timeout messages are emitted by the browser-control client, not by `localhost:18183`, and are not counted as Xiaoyi Work runtime errors. This report therefore claims visible-state and interaction QA, not a historical console-log audit.

## Develop migration boundary

The expert collaboration graph is a Symphony-style capability network built for Xiaoyi Work; it is distinct from develop's runtime Swarmflow graph. The expanded expert-team execution panel separately migrates develop `ExpandedPanel`, `ExpandedPanelTabs`, `SwarmflowTreeView`, `SwarmflowGraphView`, `AgentDetailModal`, workflow types, and fullscreen behaviour from `upstream/develop@029c76a64d4b2c754ae29aa5e4e8985f80d5673f`.

Beta3 does not expose develop's native `workflowRuns`, phase/agent lazy-load RPCs, workflow budget state, or pause/resume/stop contract. Current-turn beta3 task, event, and execution evidence is therefore adapted into a read-only recovered `WorkflowRun`; details are supplied eagerly, unsupported controls are not presented as live operations, and waiting-for-human uses beta3's single pending-question state. This validates the migrated visualisation and browsing surface, not develop's unavailable workflow-control data plane.

Only evidence timestamped at or after the latest user turn contributes invoked members to the adapted run. If no dispatch evidence exists, the panel keeps only the recognised leader instead of presenting the whole configured roster as executed.

final result: passed
