"""CLI tests: safe settings defaults from the environment, the argument
parser, and run --once exit codes."""

import psycopg
import pytest

from cli import Settings, build_parser, cmd_run, main
from newsbot.runner import ExecutionResult, RunStats
from tests.conftest import SPEC_FIXTURE

ALL_ENV_VARS = (
    "SPEC_JSON",
    "SPEC_URL",
    "SPEC_PATH",
    "SPEC_BASE_DIR",
    "DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "DRY_RUN",
    "LOG_LEVEL",
    "TRANSLATE_PROVIDER",
    "TRANSLATE_API_KEY",
    "HEALTHCHECK_URL",
    "TELEGRAM_WEBHOOK_SECRET",
    "WEBHOOK_URL",
    "PORT",
)


def clear_env(monkeypatch):
    for var in ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_settings_defaults_are_safe(monkeypatch):
    clear_env(monkeypatch)
    settings = Settings.from_env()
    assert settings.dry_run is True
    assert settings.translate_provider == "none"
    # No spec source is configured by default — missing_env reports it
    # instead of a dangling path masking the three-option error.
    assert settings.spec_path == ""
    assert settings.spec_json == ""
    assert settings.spec_url == ""


def test_dry_run_env_parsing(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    assert Settings.from_env().dry_run is False
    monkeypatch.setenv("DRY_RUN", "TRUE")
    assert Settings.from_env().dry_run is True


def test_parser_knows_all_commands():
    parser = build_parser()
    assert parser.parse_args(["run", "--once"]).once is True
    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["register-webhook"]).command == "register-webhook"
    assert parser.parse_args(["fetch", "swissinfo", "--dry-run"]).source == "swissinfo"
    args = parser.parse_args(["export", "--from", "2026-08-01", "--to", "2026-09-01"])
    assert args.format == "csv"


def test_unknown_command_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["frobnicate"])


# --- run --once exit codes --------------------------------------------------


def _run_once_with(monkeypatch, pg_dsn, fake_execution) -> int:
    monkeypatch.setattr("newsbot.runner.run_execution", fake_execution)
    settings = Settings(spec_path=SPEC_FIXTURE, database_url=pg_dsn, dry_run=True)
    return cmd_run(settings, build_parser().parse_args(["run", "--once"]))


def test_once_exits_zero_on_feed_errors(db, pg_dsn, monkeypatch):
    """A failing feed raised an error event and the run carried on — the
    schedule must not stop."""

    async def fake(deps):
        return ExecutionResult(acquired=True, stats=RunStats(errors=["blick: 404"]))

    assert _run_once_with(monkeypatch, pg_dsn, fake) == 0


def test_once_exits_zero_on_unexpected_crash(db, pg_dsn, monkeypatch):
    async def fake(deps):
        raise RuntimeError("unexpected")

    assert _run_once_with(monkeypatch, pg_dsn, fake) == 0


def test_once_exits_nonzero_when_database_lost(db, pg_dsn, monkeypatch):
    async def fake(deps):
        raise psycopg.OperationalError("server closed the connection")

    assert _run_once_with(monkeypatch, pg_dsn, fake) == 1


def test_once_exits_nonzero_when_database_unreachable(monkeypatch):
    settings = Settings(
        spec_path=SPEC_FIXTURE,
        database_url="postgresql://nobody@localhost:1/nowhere",
        dry_run=True,
    )
    with pytest.raises(SystemExit, match="DATABASE UNREACHABLE"):
        cmd_run(settings, build_parser().parse_args(["run", "--once"]))


# --- startup env validation ---------------------------------------------------


def test_run_names_every_missing_variable_at_once(monkeypatch, capsys):
    """A bare `run` must report DATABASE_URL and the spec sources together,
    not fail on the first one."""
    clear_env(monkeypatch)
    assert main(["run", "--once"]) == 2
    err = capsys.readouterr().err
    assert "DATABASE_URL" in err
    for var in ("SPEC_JSON", "SPEC_URL", "SPEC_PATH"):
        assert var in err
    assert "spec.example.json" in err


def test_register_webhook_names_all_requirements(monkeypatch, capsys):
    clear_env(monkeypatch)
    assert main(["register-webhook"]) == 2
    err = capsys.readouterr().err
    for var in ("TELEGRAM_BOT_TOKEN", "WEBHOOK_URL", "TELEGRAM_WEBHOOK_SECRET", "SPEC_PATH"):
        assert var in err


def test_bad_port_is_reported_with_the_variable_name(monkeypatch, capsys):
    clear_env(monkeypatch)
    monkeypatch.setenv("PORT", "eight thousand")
    assert main(["stats"]) == 2
    err = capsys.readouterr().err
    assert "PORT" in err


def test_validate_with_explicit_spec_needs_no_env(monkeypatch, capsys):
    clear_env(monkeypatch)
    assert main(["validate", "--spec", SPEC_FIXTURE]) == 0
    out = capsys.readouterr().out
    assert "OK:" in out
    assert "archive-only" not in out  # every fixture source is bound


def test_validate_names_archive_only_sources(monkeypatch, capsys, tmp_path):
    """An unbound source would otherwise print a header and nothing else,
    which reads as a rendering bug rather than a deliberate config."""
    import json
    from pathlib import Path

    spec = json.loads(Path(SPEC_FIXTURE).read_text())
    spec["sources"][0]["channels"] = []
    spec["sources"][0]["cold_start_policy"] = "skip_all"
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))

    clear_env(monkeypatch)
    assert main(["validate", "--spec", str(path)]) == 0
    out = capsys.readouterr().out
    assert "(archive only — stored, never posted)" in out
    assert "(1 archive-only)" in out


def test_stats_does_not_require_spec(monkeypatch, capsys):
    clear_env(monkeypatch)
    assert main(["stats"]) == 2
    err = capsys.readouterr().err
    assert "DATABASE_URL" in err
    assert "SPEC_JSON" not in err
