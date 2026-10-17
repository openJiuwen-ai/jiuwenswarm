from pathlib import Path

# Checked by path rather than by importing paper_pipeline, so the template files can land on their own.
TEMPLATE_DIR = (Path(__file__).resolve().parents[5] / "jiuwenswarm" / "agents" / "harness" / "common"
                / "paper_pipeline" / "templates" / "iclr2027")
TEMPLATE_FILES = ("iclr2027_conference.sty", "iclr2027_conference.bst", "fancyhdr.sty", "natbib.sty",
                  "math_commands.tex")


def test_template_files_are_bundled():
    assert all((TEMPLATE_DIR / name).is_file() for name in TEMPLATE_FILES)


def test_template_files_are_packaged():
    manifest = (TEMPLATE_DIR.parents[6] / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include jiuwenswarm/agents/harness/common/paper_pipeline/templates *" in manifest
