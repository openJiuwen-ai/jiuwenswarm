# SkillPack generation, reuse and distribution

A SkillPack is a composite Skill executed by one Agent. Its root `SKILL.md` describes an ordered workflow over independent ordinary Skills. SwarmSkills and nested SkillPacks are not supported members.

## Enable

The product entry points are off by default. Start with an isolated test Hub:

```sh
export SKILLPACK_PRODUCTIZATION_ENABLED=true
export SKILLPACK_DIRECT_ROUTE_MODE=confirm
export SKILLPACK_HUB_DEPLOYMENT_SCOPE=isolated
export SKILLPACK_HUB_TRANSPORT_PROFILE=legacy_skill_v1
```

Routing accepts `off`, `shadow` (evaluate and log only), or `confirm`. The environment overrides `skillpack.direct_route_mode` in service configuration.

Publishing requires deployment scope `isolated` or `version_gated`. The latter asserts that the deployment already restricts marketplace client versions; the flag does not implement Hub filtering. A legacy Hub reviews and displays the wrapper as an ordinary Skill. Native Hub support is a later release. Use `native_skillpack_v1` only with a compatible Hub. Transport failures never change profiles.

## Use

Ask the Agent to distill a completed multi-Skill workflow. `skillpack_distill` reads persisted real execution evidence and produces a candidate. Installation requires confirmation after inspecting the members and instructions.

Installed Packs appear in My Skills and the existing task Skill picker with a SkillPack label. Member changes affect Pack availability. A complete query match offers a confirmation or a choice of Packs before bypassing Symphony composition. Partial or uncertain matches continue through the existing path. Explicitly picking one Pack confirms the selection; mixed selections require a choice. Existing side-effect permissions remain in force. Failure after member execution does not automatically restart composition.

## Publish and install

Publish from the Pack detail page. One preflight lists every member as reuse, publish first, or blocked. One confirmation starts independent member uploads and review waiting, followed by Pack publication. A failed Pack does not delete successfully published members. Reopening the publishing panel resumes status queries. Changed local content requires a new preflight and confirmation.

The thin ZIP contains the existing `plugin.yaml` wrapper, root instructions and `skillpack.lock.json`. It does not contain member sources. The lock records ordered Hub asset IDs, exact versions and original ZIP SHA-256 digests. Missing members are downloaded independently from the same Hub.

Installation first displays a plan. Reuse requires matching provenance, version, archive digest and unchanged local content. Name conflicts and local edits block installation. All packages are validated before committing the inventory and `.archive` snapshots. Failures restore directories and registration state; startup recovers interrupted commits before loading Skills.

Uninstalling a Pack keeps its members. Uninstalling a Skill referenced by an installed Pack is blocked and lists its referring Packs.

## Operations

Plans and transaction journals live in the current skills directory under `.skillpack/`. Per-Skill Hub provenance is stored in `.archive/hub-provenance.json`; neither is published. Disabling the product switch stops new distillation, publication and Pack installation. Direct routing has a separate switch.

Deploy the matching agent-core and JiuwenSwarm changes together. This branch temporarily pins the companion Core PR commit in its fork. Before merging, switch to the corresponding official repository commit and refresh the lockfile. Local simulated tests do not establish real Hub review, real model or desktop platform acceptance; record those separately.

## UI examples

An actual Pack installed through distillation in an isolated WorkSwarm environment, using synthetic standup data:

![Installed SkillPack and independent members](../images/skillpack/my-skills.png)

The real installation component with a simulated plan, showing reuse and download actions; this is not a live Hub screenshot:

![Installation plan component preview](../images/skillpack/install-plan-preview.png)
