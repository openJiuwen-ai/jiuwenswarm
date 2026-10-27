"""Run this benchmark through native DeepAgent and benchmark-owned grading."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent


def materialize_refs(source: Path, output: Path) -> Path:
    import yaml
    source = source.expanduser().resolve()
    payload = yaml.safe_load(source.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('harness_refs'), dict):
        raise ValueError('harness refs must contain a harness_refs mapping')
    resolved = {}
    for role, value in payload['harness_refs'].items():
        package = Path(str(value)).expanduser()
        package = (package if package.is_absolute() else source.parent / package).resolve()
        if not package.is_dir():
            raise ValueError('missing Harness package: ' + str(package))
        resolved[str(role)] = str(package)
    if len(resolved) != 1:
        raise ValueError('this benchmark uses one native Harness role')
    payload['harness_refs'] = resolved
    for role in payload.get('roles', []):
        name = str(role.get('member_name') or role.get('role') or '')
        if name in resolved:
            role['harness_ref_path'] = resolved[name]
    output.mkdir(parents=True)
    target = output / 'input_harness_refs.yaml'
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding='utf-8')
    return target


def materialize_dataset(source: Path, output: Path) -> Path:
    from openjiuwen.rsi.harness_rsi.data_loader.case_files import copy_dataset_files
    from openjiuwen.rsi.harness_rsi.single_harness.iterative import load_cases
    target = output / 'input' / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    copy_dataset_files(load_cases([str(source)]), source, target)
    return target


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('mode', choices=['validate', 'evaluate', 'optimize'])
    cli.add_argument('--split', choices=['train', 'holdout', 'controls'], default='train')
    cli.add_argument('--profile', type=Path, help='Existing RSI profile; model refs are borrowed, scoring is replaced')
    cli.add_argument('--harness-refs', type=Path, default=ROOT / 'baseline_refs.yaml')
    cli.add_argument('--output', type=Path)
    cli.add_argument('--epochs', type=int, default=1)
    cli.add_argument('--concurrency', type=int, default=1)
    return cli


async def run(args: argparse.Namespace) -> None:
    from openjiuwen.rsi.harness_rsi.single_harness.iterative import load_cases
    from grader import Acceptance
    dataset = ROOT / (args.split + '.json')
    cases = load_cases([str(dataset)])
    acceptance = Acceptance(ROOT)
    for case in cases:
        acceptance.validate_case(case)
    if args.mode == 'validate':
        print(json.dumps({'status':'valid','split':args.split,'cases':len(cases),'model_calls':0}))
        return
    if args.profile is None or args.output is None:
        raise ValueError('evaluate/optimize require --profile and --output')
    if args.output.exists():
        raise ValueError('choose a new output directory to preserve evaluation provenance')
    if args.epochs < 1 or args.concurrency < 1:
        raise ValueError('epochs and concurrency must be positive')
    from adapter import HarnessAcceptanceEvaluator
    from openjiuwen.rsi.harness_rsi.config import load_auto_coordinating_harness_config
    cfg = load_auto_coordinating_harness_config(str(args.profile.resolve()))
    cfg = replace(cfg, workspace_dir=str(args.output.resolve() / 'run'), max_epochs=args.epochs,
                  evaluator=replace(cfg.evaluator, backend='single_harness', evaluation_method='script_based'),
                  scheduling=replace(cfg.scheduling, full_evaluation_concurrency=args.concurrency),
                  data_loader=replace(cfg.data_loader, batch_size=1))
    evaluator = HarnessAcceptanceEvaluator(cfg.evaluator)
    output = args.output.resolve()
    refs = materialize_refs(args.harness_refs, output)
    dataset = materialize_dataset(dataset, output)
    cases = load_cases([str(dataset)])
    if args.mode == 'evaluate':
        ref = await evaluator.evaluate_batch(cases=cases, team_skill_ref_path='',
                                             harness_refs_path=str(refs),
                                             output_dir=str(output), case_concurrency=args.concurrency)
        print(json.dumps({'eval_ref':ref,'split':args.split},ensure_ascii=False))
        return
    if args.split != 'train':
        raise ValueError('optimization may only consume train; holdout and controls are evaluation-only')
    from openjiuwen.rsi.harness_rsi.single_harness.iterative import (
        IterativeSingleHarnessRequest, SingleHarnessIterativeOptimizationOrchestrator,
    )
    engine = SingleHarnessIterativeOptimizationOrchestrator(cfg, evaluator=evaluator)
    result = await engine.run(IterativeSingleHarnessRequest(
        dataset_files=[str(dataset)], harness_refs_path=str(refs),
        output_dir=str(output / 'run'), dataset_id='harness-baseline-v1-train', auto_full_baseline=True,
    ))
    # Holdout evidence is written only after optimization and never fed back to it.
    reports = {}
    for split in ['holdout', 'controls']:
        frozen_split = materialize_dataset(ROOT / (split + '.json'), output)
        split_cases = load_cases([str(frozen_split)])
        reports[split] = await evaluator.evaluate_batch(cases=split_cases, team_skill_ref_path='',
                            harness_refs_path=result.best_harness_refs_path,
                            output_dir=str(output / ('post_' + split)), case_concurrency=args.concurrency)
    print(json.dumps({'best_score':result.best_score,'best_harness_refs':result.best_harness_refs_path,
                      'published_harness_refs':result.published_harness_refs_path, 'post_evaluations':reports},ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(run(parser().parse_args()))
