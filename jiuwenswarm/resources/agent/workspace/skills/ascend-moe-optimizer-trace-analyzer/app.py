from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass

from analyzers.parser import parse_trace_json, events_to_dicts
from analyzers.phase_mapper import PhaseMapper
from analyzers.metrics import (
    build_phase_instances,
    build_phase_summary,
    build_category_summary,
    build_core_group_summary,
    build_phase_core_group_summary,
    build_category_core_group_summary,
    build_name_summary,
    build_phase_tid_summary,
    build_overlap_summary,
    build_bubble_summary,
    build_trace_overview,
)
from analyzers.diagnosis import AutoDiagnosisInput, build_auto_diagnosis
from analyzers.llm_analysis import LLMPromptInput, build_llm_prompt, generate_llm_analysis
from analyzers.reporter import (
    MarkdownReportInput,
    StatisticalSummaryInput,
    ensure_dir,
    save_dataframe,
    save_json,
    build_markdown_report,
    build_statistical_summary,
    save_text,
)

logger = logging.getLogger(__name__)


@dataclass
class RunSummaryInput:
    raw_events_count: int
    overview: dict
    phase_summary: list
    category_summary: list
    core_group_summary: list
    diagnosis: dict
    output_dir: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ascend MoE trace analysis: Chrome/Perfetto JSON (ascend-moe-optimizer-trace-analyzer skill)"
    )
    parser.add_argument(
        "--trace",
        required=True,
        help="Path to trace json",
    )
    parser.add_argument(
        "--phase-map",
        default="config/phase_map.yaml",
        help="Path to phase mapping yaml",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory to save outputs",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="Rows to show in markdown report sections",
    )
    parser.add_argument(
        "--llm-analysis",
        action="store_true",
        help="Run an optional LLM command and append its analysis to report.md",
    )
    parser.add_argument(
        "--llm-command",
        default=None,
        help="External LLM command. It receives the analysis prompt on stdin. Can also use TRACE_ANALYSIS_LLM_CMD.",
    )
    parser.add_argument(
        "--llm-timeout",
        type=int,
        default=120,
        help="Timeout in seconds for --llm-analysis command",
    )
    return parser.parse_args()


def validate_inputs(trace_path: str, phase_map_path: str) -> None:
    if not os.path.exists(trace_path):
        raise FileNotFoundError(f"Trace file not found: {trace_path}")

    if not os.path.exists(phase_map_path):
        raise FileNotFoundError(f"Phase map file not found: {phase_map_path}")


def log_run_summary(ctx: RunSummaryInput) -> None:
    logger.info("Trace analysis completed.")
    logger.info("Raw parsed events: %s", ctx.raw_events_count)
    logger.info("Mapped phase instances: %s", ctx.overview.get("num_instances", 0))
    logger.info("Unique phases: %s", ctx.overview.get("num_phases", 0))
    logger.info("Unique raw names: %s", ctx.overview.get("num_names", 0))
    logger.info("Unique pids: %s", ctx.overview.get("num_pids", 0))
    logger.info("Unique tids: %s", ctx.overview.get("num_tids", 0))
    logger.info("Core groups: %s", ", ".join(ctx.overview.get("core_groups", [])))
    logger.info("Trace wall time (us): %.3f", ctx.overview.get("total_wall_us", 0.0))

    if ctx.diagnosis.get("headline"):
        logger.info("")
        logger.info("Diagnosis: %s", ctx.diagnosis["headline"])

    if ctx.phase_summary:
        logger.info("")
        logger.info("Top phases by union_us:")
        for row in ctx.phase_summary[:10]:
            logger.info(
                "  %s: count=%s, union_us=%.3f, total_us=%.3f, category=%s",
                row.get("phase"),
                row.get("count"),
                row.get("union_us", 0.0),
                row.get("total_us", 0.0),
                row.get("category"),
            )

    if ctx.category_summary:
        logger.info("")
        logger.info("Top categories by union_us:")
        for row in ctx.category_summary[:10]:
            logger.info(
                "  %s: count=%s, union_us=%.3f, ratio=%.3f",
                row.get("category"),
                row.get("count"),
                row.get("union_us", 0.0),
                row.get("ratio_to_total_wall", 0.0),
            )

    if ctx.core_group_summary:
        logger.info("")
        logger.info("Core groups by union_us:")
        for row in ctx.core_group_summary:
            logger.info(
                "  %s: cores=%s, union_us=%.3f, ratio=%.3f",
                row.get("core_group"),
                row.get("observed_core_count"),
                row.get("union_us", 0.0),
                row.get("ratio_to_total_wall", 0.0),
            )

    logger.info("")
    logger.info("Outputs saved to: %s", ctx.output_dir)


