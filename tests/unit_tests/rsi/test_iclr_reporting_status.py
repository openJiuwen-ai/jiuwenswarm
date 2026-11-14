"""Real SDK final verification with isolated files; no writer/model/compiler runs."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting import (
    agent as sdk,
)
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.sections import (
    DOCUMENT_ORDER,
)

from jiuwenswarm.agents.harness.common.rsi import iclr_reporting as candidate


def no_network(*args, **kwargs):
    raise RuntimeError("Network disabled in candidate tests")


class ReportingStatusTests(unittest.TestCase):
    def setUp(self):
        for target in (
            "socket.create_connection",
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.getaddrinfo",
        ):
            network_patch = patch(target, side_effect=no_network)
            network_patch.start()
            self.addCleanup(network_patch.stop)

    def collect(self, error=None, *, pdf=True, citation=None, agent_type=None):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            sections = workspace / "sections"
            sections.mkdir()
            for name in DOCUMENT_ORDER:
                text = "A short retained section."
                if citation:
                    text += " \\cite{" + citation + "}."
                (sections / (name + ".tex")).write_text(text, encoding="utf8")
            refs = workspace / "refs.bib"
            refs.write_text(
                "@article{known,title={Retained reference}}", encoding="utf8"
            )
            final_pdf = workspace / "main.pdf"
            if pdf:
                final_pdf.write_bytes(b"%PDF-1.4\nretained compiled artifact\n%%EOF")
            agent = object.__new__(agent_type or candidate.IclrReportingAgent)
            agent._latex_runtime = SimpleNamespace(available=True)
            with (
                patch.object(sdk, "paper_output_path", return_value=final_pdf),
                patch.object(
                    sdk, "paper_tex_path", return_value=workspace / "main.tex"
                ),
            ):
                kwargs = dict(
                    run_id="isolated-status-test",
                    workspace=workspace,
                    sections_dir=sections,
                    refs_bib_path=refs,
                    figure_paths=["retained-figure.pdf"],
                    known_keys={"known"},
                    result=SimpleNamespace(variants=[]),
                    session_error=error,
                )
                output = agent._verify_and_build_output(**kwargs)
                baseline = sdk.ReportingAgent._verify_and_build_output(agent, **kwargs)
                expected = baseline.model_dump()
                if error and agent_type is None:
                    expected["status"] = "failed"
                self.assertEqual(output.model_dump(), expected)
                return output

    def test_compiled_pdf_cannot_hide_session_error(self):
        error = "writer session failed: RSI_MODEL_BUDGET_EXCEEDED"
        baseline = self.collect(error, agent_type=sdk.ReportingAgent)
        actual = self.collect(error)
        self.assertEqual(baseline.status, "compiled")
        self.assertEqual(actual.status, "failed")
        self.assertIsNotNone(actual.paper_pdf_path)
        self.assertIn(error, actual.notes)
        self.assertEqual(actual.figure_paths, ["retained-figure.pdf"])
        self.assertTrue(actual.sections_dir and actual.refs_bib_path)

    def test_no_session_error_remains_compiled(self):
        self.assertEqual(self.collect(citation="known").status, "compiled")

    def test_unknown_citation_remains_failed(self):
        self.assertEqual(self.collect(citation="unknown").status, "failed")

    def test_missing_compiled_pdf_remains_failed(self):
        output = self.collect(pdf=False)
        self.assertEqual(output.status, "failed")
        self.assertIsNone(output.paper_pdf_path)

    def test_soft_lint_notes_do_not_change_compiled_status(self):
        output = self.collect()
        self.assertIn("too short", output.notes)
        self.assertEqual(output.status, "compiled")

    def test_arbitrary_explicit_error_and_empty_error(self):
        self.assertEqual(self.collect("stopped midway").status, "failed")
        self.assertEqual(self.collect("").status, "compiled")


if __name__ == "__main__":
    unittest.main()
