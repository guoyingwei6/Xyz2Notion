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
