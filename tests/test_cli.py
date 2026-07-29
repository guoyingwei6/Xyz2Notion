from xyz2notion import __version__
from xyz2notion.cli import main


def test_doctor_reports_installation(capsys: object) -> None:
    assert main(["doctor"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert f"Xyz2Notion {__version__}: OK" in output
    assert "5 credential types" in output


def test_help_is_default(capsys: object) -> None:
    assert main([]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "doctor" in output


def test_config_check_accepts_example(capsys: object) -> None:
    assert main(["config-check", "--config", "config.example.yaml"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Configuration OK" in output
    assert "tingwu_cookie, siliconflow" in output


def test_config_check_reports_missing_file(capsys: object) -> None:
    assert main(["config-check", "--config", "missing.yaml"]) == 2
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Configuration error" in error


def test_config_schema_command(capsys: object) -> None:
    assert main(["config-schema"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert '"schema_version"' in output


def test_notion_init_reports_missing_token(capsys: object, monkeypatch: object) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)  # type: ignore[attr-defined]
    monkeypatch.delenv("NOTION_PAGE_ID", raising=False)  # type: ignore[attr-defined]
    assert main(["notion-init"]) == 2
    error = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "Missing required credential" in error
