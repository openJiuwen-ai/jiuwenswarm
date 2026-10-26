import pytest


def test_project_invalid_escape_remains_an_error() -> None:
    source = r'''PROJECT_PATTERN = "\."'''

    # Compile-time invalid escapes become SyntaxError once the matching warning
    # is treated as an error (SyntaxWarning on 3.12+, DeprecationWarning on 3.11
    # via pytest.ini).
    with pytest.raises(SyntaxError, match="invalid escape sequence"):
        compile(source, "jiuwenswarm/warning_probe.py", "exec")
