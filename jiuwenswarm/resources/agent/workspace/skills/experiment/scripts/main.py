"""Command-line entrypoint for validating, scaffolding and running module three."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Type

from pydantic import BaseModel, ValidationError

from agent_coordinator import ExperimentAgentCoordinator
from contracts import ExperimentModuleInput, ExperimentModuleOutput
from load_inputs import load_request
from pipeline import ExperimentPipeline
from planning_adapter import adapt_planning_bundle
from scaffold_manifest import scaffold_manifest
from io_utils import write_json_atomic
from implementation_builder import GeneratedImplementationProposal
from review_contracts import CodeReviewDecision, ExecutionReviewDecision


ContractType = Type[BaseModel]


def _load_json(path: Path) -> object:
    if str(path) == "-":
        return json.load(sys.stdin)
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _validate(path: Path, model: ContractType) -> int:
    try:
        validated = model.model_validate(_load_json(path))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"INVALID: {exc}")
        return 1
    print(f"VALID: {validated.__class__.__name__}")
    return 0


def _print_schema(model: ContractType) -> int:
    print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2))
    return 0


def _add_agent_resume_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)


def _add_download_arguments(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(allow_downloads=True)
    parser.add_argument(
        "--allow-downloads",
        dest="allow_downloads",
        action="store_true",
        help="Allow public dataset search/downloads (default)",
    )
    parser.add_argument(
        "--no-downloads",
        dest="allow_downloads",
        action="store_false",
        help="Disable all network dataset downloads for this invocation",
    )


def _agent_coordinator(args: argparse.Namespace) -> ExperimentAgentCoordinator:
    max_download_gb = getattr(args, "max_download_gb", 20.0)
    if max_download_gb <= 0:
        raise ValueError("--max-download-gb must be greater than zero")
    return ExperimentAgentCoordinator(
        manifest_path=getattr(args, "manifest", None),
        allow_downloads=getattr(args, "allow_downloads", True),
        max_download_bytes=int(max_download_gb * 1024**3),
        base_dir=Path.cwd(),
        # 宿主对单次工具调用有 300 秒硬超时且判为非幂等不重试；工具必须在超时前
        # **自己**返回进度，而不是被硬杀。默认 240s 留出收尾余量。
        time_budget_seconds=getattr(args, "time_budget_s", None),
    )


def _print_agent_response(response: BaseModel) -> int:
    print(response.model_dump_json(indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Experiment module runtime tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_input = subparsers.add_parser("validate-input")
    validate_input.add_argument(
        "--input", required=True, type=Path, help="JSON path, or - for stdin"
    )

    validate_output = subparsers.add_parser("validate-output")
    validate_output.add_argument(
        "--input", required=True, type=Path, help="JSON path, or - for stdin"
    )

    schema = subparsers.add_parser("schema")
    schema.add_argument("--kind", choices=("input", "output"), required=True)

    scaffold = subparsers.add_parser(
        "scaffold-manifest",
        help="Generate a deliberately non-runnable manifest draft",
    )
    scaffold.add_argument("--input", required=True, type=Path)
    scaffold.add_argument("--output", type=Path)
    scaffold.add_argument("--force", action="store_true")

    adapt = subparsers.add_parser(
        "adapt-planning",
        help="Assemble Module 2 split outputs into strict Module 3 input",
    )
    adapt.add_argument("--planning-dir", required=True, type=Path)
    adapt.add_argument("--workspace-root", required=True, type=Path)
    adapt.add_argument("--manifest", type=Path)
    adapt.add_argument("--domain", type=Path)
    adapt.add_argument("--resource-constraints", type=Path)
    adapt.add_argument("--output", type=Path)

    run = subparsers.add_parser("run", help="Execute the complete experiment pipeline")
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("--manifest", type=Path)
    _add_download_arguments(run)
    run.add_argument("--max-download-gb", type=float, default=20.0)
    run.add_argument("--print-output", action="store_true")

    agent_init = subparsers.add_parser(
        "agent-init",
        help="Validate inputs and initialize the persisted Agent state machine",
    )
    agent_init.add_argument("--input", required=True, type=Path)
    agent_init.add_argument("--manifest", type=Path)
    _add_download_arguments(agent_init)
    agent_init.add_argument("--max-download-gb", type=float, default=20.0)

    agent_data = subparsers.add_parser(
        "agent-data", help="Run the ExperimentDataAgent stage"
    )
    _add_agent_resume_arguments(agent_data)
    _add_download_arguments(agent_data)
    agent_data.add_argument("--max-download-gb", type=float, default=20.0)
    # 单次调用的下载时间预算（秒）。宿主对工具调用有 300 秒硬超时且判为非幂等
    # 不重试——超过就是整个 task loop 结束。工具必须在这个预算内主动返回进度，
    # agent 再用同一组参数续跑。默认 240s（留 60s 收尾）；0 = 不限（CLI 直调）。
    agent_data.add_argument("--time-budget-s", type=float, default=240.0)

    agent_implementation = subparsers.add_parser(
        "agent-implementation",
        help="Run the ExperimentImplementationAgent stage",
    )
    _add_agent_resume_arguments(agent_implementation)

    agent_inspect_implementation = subparsers.add_parser(
        "agent-inspect-implementation",
        help="Return the validated read-only Implementation Builder context",
    )
    _add_agent_resume_arguments(agent_inspect_implementation)

    agent_write_generated = subparsers.add_parser(
        "agent-write-generated",
        help="Write a validated generated-code proposal inside this run only",
    )
    _add_agent_resume_arguments(agent_write_generated)
    agent_write_generated.add_argument(
        "--proposal",
        type=Path,
        default=Path("-"),
        help="Proposal JSON path; omit or use - to read stdin",
    )

    agent_review_context = subparsers.add_parser(
        "agent-review-context",
        help="Return the root Agent's isolated code or execution review context",
    )
    _add_agent_resume_arguments(agent_review_context)

    agent_code_review = subparsers.add_parser(
        "agent-submit-code-review",
        help="Submit strict root-Agent code review and conditionally run bounded smoke",
    )
    _add_agent_resume_arguments(agent_code_review)
    agent_code_review.add_argument(
        "--review", type=Path, default=Path("-"), help="JSON path or stdin"
    )

    agent_execution_review = subparsers.add_parser(
        "agent-submit-execution-review",
        help="Submit strict root-Agent post-smoke review for backend approval",
    )
    _add_agent_resume_arguments(agent_execution_review)
    agent_execution_review.add_argument(
        "--review", type=Path, default=Path("-"), help="JSON path or stdin"
    )

    agent_execute = subparsers.add_parser(
        "agent-execute", help="Run the ExperimentExecutionAgent stage"
    )
    _add_agent_resume_arguments(agent_execute)

    agent_analyze = subparsers.add_parser(
        "agent-analyze", help="Run the ExperimentAnalysisAgent stage"
    )
    _add_agent_resume_arguments(agent_analyze)

    agent_status = subparsers.add_parser(
        "agent-status", help="Read the persisted ExperimentAgent state"
    )
    _add_agent_resume_arguments(agent_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "validate-input":
        return _validate(args.input, ExperimentModuleInput)
    if args.command == "validate-output":
        return _validate(args.input, ExperimentModuleOutput)
    if args.command == "scaffold-manifest":
        request = load_request(args.input)
        output = args.output
        if output is None:
            output = Path(request.execution_config.run_dir) / "implementation-manifest.json"
        scaffold_manifest(request, output, overwrite=args.force)
        print(f"CREATED: {output}")
        print(
            "This draft is blocked by execution_approved=false, ready=false, "
            "verified=false, and seed_aggregation=null."
        )
        return 0
    if args.command == "adapt-planning":
        result = adapt_planning_bundle(
            args.planning_dir,
            workspace_root=args.workspace_root,
            manifest_path=args.manifest,
            domain_path=args.domain,
            resource_constraints_path=args.resource_constraints,
        )
        if args.output is not None and result.request is not None:
            write_json_atomic(
                args.output,
                result.request.model_dump(mode="json"),
            )
        print(result.model_dump_json(indent=2))
        return 0 if result.status == "READY" else 2
    if args.command == "run":
        if args.max_download_gb <= 0:
            raise SystemExit("--max-download-gb must be greater than zero")
        request = load_request(args.input)
        pipeline = ExperimentPipeline(
            manifest_path=args.manifest,
            allow_downloads=args.allow_downloads,
            max_download_bytes=int(args.max_download_gb * 1024**3),
            base_dir=Path.cwd(),
        )
        output = pipeline.run(request)
        if args.print_output:
            print(output.model_dump_json(indent=2))
        else:
            print(f"STATUS: {output.status}")
        return 0 if output.status in {"PASS", "PARTIAL"} else 2
    if args.command == "agent-init":
        return _print_agent_response(
            _agent_coordinator(args).initialize(load_request(args.input))
        )
    if args.command == "agent-data":
        return _print_agent_response(
            _agent_coordinator(args).prepare_data(args.run_dir, run_id=args.run_id)
        )
    if args.command == "agent-implementation":
        return _print_agent_response(
            _agent_coordinator(args).resolve_implementation(
                args.run_dir, run_id=args.run_id
            )
        )
    if args.command == "agent-inspect-implementation":
        return _print_agent_response(
            _agent_coordinator(args).inspect_implementation(
                args.run_dir, run_id=args.run_id
            )
        )
    if args.command == "agent-write-generated":
        proposal = GeneratedImplementationProposal.model_validate(
            _load_json(args.proposal)
        )
        return _print_agent_response(
            _agent_coordinator(args).write_generated_proposal(
                args.run_dir,
                run_id=args.run_id,
                proposal=proposal,
            )
        )
    if args.command == "agent-review-context":
        return _print_agent_response(
            _agent_coordinator(args).review_context(
                args.run_dir, run_id=args.run_id
            )
        )
    if args.command == "agent-submit-code-review":
        decision = CodeReviewDecision.model_validate(_load_json(args.review))
        return _print_agent_response(
            _agent_coordinator(args).submit_code_review(
                args.run_dir, run_id=args.run_id, decision=decision
            )
        )
    if args.command == "agent-submit-execution-review":
        decision = ExecutionReviewDecision.model_validate(_load_json(args.review))
        return _print_agent_response(
            _agent_coordinator(args).submit_execution_review(
                args.run_dir, run_id=args.run_id, decision=decision
            )
        )
    if args.command == "agent-execute":
        return _print_agent_response(
            _agent_coordinator(args).execute(args.run_dir, run_id=args.run_id)
        )
    if args.command == "agent-analyze":
        return _print_agent_response(
            _agent_coordinator(args).analyze(args.run_dir, run_id=args.run_id)
        )
    if args.command == "agent-status":
        return _print_agent_response(
            _agent_coordinator(args).status(args.run_dir, run_id=args.run_id)
        )
    model = ExperimentModuleInput if args.kind == "input" else ExperimentModuleOutput
    return _print_schema(model)


if __name__ == "__main__":
    raise SystemExit(main())
