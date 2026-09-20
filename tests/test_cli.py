"""The command line interface."""

from __future__ import annotations

import json
import time

import pytest

from netpilot.cli import build_parser, main


@pytest.fixture()
def data_dir(tmp_path):
    return str(tmp_path / "cli")


def run(*args, data_dir=None):
    """Invoke the CLI exactly as the console script would."""
    argv = list(args)
    if data_dir:
        argv = ["--data-dir", data_dir] + argv
    return main(argv)


# ── parser ──────────────────────────────────────────────────────────────────────────


def test_parser_has_all_documented_commands():
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(actions[0].choices)  # type: ignore[union-attr]
    assert {
        "doctor",
        "devices",
        "monitor",
        "deploy",
        "backup",
        "templates",
        "events",
        "web",
        "gui",
        "version",
    } <= commands


def test_no_arguments_prints_help(capsys):
    assert main([]) == 0
    assert "usage: netpilot" in capsys.readouterr().out


def test_version_command(capsys):
    assert run("version") == 0
    assert "netpilot" in capsys.readouterr().out


# ── doctor ──────────────────────────────────────────────────────────────────────────


def test_doctor_reports_every_check(data_dir, capsys):
    code = run("doctor", data_dir=data_dir)
    output = capsys.readouterr().out
    for name in ("ICMP", "SNMP", "SSH", "Storage", "Secrets"):
        assert name in output
    assert code in (0, 1)


def test_doctor_json_output_is_machine_readable(data_dir, capsys):
    run("doctor", "--json", data_dir=data_dir)
    payload = json.loads(capsys.readouterr().out)
    assert {entry["check"] for entry in payload} >= {"ICMP", "Storage", "Secrets"}
    assert all("ok" in entry for entry in payload)


def test_doctor_explains_an_icmp_failure(data_dir, capsys):
    run("doctor", data_dir=data_dir)
    output = capsys.readouterr().out
    if "[FAIL] ICMP" in output:
        assert "fallback" in output


# ── devices ─────────────────────────────────────────────────────────────────────────


def test_devices_empty(data_dir, capsys):
    assert run("devices", data_dir=data_dir) == 0
    assert "(none)" in capsys.readouterr().out


def test_add_a_device_and_list_it(data_dir, capsys):
    assert run("devices", "--add", "127.0.0.1", "--name", "local", "--vendor", "mikrotik",
               "--tag", "lab,core", data_dir=data_dir) == 0
    output = capsys.readouterr().out
    assert "added device #1" in output
    assert "monitor(s) attached automatically" in output
    assert "being monitored" in output

    run("devices", data_dir=data_dir)
    listing = capsys.readouterr().out
    assert "local" in listing
    assert "127.0.0.1" in listing
    assert "lab,core" in listing


