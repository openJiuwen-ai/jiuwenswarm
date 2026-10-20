import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DESIGNER_SKILLS = "server/runtime/designer/skills"


def test_designer_skills_are_declared_as_package_data():
    """A wheel must ship the Designer skill corpus.

    The runtime resolves the corpus as
    ``jiuwenswarm/server/runtime/designer/skills`` next to the package
    (``designer.paths.skills_dir``); a wheel built without these files makes
    every ``skills_loader`` lookup return an empty string.
    """
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["jiuwenswarm"]
    assert f"{DESIGNER_SKILLS}/*" in patterns
    assert f"{DESIGNER_SKILLS}/**/*" in patterns

    manifest = (PROJECT_ROOT / "MANIFEST.in").read_text()
    assert f"recursive-include jiuwenswarm/{DESIGNER_SKILLS} *" in manifest


def test_designer_skills_corpus_is_present():
    skills = PROJECT_ROOT / "jiuwenswarm" / DESIGNER_SKILLS
    assert (skills / "index.json").is_file()
    assert (skills / "orchestration" / "director.md").is_file()
    assert len(list(skills.rglob("*.md"))) >= 20