def main() -> None:
    args = parse_args()
    validate_inputs(args.trace, args.phase_map)

    ensure_dir(args.output_dir)

    # 1. parse trace
    parsed_events = parse_trace_json(args.trace)
    raw_dicts = events_to_dicts(parsed_events)

    # 2. phase mapping
    mapper = PhaseMapper(args.phase_map)

    # 3. build tables
    instances_df = build_phase_instances(raw_dicts, mapper)
    phase_summary_df = build_phase_summary(instances_df)
    category_summary_df = build_category_summary(instances_df)
    core_group_summary_df = build_core_group_summary(instances_df)
    phase_core_group_summary_df = build_phase_core_group_summary(instances_df)
    category_core_group_summary_df = build_category_core_group_summary(instances_df)
    name_summary_df = build_name_summary(instances_df)
    phase_tid_summary_df = build_phase_tid_summary(instances_df)
    overlap_df = build_overlap_summary(instances_df)
    bubble_df = build_bubble_summary(instances_df)
    overview = build_trace_overview(instances_df)
    overview["raw_parsed_events"] = len(raw_dicts)

    diagnosis = build_auto_diagnosis(
        AutoDiagnosisInput(
            overview=overview,
            phase_summary=phase_summary_df,
            category_summary=category_summary_df,
            core_group_summary=core_group_summary_df,
            phase_core_group_summary=phase_core_group_summary_df,
            category_core_group_summary=category_core_group_summary_df,
            name_summary=name_summary_df,
            overlap_summary=overlap_df,
            bubble_summary=bubble_df,
        )
    )

    llm_prompt = build_llm_prompt(
        LLMPromptInput(
            overview=overview,
            diagnosis=diagnosis,
            phase_summary=phase_summary_df,
            category_summary=category_summary_df,
            core_group_summary=core_group_summary_df,
            phase_core_group_summary=phase_core_group_summary_df,
            category_core_group_summary=category_core_group_summary_df,
            name_summary=name_summary_df,
            overlap_summary=overlap_df,
            bubble_summary=bubble_df,
            top_n=args.top_n,
        )
    )
    llm_analysis = generate_llm_analysis(
        prompt=llm_prompt,
        enabled=args.llm_analysis,
        command=args.llm_command,
        timeout_s=args.llm_timeout,
    )

    plot_files = []
    try:
        from analyzers.plots import generate_summary_plots

        plot_files.extend(
            generate_summary_plots(
                output_dir=args.output_dir,
                phase_summary=phase_summary_df,
                category_summary=category_summary_df,
                core_group_summary=core_group_summary_df,
                top_n=args.top_n,
            )
        )
    except ModuleNotFoundError as exc:
        logger.warning("summary plots skipped: missing dependency (%s)", exc)

    statistical_summary = build_statistical_summary(
        StatisticalSummaryInput(
            overview=overview,
            phase_summary=phase_summary_df,
            category_summary=category_summary_df,
            core_group_summary=core_group_summary_df,
            category_core_group_summary=category_core_group_summary_df,
            overlap_summary=overlap_df,
            bubble_summary=bubble_df,
            heading="## Statistical Highlights",
        )
    )
    standalone_statistical_summary = build_statistical_summary(
        StatisticalSummaryInput(
            overview=overview,
            phase_summary=phase_summary_df,
            category_summary=category_summary_df,
            core_group_summary=core_group_summary_df,
            category_core_group_summary=category_core_group_summary_df,
            overlap_summary=overlap_df,
            bubble_summary=bubble_df,
            heading="# Statistical Highlights",
        )
    )

    # 4. save tables
    save_dataframe(instances_df, f"{args.output_dir}/phase_instances.csv")
    save_dataframe(phase_summary_df, f"{args.output_dir}/phase_summary.csv")
    save_dataframe(category_summary_df, f"{args.output_dir}/category_summary.csv")
    save_dataframe(core_group_summary_df, f"{args.output_dir}/core_group_summary.csv")
    save_dataframe(phase_core_group_summary_df, f"{args.output_dir}/phase_core_group_summary.csv")
    save_dataframe(category_core_group_summary_df, f"{args.output_dir}/category_core_group_summary.csv")
    save_dataframe(name_summary_df, f"{args.output_dir}/name_summary.csv")
    save_dataframe(phase_tid_summary_df, f"{args.output_dir}/phase_tid_summary.csv")
    save_dataframe(overlap_df, f"{args.output_dir}/overlap_summary.csv")
    save_dataframe(bubble_df, f"{args.output_dir}/bubble_summary.csv")
    save_text(standalone_statistical_summary, f"{args.output_dir}/statistical_summary.md")
    save_text(llm_prompt, f"{args.output_dir}/llm_prompt.md")
    save_json(llm_analysis, f"{args.output_dir}/llm_analysis_meta.json")
    if llm_analysis.get("analysis"):
        save_text(llm_analysis["analysis"], f"{args.output_dir}/llm_analysis.md")

    # 6. save report
    report_md = build_markdown_report(
        MarkdownReportInput(
            overview=overview,
            phase_summary=phase_summary_df,
            category_summary=category_summary_df,
            core_group_summary=core_group_summary_df,
            phase_core_group_summary=phase_core_group_summary_df,
            category_core_group_summary=category_core_group_summary_df,
            name_summary=name_summary_df,
            overlap_summary=overlap_df,
            bubble_summary=bubble_df,
            diagnosis=diagnosis,
            llm_analysis=llm_analysis,
            plot_files=plot_files,
            statistical_summary=statistical_summary,
            top_n=args.top_n,
        )
    )
    save_text(report_md, f"{args.output_dir}/report.md")

    # 7. save overview json
    save_json(overview, f"{args.output_dir}/summary.json")
    save_json(diagnosis, f"{args.output_dir}/diagnosis.json")
    if args.llm_analysis and llm_analysis.get("status") != "ok":
        logger.warning(
            "LLM analysis skipped: %s",
            llm_analysis.get("error") or llm_analysis.get("status"),
        )

    log_run_summary(
        RunSummaryInput(
            raw_events_count=len(raw_dicts),
            overview=overview,
            phase_summary=phase_summary_df,
            category_summary=category_summary_df,
            core_group_summary=core_group_summary_df,
            diagnosis=diagnosis,
            output_dir=args.output_dir,
        )
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
