from jiuwenclaw.agentserver.skilldev.utils.skill_md_validation import validate_skill_md_content


def test_skill_md_rejects_non_kebab_case_name():
    content = "\n".join(
        [
            "---",
            "name: Joke_Teller",
            "description: ok",
            "---",
            "",
            "body",
            "",
        ]
    )
    err = validate_skill_md_content(content)
    assert err is not None
    assert "kebab-case" in err


def test_skill_md_rejects_angle_brackets_in_description():
    content = "\n".join(
        [
            "---",
            "name: joke-teller",
            "description: <bad>",
            "---",
            "",
            "body",
            "",
        ]
    )
    err = validate_skill_md_content(content)
    assert err is not None
    assert "angle brackets" in err


def test_skill_md_allows_dangerous_command_in_skill_md():
    content = "\n".join(
        [
            "---",
            "name: joke-teller",
            "description: ok",
            "---",
            "",
            "rm -rf /abc",
            "",
        ]
    )
    err = validate_skill_md_content(content)
    assert err is None


def test_skill_md_allows_utf8_bom_frontmatter():
    content = "\ufeff" + "\n".join(
        [
            "---",
            "name: joke-teller",
            "description: ok",
            "---",
            "",
            "body",
            "",
        ]
    )
    err = validate_skill_md_content(content)
    assert err is None


def test_skill_md_allows_crlf_frontmatter():
    content = "\r\n".join(
        [
            "---",
            "name: joke-teller",
            "description: ok",
            "---",
            "",
            "body",
            "",
        ]
    )
    err = validate_skill_md_content(content)
    assert err is None


def test_skill_md_rejects_short_password_assignment():
    content = "\n".join(
        [
            "---",
            "name: joke-teller",
            "description: ok",
            "---",
            "",
            "password:huawei@123",
            "",
        ]
    )
    err = validate_skill_md_content(content)
    assert err is not None
    assert "possible hardcoded credential" in err


def test_skill_md_allows_password_placeholder():
    content = "\n".join(
        [
            "---",
            "name: joke-teller",
            "description: ok",
            "---",
            "",
            "password:${PASSWORD}",
            "",
        ]
    )
    err = validate_skill_md_content(content)
    assert err is None