def test_add_device_json(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--name", "local", "--json", data_dir=data_dir)
    capsys.readouterr()
    run("devices", "--json", data_dir=data_dir)
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "local"
    assert payload[0]["state"] == "unknown"


def test_duplicate_device_exits_with_a_message(data_dir, capsys):
    run("devices", "--add", "10.0.0.1", data_dir=data_dir)
    capsys.readouterr()
    assert run("devices", "--add", "10.0.0.1", data_dir=data_dir) == 2
    assert "already in the inventory" in capsys.readouterr().out


def test_unknown_credential_is_reported(data_dir, capsys):
    code = run("devices", "--add", "10.0.0.1", "--credential", "nope", data_dir=data_dir)
    assert code == 2
    assert "no credential named" in capsys.readouterr().out


def test_search_filters_the_listing(data_dir, capsys):
    run("devices", "--add", "10.0.0.1", "--name", "alpha", data_dir=data_dir)
    run("devices", "--add", "10.0.0.2", "--name", "beta", data_dir=data_dir)
    capsys.readouterr()
    run("devices", "--search", "alpha", data_dir=data_dir)
    output = capsys.readouterr().out
    assert "alpha" in output
    assert "beta" not in output


# ── templates ───────────────────────────────────────────────────────────────────────


def test_templates_lists_the_library(data_dir, capsys):
    assert run("templates", data_dir=data_dir) == 0
    output = capsys.readouterr().out
    assert "NTP servers" in output
    assert "built-in" in output


def test_templates_filters_by_vendor(data_dir, capsys):
    run("templates", "--vendor", "cisco", data_dir=data_dir)
    output = capsys.readouterr().out
    assert "cisco" in output
    assert "mikrotik" not in output


def test_template_show_prints_the_body(data_dir, capsys):
    assert run("templates", "--show", "NTP servers", data_dir=data_dir) == 0
    output = capsys.readouterr().out
    assert "ntp" in output.lower()


def test_template_show_unknown_name(data_dir, capsys):
    assert run("templates", "--show", "nope", data_dir=data_dir) == 2
    assert "no template named" in capsys.readouterr().out


def test_templates_json(data_dir, capsys):
    run("templates", "--json", data_dir=data_dir)
    payload = json.loads(capsys.readouterr().out)
    assert any(t["name"] == "NTP servers" for t in payload)
    assert all("builtin" in t for t in payload)


# ── deploy dry run ──────────────────────────────────────────────────────────────────


def test_deploy_without_anything_to_send(data_dir, capsys):
    assert run("deploy", data_dir=data_dir) == 2
    assert "nothing to deploy" in capsys.readouterr().out


def test_deploy_with_unknown_template(data_dir, capsys):
    run("devices", "--add", "10.0.0.1", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    assert run("deploy", "--template", "nope", data_dir=data_dir) == 2
    assert "no template named" in capsys.readouterr().out


def test_deploy_dry_run_reports_the_plan(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--name", "r1", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    code = run("deploy", "--template", "NTP servers", "--device", "1", "--dry-run",
               data_dir=data_dir)
    output = capsys.readouterr().out
    assert "job #1" in output
    assert "DRY RUN" in output
    assert "dry run: nothing will be written" in output
    assert code == 0


def test_deploy_dry_run_json_is_structured(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--name", "r1", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    run("deploy", "--template", "NTP servers", "--device", "1", "--dry-run", "--json",
        data_dir=data_dir)
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["targets"][0]["commands"]
    assert "backup" not in payload["targets"][0]


def test_deploy_variable_overrides_are_applied(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    run("deploy", "--template", "NTP servers", "--var", "ntp1=10.9.9.9", "--device", "1",
        "--dry-run", "--json", data_dir=data_dir)
    payload = json.loads(capsys.readouterr().out)
    commands = payload["targets"][0]["commands"]
    assert any("10.9.9.9" in command for command in commands)


def test_deploy_to_a_monitoring_only_device_is_refused(data_dir, capsys):
    run("devices", "--add", "10.0.0.1", "--vendor", "generic", data_dir=data_dir)
    capsys.readouterr()
    assert run("deploy", "--body", "ls", "--vendor", "generic", "--device", "1",
               data_dir=data_dir) == 1
    assert "support configuration push" in capsys.readouterr().err


def test_deploy_with_no_matching_devices(data_dir, capsys):
    run("devices", "--add", "10.0.0.1", "--vendor", "mikrotik", "--tag", "edge", data_dir=data_dir)
    capsys.readouterr()
    assert run("deploy", "--body", "/x", "--vendor", "mikrotik", "--tag", "core",
               data_dir=data_dir) == 1
    assert "no devices matched" in capsys.readouterr().err


# ── events & backups ────────────────────────────────────────────────────────────────


def test_events_listing(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--name", "r1", data_dir=data_dir)
    capsys.readouterr()
    assert run("events", data_dir=data_dir) == 0
    output = capsys.readouterr().out
    assert "monitor(s) attached" in output


def test_events_json_and_filters(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--name", "r1", data_dir=data_dir)
    capsys.readouterr()
    run("events", "--json", data_dir=data_dir)
    payload = json.loads(capsys.readouterr().out)
    assert payload and payload[0]["kind"] == "device"

    run("events", "--json", "--severity", "critical", data_dir=data_dir)
    assert json.loads(capsys.readouterr().out) == []


def test_backup_listing_is_empty_initially(data_dir, capsys):
    assert run("backup", "--list", data_dir=data_dir) == 0
    assert "(none)" in capsys.readouterr().out


def test_job_json_reports_skipped_targets(data_dir, capsys):
    """Skipped is a real outcome and has to be visible next to ok/failed."""
    run("devices", "--add", "127.0.0.1", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    run("deploy", "--template", "NTP servers", "--device", "1", "--dry-run", "--json",
        data_dir=data_dir)
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 0
    assert payload["succeeded"] == 1


def test_backup_listing_json(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--name", "r1", data_dir=data_dir)
    capsys.readouterr()
    run("backup", "--list", "--json", data_dir=data_dir)
    assert json.loads(capsys.readouterr().out) == []


def test_backup_of_no_matching_devices(data_dir, capsys):
    assert run("backup", "--tag", "nothing", data_dir=data_dir) == 2
    assert "no devices matched" in capsys.readouterr().out


# ── exit codes ──────────────────────────────────────────────────────────────────────


def test_unknown_command_exits_nonzero():
    with pytest.raises(SystemExit) as excinfo:
        main(["nonsense"])
    assert excinfo.value.code == 2


def test_json_output_is_always_parseable(data_dir, capsys):
    for command in (
        ["devices", "--json"],
        ["templates", "--json"],
        ["events", "--json"],
        ["backup", "--list", "--json"],
        ["doctor", "--json"],
    ):
        run(*command, data_dir=data_dir)
        raw = capsys.readouterr().out
        assert json.loads(raw) is not None, command


def test_template_defaults_survive_the_cli_path(data_dir, capsys):
    """Built-in templates have no id; resolving by name must still apply their defaults."""
    run("devices", "--add", "127.0.0.1", "--name", "r1", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    run("deploy", "--template", "NTP servers", "--device", "1", "--dry-run", "--json",
        data_dir=data_dir)
    payload = json.loads(capsys.readouterr().out)
    commands = payload["targets"][0]["commands"]
    assert any("pool.ntp.org" in command for command in commands)
    assert not any("{{" in command for command in commands)


def test_deploy_requires_an_explicit_target_selector(data_dir, capsys):
    """`deploy` with no selector must refuse rather than fan out to the whole inventory."""
    run("devices", "--add", "127.0.0.1", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    code = run("deploy", "--template", "NTP servers", "--dry-run", data_dir=data_dir)
    output = capsys.readouterr().out
    assert code == 2
    assert "no targets selected" in output
    assert "--all" in output


def test_deploy_all_is_an_explicit_choice(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--vendor", "mikrotik", data_dir=data_dir)
    run("devices", "--add", "127.0.0.2", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    run("deploy", "--template", "NTP servers", "--all", "--dry-run", "--json", data_dir=data_dir)
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 2


def test_backup_without_a_selection_means_everything(data_dir, capsys):
    """A backup only reads, so the friendly default is allowed here."""
    run("devices", "--add", "127.0.0.1", "--vendor", "generic", data_dir=data_dir)
    capsys.readouterr()
    run("backup", "--json", data_dir=data_dir)
    output = capsys.readouterr().out
    assert json.loads(output) is not None, "backup must emit valid JSON"


def test_cli_json_stdout_is_pure_json(data_dir, capsys):
    """A shell pipeline doing `netpilot ... --json | jq` must not choke on banner text."""
    run("devices", "--add", "127.0.0.1", "--name", "r1", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    run("deploy", "--template", "NTP servers", "--device", "1", "--dry-run", "--json",
        data_dir=data_dir)
    raw = capsys.readouterr().out
    assert raw.lstrip().startswith("{")
    assert json.loads(raw)["total"] == 1


def test_json_flag_works_before_and_after_the_subcommand(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--name", "r1", data_dir=data_dir)
    capsys.readouterr()

    run("devices", "--json", data_dir=data_dir)
    after = json.loads(capsys.readouterr().out)

    run("--json", "devices", data_dir=data_dir)
    before = json.loads(capsys.readouterr().out)

    assert after == before


def test_deploy_by_template_name_without_repeating_the_body(data_dir, capsys):
    run("devices", "--add", "127.0.0.1", "--vendor", "mikrotik", data_dir=data_dir)
    capsys.readouterr()
    run("deploy", "--template", "NTP servers", "--device", "1", "--dry-run", "--json",
        data_dir=data_dir)
    job = json.loads(capsys.readouterr().out)
    assert job["targets"][0]["commands"]
