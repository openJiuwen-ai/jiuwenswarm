"""Materialize public ICLR styles for the existing sandboxed paper writer."""

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import paper_workspace_dir
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting import figures
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.agent import ReportingAgent
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.sections import DOCUMENT_ORDER

_TEMPLATE_FILES = ("iclr2026_conference.sty", "iclr2026_conference.bst", "natbib.sty", "fancyhdr.sty")
_SHORT_PAPER_POLICY = (
    "ICLR short-paper completion policy takes precedence over conflicting full-paper targets "
    "and review order below. Aim for about 2860 words: abstract 180, introduction 380, "
    "related work 320, method 750, experiments 650, discussion 420, conclusion 160. "
    "Word-count notes are non-blocking short-paper guidance: retain them without padding; "
    "missing sections, unknown citation keys, session errors and compiler errors remain blocking. "
    "On retry, inventory the existing sections once and write missing sections before rechecking existing ones. "
    "Use supplied exact host facts once; do not repeatedly reconstruct supplied verified_facts from results.json "
    "or run shell Python queries to rederive already provided statistics. Preserve genuine numeric discrepancies for review. "
    "If grep reports a sandbox access denial, switch to glob/read_file without widening sandbox roots. "
    "Configured enabled modules do not prove actual execution; describe configured architecture separately from supplied actual run reports. "
    "Without a supplied actual Reflection report, do not claim this run executed Reflection; identify it only as an optional architecture stage. "
    "Use verified_facts to identify completed input-token matching; do not describe completed controls as future work, and distinguish them from output-length or compute matching. "
    "Shared initial-correct responses contribute zero to a paired arm contrast; do not attribute a full-versus-control advantage to an initial policy shared by both arms. "
    "Identical McNemar p-values may result from identical discordant counts; do not infer inheritance or absent recomputation from p-value equality, and rely on actual Execution evidence. "
    "Read each enabled SKILL.md and supplied evidence once, then immediately write title.txt, contributions.txt, keywords.txt and all sections. "
    "Treat supplied skill CLIs as black boxes: do not read their Python implementations or inspect SDK/site-packages "
    "with inspect.getsource or inspect.getsourcefile. Invoke documented CLIs and diagnose only their actual JSON/compiler errors. "
    "Finish all seven drafts, including abstract, before any lint; write abstract after the other six drafts. "
    "Run the supplied real ts-latex compile before optional review. "
    "ts-review remains bounded: at most three repair attempts per section, then move on. "
    "For each three-lens review pass, dispatch reviewer calls sequentially, with at most one reviewer task_tool call per assistant response; "
    "wait for its result before issuing the next. Keep the draft unchanged between these three isolated reviews "
    "and never include earlier reviewer outputs in a later prompt. "
    "Retain word-count and layout notes instead of padding to full-paper minima. "
    "Verify numeric warnings against supplied evidence, preserving genuine mismatches without "
    "inventing replacements or repeatedly querying known_numbers. "
    "All unknown citation keys and actual compiler errors must be resolved before claiming success. "
    "If review changes source, recompile before finishing."
)


def _writer_facts(metrics):
    # Keep exact nested findings; enumeration stays in the untouched results.json.
    def compact(value):
        if isinstance(value, dict):
            return {key: compact(item) for key, item in value.items()
                    if key != "enumerated_possible_missing_outcomes"}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    facts = compact({key: value for key, value in metrics.items()
                     if key not in {"length_match_audit", "diagnostic_minus_matched_prompt_tokens"}})
    differences = metrics.get("diagnostic_minus_matched_prompt_tokens")
    if differences:
        facts["prompt_token_difference_summary"] = {
            "count": len(differences), "minimum": min(differences), "maximum": max(differences),
            "mean": sum(differences) / len(differences), "total": sum(differences),
        }
    rows = metrics.get("length_match_audit")
    if rows is not None:
        facts["decoded_length_match_summary"] = {
            "count": len(rows), "equal_count": sum(row.get("matched_lengths_equal") is True for row in rows),
        }
    return facts


