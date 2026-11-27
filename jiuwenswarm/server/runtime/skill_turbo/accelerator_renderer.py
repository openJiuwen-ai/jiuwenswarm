"""Request-scoped accelerator disclosure, rendered only from parsed declarations."""

from __future__ import annotations

import json

START = '<skill_accelerators>'
END = '</skill_accelerators>'


def render_catalog(accelerators, *, language='cn', line_budget=96, max_per_skill=8):
    lines = []
    for skill, specs in sorted(accelerators.items()):
        for spec in sorted(specs, key=lambda item: item.order)[:max_per_skill]:
            review = ' [review]' if spec.hitl.kind == 'review' else ''
            prefix = f'{skill}/{spec.id} ({spec.order}){review}: '
            text = prefix + spec.summary + ' [' + ', '.join(spec.covers.steps) + ']'
            lines.append(text if len(text) <= line_budget else text[:line_budget - 1] + '…')
        if len(specs) > max_per_skill:
            text = f'{skill}: 加载技能查看其余段' if language == 'cn' else f'{skill}: load skill for more segments'
            lines.append(text[:line_budget])
    return '\n'.join(lines)


def _single_guide(language='cn'):
    if language == 'en':
        return '''For skills with declared segments, choose an applicable segment from visible capabilities, prerequisites and progress. No explicit acceleration keyword is required; honor explicit no-acceleration, standard-flow or other-skill requests. Load the skill for full schemas, then use skill_acceleration_exec(query=faithful_user_goal, skill=exact_name, accelerator=exact_id, params=declared_arguments). A skill-only call returns candidates; explicitly select a segment. Standard tools and segments may alternate; skip completed steps and follow next_steps. No batch or force parameter is supported.
PPT targets a complete deck. Outline/review/ok/partial is not final delivery. Only trusted delivery_confirmed with no remaining work permits completion. Never mark the todo complete on intermediate success. Use SVG reference segments only within their declared scope, not as a native/template_fill replacement. Edit locally with ordinary tools; generate must not regenerate local edits. Export may consume a valid complete single-page collection, including ordinary-tool output or edited pages, but not Live bindings, unsaved objects or whole PPTX files.
Reuse the original task/todo across segments; changing query or fresh_start does not create a new task. For a new independent deliverable, first successfully create a new outer todo, then invoke serially. Do not copy paths to forge producer provenance or bypass prerequisites. Completed segments return already_done without rerunning. Retry only unfinished work lawfully. Resume the original card/identity/inputs; definition changes invalidate its checkpoint. A can_pause=false failure is not a waiting card. Cancellation stops execution. Unified tool permission does not remove business review.
Missing pages or merge failure means failed, no partial automatic delivery. Unknown send outcome means unconfirmed: preserve files and evidence; never automatically resend through fallback/retry/resume. Historical delivery/already_done is not a new send or summary. persisted=false warns about storage; it does not negate a trusted send. Cached last-good declarations are display only; execution revalidates current code and declarations.'''
    return '''有阶段清单的技能：根据可见能力、适用场景、前提和进度选择是否使用段，无需用户明确说出“加速”；遵从禁用加速、普通流程或指定其他技能的要求，不扩写用户意图。加载技能查看完整schema，再调用 skill_acceleration_exec(query=忠实需求, skill=准确技能名, accelerator=准确段id, params=声明参数)。仅传skill返回候选，仍须显式选段。普通工具与段可交替，跳过已完成步骤，按next_steps继续。不支持批量或force参数。
PPT目标是完整演示文稿，outline、审阅、ok/partial均不代表最终交付；只有可信delivery_confirmed且无剩余工作才可完成请求，不能凭中间成功完成todo。SVG参考四段不是native/template_fill的无差别替代。局部编辑用普通工具，不交给generate重新生成；完整合法单页集合（含普通工具生成或合法改稿）可export，Live绑定态、未保存对象、整本PPTX不可冒充单页集合。
各段复用原任务/todo；改query或fresh_start不能创建新任务。新增独立交付物须先成功创建新外层todo，再串行调用；禁止并行建todo与调用。不可复制路径伪造producer或绕过前提，合法改稿由下一段核验。已完成段already_done跳过，无强制重跑；未完成段合法重试与原卡恢复保留。恢复使用原身份/inputs，声明或源码变化使原断点失效；can_pause=false已failed不等待卡片，取消则停止。统一工具授权仍保留业务审阅。
缺页、合并失败为failed，不自动发送部分成果。发送结果不明则交付未确认，保留文件和依据，普通回退、后续调用、恢复均不可自动重发；明确要求重发走既有发送工具授权。历史交付、already_done不产生新发送或总结。persisted=false仅存储告警，不否定可信发送。last-good清单仅供展示，执行须重新核验当前声明与源码。'''


