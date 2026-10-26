"""Offline search probe with fixed query fixtures, never invoke a model or skill.

python -m jiuwenswarm.agents.harness.flash.skill_selection.benchmark --skills-dir PATH
"""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace

CASES = {
    'ppt': '帮我做一个ppt介绍水果香蕉，3页一共',
    'railway': '帮我查询明天北京到上海的高铁，优先上午出发、二等座，列出车次、出发时间、到达时间和余票情况。',
    'excel': ('请生成一个 Excel 工作簿，录入销售额：1 月 12000 元、2 月 15000 元、3 月 18000 元。'
              '使用公式计算合计和平均值，并生成柱状图，交付 .xlsx 文件。'),
    'word': ('请将以下内容整理成正式的会议纪要，交付 .docx 文件：今天讨论了官网改版，决定先改首页和产品介绍页；'
             '小王负责设计，下周三交初稿；小李负责开发，下周五完成；预算尚未确定，需要负责人确认。请列出决议、待办事项和待确认问题。'),
}

# Fixed illustrative model-tool arguments, not a test of model extraction.
QUERIES = {
    'ppt': ('制作三页介绍香蕉的 PPT 演示文稿', ['presentation', 'slides', 'ppt']),
    'railway': ('查询明天北京到上海高铁车次时刻及二等座余票，优先上午出发', ['train', 'railway', '高铁', '余票']),
    'excel': ('生成销售额 Excel 工作簿，公式计算合计平均值并生成柱状图，交付 xlsx',
              ['excel', 'xlsx', 'spreadsheet', 'formula', 'chart']),
    'word': ('生成正式会议纪要 Word 文档，包含决议、待办和待确认问题，交付 docx', ['word', 'docx', 'meeting minutes']),
}


async def measure(args):
    from openjiuwen.harness.rails import SkillUseRail
    from openjiuwen.harness.prompts import SystemPromptBuilder
    from openjiuwen.core.single_agent.ability_manager import AbilityManager
    from .rail import SkillSelectionRail
    from .pipeline import SelectionPipeline
    from .types import SkillDocument
    from .tool import SkillSearchInput

    native = SkillUseRail(args.skills_dir, skill_mode='all', disabled_skills=args.disabled_skill)
    started = time.perf_counter()
    await native.reload_skills()
    native_ms = (time.perf_counter() - started) * 1000
    config = {'flash': {'skill_selection': {'enabled': True}}}
    agent = SimpleNamespace(system_prompt_builder=SystemPromptBuilder(), ability_manager=AbilityManager())
    rail = SkillSelectionRail(config_provider=lambda: config, skill_rail_provider=lambda: native)
    started = time.perf_counter()
    rail.init(agent)
    pipeline, _ = await rail.service.current_snapshot()
    cold_ms = (time.perf_counter() - started) * 1000
    if pipeline is None:
        raise ValueError('No readable skills in the supplied directories')
    report = {'measurement': 'offline; no LLM, no service, no skill execution',
              'warm_scope': ('search tool with fixed query fixtures; '
                             'excludes model generation, SDK rails and skill loading'),
              'samples_per_case': args.samples, 'native_catalog_ms': native_ms, 'cold_index_ms': cold_ms,
              'catalog_count': len(pipeline.documents), 'native_count': len(native.skills), 'cases': {}}
    try:
        for name, query in CASES.items():
            times = []
            for _ in range(args.samples):
                ctx = SimpleNamespace(inputs=SimpleNamespace(messages=[{'role': 'user', 'content': query}], tools=[]),
                                      extra={}, session=None)
                await rail.before_model_call(ctx)
                await rail.after_model_call(ctx)
                started = time.perf_counter()
                model_query, keywords = QUERIES[name]
                request = SkillSearchInput(action='search', query=model_query, keywords=keywords)
                result = await rail.handle_action(request, ctx)
                times.append((time.perf_counter() - started) * 1000)
                if result['status'] != 'candidates':
                    raise RuntimeError(f'Unexpected search fallback for {name}: {result["status"]}')
                top = [candidate['name'] for candidate in result['candidates']]
                chars = len(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
                ticket = ctx.extra[rail.REUSE].tickets[result['search_id']]
                legacy = [dict(name=d.name, score=score, description=d.description,
                               capability_record=d.capability_record) for d, score in ticket['result'].candidates]
                legacy_chars = len(json.dumps(legacy, ensure_ascii=False))
                await rail.after_invoke(ctx)
            report['cases'][name] = dict(warm_search_median_ms=statistics.median(times),
                                        warm_search_max_ms=max(times), tool_result_chars=chars,
                                        legacy_candidate_chars=legacy_chars, top=top)
        # This isolates ranker scaling; artificial records cannot validate recall.
        documents = list(pipeline.documents.values())
        for i in range(len(documents), 1000):
            documents.append(SkillDocument(str(i), f'noise-{i}', '', f'archival unrelated marker {i}', ''))
        large = SelectionPipeline(rail.service.settings)
        large.prepare(documents)
        times = []
        for _ in range(20):
            started = time.perf_counter()
            large.search(*QUERIES['excel'], allowed_ids=frozenset(large.documents))
            times.append((time.perf_counter() - started) * 1000)
        report['synthetic_scale'] = dict(count=len(documents), bm25_median_ms=statistics.median(times),
                                        note='Timing only; padded artificial records, not a recall benchmark')
        return report
    finally:
        rail.uninit(agent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-dir', action='append', required=True)
    parser.add_argument('--disabled-skill', action='append', default=[])
    parser.add_argument('--samples', type=int, default=12, choices=range(1, 101))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    scratch = Path(tempfile.mkdtemp(prefix='flash-selection-benchmark-'))
    # Isolate SDK logging/config side effects from the running user's workspace.
    os.environ['JIUWENSWARM_DATA_DIR'] = str(scratch / 'data')
    os.environ['JIUWENSWARM_CONFIG_DIR'] = str(scratch / 'config')
    from .execution import get_runtime
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(measure(args))
        text = json.dumps(report, ensure_ascii=False, indent=2)
        output = args.output or scratch / 'benchmark.json'
        output.write_text(text, encoding='utf-8')
        logging.disable(logging.NOTSET)
        logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stdout, force=True)
        logging.info('%s', text)
        logging.info('REPORT: %s', output)
    finally:
        get_runtime().shutdown()


if __name__ == '__main__':
    main()