class IclrReportingAgent(ReportingAgent):
    def __init__(self, config, *, template_dir: str | Path, model=None):
        super().__init__(config, model=model)
        self.template_dir = Path(template_dir).expanduser().resolve()

    async def _run_async(self, inputs):
        result = inputs.result.model_copy(deep=True)
        for variant in result.variants:
            intervals = variant.metrics.get("accuracy_wilson_95", {})
            for arm, bounds in intervals.items():
                for label, value in zip(("lower", "upper"), bounds):
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        variant.metrics.setdefault(f"accuracy_wilson_95_{arm}_{label}", value)
        return await super()._run_async(inputs.model_copy(update={"result": result}))

    @staticmethod
    def _build_evidence_blocks(inputs, design_context, background, bib, figure_path):
        evidence = ReportingAgent._build_evidence_blocks(inputs, design_context, background, bib, figure_path)
        names = ("baseline_accuracy", "generic_accuracy", "verifier_accuracy")
        variants = inputs.result.variants
        if len(variants) != 1 or variants[0].name != "paired_study" or not all(
            isinstance(variants[0].metrics.get(name), (int, float))
            and not isinstance(variants[0].metrics.get(name), bool) for name in names
        ):
            return evidence
        metrics = variants[0].metrics
        if isinstance(metrics.get("lengthmatched_accuracy"), (int, float)) and not isinstance(
            metrics.get("lengthmatched_accuracy"), bool
        ):
            names += ("lengthmatched_accuracy",)
        evidence["verified_facts"] = (
            "Exact host-computed facts for drafting; raw rows and allocations remain in results.json. "
            "These facts suffice for numeric claims; draft sections now without rereading raw enumeration.\n"
            + json.dumps(_writer_facts(metrics), ensure_ascii=False, indent=2)
        )
        result = inputs.result.model_copy(deep=True)
        result.variants[0].metrics = {name: result.variants[0].metrics[name] for name in names}
        if figure_path is not None:
            figures.build_results_figure(result, figure_path)
        visible = ReportingAgent._build_evidence_blocks(
            inputs.model_copy(update={"result": result}), design_context, background, bib, figure_path,
        )
        marker = "\n\nHost-rendered results table — include exactly as given, do not redraw it:\n\n"
        evidence["results"] = evidence["results"].split(marker, 1)[0] + marker + visible["results"].split(marker, 1)[1]
        return evidence

    def _build_paper_agent(self, *, run_id: str):
        # Upstream has already wiped the first-attempt workspace at this point.
        workspace = paper_workspace_dir(run_id)
        workspace.mkdir(parents=True, exist_ok=True)
        for name in _TEMPLATE_FILES:
            shutil.copy2(self.template_dir / name, workspace / name)
        agent = super()._build_paper_agent(run_id=run_id)
        prompt = _SHORT_PAPER_POLICY + "\n\n" + agent.deep_config.system_prompt
        agent.deep_config.system_prompt = prompt
        agent.system_prompt_builder.add_section(PromptSection(
            name=SectionName.IDENTITY, content={"cn": prompt, "en": prompt}, priority=10,
        ))
        agent.apply_prompt_builder_to_react_agent()
        return agent

    def _materialize_skills(self, workspace: Path, skill_dirs: list[str]) -> Path:
        root = super()._materialize_skills(workspace, skill_dirs)
        script = root / "ts-latex" / "scripts" / "compile.py"
        text = script.read_text(encoding="utf-8")
        wrapper = '\n\ndef assemble_document(**kwargs):\n    document = _assemble_document(**kwargs)\n'
        for old, new in (
            (r"\usepackage[preprint]{neurips_2025}", r"\usepackage{iclr2026_conference,times}"),
            (r"\author{OpenJiuwen Team}", r"\author{Anonymous Authors}"),
            (r"\bibliographystyle{plainnat}", r"\bibliographystyle{iclr2026_conference}"),
        ):
            wrapper += f"    document = document.replace({old!r}, {new!r})\n"
        wrapper += "    return document\n"
        text = text.replace("    assemble_document,", "    assemble_document as _assemble_document,", 1)
        text = text.replace("\ndef main()", wrapper + "\ndef main()", 1)
        script.write_text(text, encoding="utf-8")
        for name in ("ts-write", "ts-review"):
            skill = root / name / "SKILL.md"
            text = skill.read_text(encoding="utf-8")
            text = text.replace(f"# {name}\n", f"# {name}\n\n{_SHORT_PAPER_POLICY}\n", 1)
            if name == "ts-write":
                for old, new in (("800-1200", "330-420"), ("400-800", "260-360"),
                                 ("2000-3000", "650-850"), ("1000-1500", "550-750"),
                                 ("900-1400", "350-500"), ("200-280", "130-180"), ("150-220", "150-200")):
                    text = text.replace(old, new)
            else:
                text = text.replace("only after every section passes Step 1", "after bounded Step 1, with unresolved soft notes retained")
            skill.write_text(text, encoding="utf-8")
        return root

    def _verify_and_build_output(
        self, *, run_id, workspace, sections_dir, refs_bib_path,
        figure_paths, known_keys, result, session_error=None, preflight_note=None,
    ):
        output = super()._verify_and_build_output(
            run_id=run_id, workspace=workspace, sections_dir=sections_dir,
            refs_bib_path=refs_bib_path, figure_paths=figure_paths,
            known_keys=known_keys, result=result, session_error=session_error,
            preflight_note=preflight_note,
        )
        if session_error:
            return output.model_copy(update={"status": "failed"})
        return output

    async def _run_paper_agent(self, *, run_id: str, query: str) -> str | None:
        workspace = paper_workspace_dir(run_id)
        script = workspace / ".skills/ts-latex/scripts/compile.py"
        command = [sys.executable, "-B", str(script), str(workspace)]
        shell_command = " ".join(f'"{Path(arg).as_posix()}"' if arg != "-B" else arg for arg in command)
        query += (
            "\n\n" + _SHORT_PAPER_POLICY +
            "\n\nCompetition format requirement: write an English ICLR Short Paper using the "
            "four ICLR 2026 template files already available in this paper workspace. "
            r"main.tex must use \documentclass{article}, \usepackage{iclr2026_conference,times}, "
            r"\author{Anonymous Authors}, \bibliographystyle{iclr2026_conference}, and \bibliography{refs}. "
            r"Keep anonymous submission mode: do not invoke \iclrfinalcopy. "
            "Do not use a NeurIPS template. Preserve the supplied evidence and refs.bib citation keys; "
            "write and compile main.pdf through the normal reporting skills. "
            "Use this exact command once title.txt, refs.bib and all seven sections exist: "
            f"{shell_command}. Do not use cd /d or redirect output to nul on this Windows bash host. "
            "Use this explicit paper workspace; do not search the SDK installation for experiments. "
            "Finish all seven sections, then compile before optional bounded lint review; "
            "retain soft numeric and layout notes and recompile after necessary source changes. "
            "Round measured statistics to four decimal places for the existing numeric checker; "
            "preserve actual numbers and citation keys instead of inventing replacements."
        )
        session_error = await super()._run_paper_agent(run_id=run_id, query=query)
        required = [workspace / "title.txt", workspace / "refs.bib"]
        required.extend(workspace / "sections" / f"{name}.tex" for name in DOCUMENT_ORDER)
        # Compile the last writer edits; an earlier or partial PDF cannot stand in for success.
        pdf = workspace / "main.pdf"
        pdf.unlink(missing_ok=True)
        if not script.is_file():
            return session_error or "host compile script is missing"
        if not all(path.is_file() and path.stat().st_size > 0 for path in required):
            return "; ".join(note for note in (session_error, "host compile requires nonempty title, refs and all seven sections") if note)

        logs = workspace / "logs"
        logs.mkdir(exist_ok=True)
        started = time.monotonic()
        timed_out = False
        returncode = None
        try:
            proc = await asyncio.to_thread(
                subprocess.run, command, cwd=workspace, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=330, check=False,
            )
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout, stderr = exc.stdout, exc.stderr
        try:
            outcome = json.loads((stdout or "").splitlines()[-1])
            compiled = isinstance(outcome, dict) and outcome.get("success") is True
        except (IndexError, ValueError):
            compiled = False
        for name, content in (("stdout", stdout), ("stderr", stderr)):
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            (logs / f"host_compile.{name}.log").write_text(content or "", encoding="utf-8")
        (logs / "host_compile.json").write_text(json.dumps({
            "command": command, "returncode": returncode, "timed_out": timed_out,
            "duration_seconds": time.monotonic() - started,
        }, indent=2), encoding="utf-8")
        if timed_out or returncode != 0 or not compiled or not pdf.is_file() or pdf.stat().st_size == 0:
            pdf.unlink(missing_ok=True)
            failure = ("host compile timed out after 330s; see logs/host_compile.*" if timed_out else
                       "host compile did not produce a nonempty PDF; see logs/host_compile.*")
            return "; ".join(note for note in (session_error, failure) if note)
        return session_error