def render_guide(language='cn', *, batch_enabled=True, max_batch_steps=8):
    text = _single_guide(language)
    if not batch_enabled:
        return text
    if language == 'en':
        text = text.replace('No batch or force parameter is supported.', '')
        text = text.replace('Completed segments return already_done without rerunning.', 'Completed segments default to already_done.')
        return text + f"""
When subsequent steps and how to obtain their inputs are clear, prefer one call covering multiple stages. Stop at the corresponding boundary when the user limits the scope, requests an intermediate check, or needs results before deciding what follows. A request to do a step first may itself limit scope; the user need not say single stage.
Use skill_acceleration_exec(query=faithful_goal, skill=exact_name, accelerator=start_id, params=start_arguments). Omitted/null auto_batch defaults to following declared batch.auto_next successors; true explicitly requests it, false executes ONLY the specified stage. No successor means only that stage. Never infer successors from order. params apply ONLY to the starting step. Return to the outer agent for inspection or edits at each chain end. Use 1–{min(max_batch_steps, 8)} stages per batch.
For a five-slide PPT, start with prep by default. For preparation only (including do preparation first), use prep with auto_batch=false. For outline approval before generation, enable outline_review and stop at the outline boundary. For pages without export, use generate with auto_batch=false. For explicitly selected stages use accelerators. Omitted/null mode preserves explicit accelerators lists, __full__, skill-only discovery and query-only routing; explicit true conflicts with accelerators or __full__.
skill_acceleration_exec(query=faithful_goal, skill=exact_name, accelerators=[{{"id":"segment_id","params":{{}},"force":false}}], params=shared_arguments).
accelerators and accelerator are mutually exclusive. Shared params must be valid for every target; step.params non-None values override them. Do not share prep-only arguments with later schemas. Keep declaration order; no duplicate IDs or __full__. batch.standalone=false permits only an exclusive one-element batch, including step.force=true. There is no top-level single-segment force.
Skip completed work by default. An explicit step.force rerun first persists invalidation and never bypasses permissions, provenance, review or send protection. After an upstream rerun, decide whether stale downstream outputs need recomputation; they are not silently regenerated. A batch stops on failure/blocking. Repair stopped_at before using resume_remaining. Review defaults to a checkpoint; continue only when continue_after_resume=true AND stop_after=never. Clarify follows its own continue_after_resume flag. Unanswered cards always wait. Resume the original entire call, not a fresh batch. Batch success covers only its requested range; all-skipped/history is not a new delivery."""
    text = text.replace('不支持批量或force参数。', '').replace('已完成段already_done跳过，无强制重跑；', '已完成段默认already_done跳过；')
    return text + f"""
后续步骤及输入获取方式已经明确时，优先一次调用多个阶段。用户限定执行范围、要求中间检查或需要根据结果决定后续步骤时，在对应边界停止。「先做某一步」也可能是在限定范围，不必等用户说出「单段」。
调用 skill_acceleration_exec(query=忠实目标, skill=准确技能名, accelerator=起始段id, params=起始段参数)。auto_batch省略或null时普通起始段默认沿batch.auto_next组批；true明确要求组批，false明确只执行指定单段。没有后继只执行起始段，不按order猜后继；params只给起始段，每批1～{min(max_batch_steps, 8)}段，每条声明链结束后回外层检查或改稿，再决定下一批。
「生成一份5页PPT」从prep开始默认组批；「只做需求准备」或「先做需求准备」用prep、auto_batch=false；「先给我大纲，确认后再生成」启用outline_review，停在大纲边界；「只生成页面，暂时不要导出」用generate、auto_batch=false；「按这几个阶段执行」提交显式accelerators列表。省略/null不改变显式列表、__full__、仅skill查询和旧query路由；显式true与accelerators或__full__冲突即报错。
skill_acceleration_exec(query=忠实目标, skill=准确技能名, accelerators=[{{"id":"准确段id","params":{{}},"force":false}}], params=共享参数)。
accelerators与accelerator互斥；共享params须在每个目标schema均合法，step.params的非None值覆盖共享值，prep专属参数不要共享给后段。按声明顺序，不重复id，不放__full__。batch.standalone=false只能独占单元素批量，也可使用步骤内force=true；不新增单段顶层force。
默认跳过已完成段；明确step.force重跑须先可靠保存失效，不绕过权限、producer、文件、审阅或发送保护。上游重跑后由外层判断下游是否重算，不自动重做、不宣称旧下游基于新事实生成。失败/阻断即停，先修复stopped_at，再按实际前提调用resume_remaining。review默认停批，仅continue_after_resume=true且stop_after=never双条件同时成立才续；clarify按其字段，未答卡始终等待。恢复原整批调用，不另开新批重做前段。success只表示本批范围完成；全部already_done/历史发送不构成本批新交付。"""


def render_appendix(skill, specs, progress, *, language='cn', batch_enabled=True, max_batch_steps=8):
    sections = [START, f'{skill}: ' + ('阶段详情' if language == 'cn' else 'segment details'), render_guide(language, batch_enabled=batch_enabled, max_batch_steps=max_batch_steps)]
    for spec in sorted(specs, key=lambda item: item.order):
        details = {key: getattr(spec, key) for key in ('id', 'order', 'summary', 'when_to_use', 'when_not_to_use', 'input_schema', 'defaults')}
        details.update(requires=[item.model_dump() for item in spec.requires], outputs=spec.outputs.model_dump(),
                       next_steps=spec.handoff.next_steps, continue_hint=spec.handoff.continue_hint,
                       covers=spec.covers.model_dump(), hitl=spec.hitl.model_dump())
        if batch_enabled:
            details['batch'] = spec.batch.model_dump()
        sections.append(json.dumps(details, ensure_ascii=False))
    sections.extend([json.dumps(progress, ensure_ascii=False), END])
    return '\n'.join(sections)
