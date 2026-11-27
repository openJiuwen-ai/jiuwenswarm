from jiuwenclaw.agentserver.skilldev.utils.static_security import (
    validate_scripts_file_content,
)


def test_scripts_file_rejects_dangerous_command():
    err = validate_scripts_file_content("echo hi\nrm -rf /abc\n", rel_path="my-skill/scripts/run.sh")
    assert err is not None
    assert "prohibited command pattern" in err


def test_scripts_file_rejects_short_password_assignment():
    err = validate_scripts_file_content("password:huawei@123\n", rel_path="my-skill/scripts/run.sh")
    assert err is not None
    assert "possible hardcoded credential" in err


def test_scripts_file_allows_password_placeholder():
    err = validate_scripts_file_content("password:${PASSWORD}\n", rel_path="my-skill/scripts/run.sh")
    assert err is None

