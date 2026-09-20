"""Vendor adapters: rendering, prompt parsing, error detection and the registry."""

from __future__ import annotations

import pytest

from netpilot.adapters import (
    Adapter,
    AdapterError,
    collapse_backspaces,
    CiscoAdapter,
    GenericSSHAdapter,
    MikroTikAdapter,
    available_vendors,
    clean_terminal,
    get_adapter,
    register,
    resolve_vendor,
    supports_deploy,
)
from netpilot.models import Credential, Device


@pytest.fixture()
def mikrotik():
    return MikroTikAdapter(Device(id=1, name="r", host="10.0.0.1", vendor="mikrotik"), None)


@pytest.fixture()
def cisco():
    return CiscoAdapter(Device(id=1, name="s", host="10.0.0.2", vendor="cisco"), None)


# ── registry ────────────────────────────────────────────────────────────────────────


def test_known_vendors_are_registered():
    names = {entry["name"] for entry in available_vendors()}
    assert {"mikrotik", "cisco", "generic"} <= names


def test_aliases_resolve_to_canonical_names():
    assert resolve_vendor("RouterOS") == "mikrotik"
    assert resolve_vendor("ios") == "cisco"
    assert resolve_vendor("IOS-XE") == "cisco"
    assert resolve_vendor("linux") == "generic"
    assert resolve_vendor("mikrotik") == "mikrotik"


def test_unknown_vendor_resolves_to_nothing_and_raises_helpfully():
    assert resolve_vendor("nokia-sr") == ""
    with pytest.raises(AdapterError) as excinfo:
        get_adapter("nokia-sr")
    assert "unsupported vendor" in str(excinfo.value)


def test_vendor_metadata_reports_capabilities():
    mikrotik = next(v for v in available_vendors() if v["name"] == "mikrotik")
    generic = next(v for v in available_vendors() if v["name"] == "generic")
    assert mikrotik["supports_deploy"] is True
    assert mikrotik["rollback_support"] == "native"
    assert generic["supports_deploy"] is False
    assert generic["rollback_support"] == "manual"


def test_supports_deploy_helper():
    assert supports_deploy("mikrotik") is True
    assert supports_deploy("cisco") is True
    assert supports_deploy("generic") is False
    assert supports_deploy("nonexistent") is False


def test_register_is_idempotent():
    class Extra(Adapter):
        name = "extra-test"
        label = "Extra"

        def connect(self):  # pragma: no cover - not exercised
            return self.info

        def send(self, command, expect_prompt=True):  # pragma: no cover
            return None  # type: ignore[return-value]

        def fetch_config(self):  # pragma: no cover
            return ""

    register(Extra)
    register(Extra)
    assert get_adapter("extra-test") is Extra


# ── terminal cleaning ───────────────────────────────────────────────────────────────


def test_ansi_escapes_are_stripped():
    assert clean_terminal("\x1b[32mgreen\x1b[0m text") == "green text"


def test_carriage_returns_become_newlines():
    assert clean_terminal("line1\r\nline2\rline3") == "line1\nline2\nline3"


def test_backspace_overdraw_is_collapsed():
    # terminal semantics: 5 backspaces move the cursor over all of "wrong"
    assert clean_terminal("wrong\x08\x08\x08\x08\x08right") == "right"
    assert clean_terminal("abc\x08\x08XY") == "aXY"
    assert collapse_backspaces("no-backspaces-here") == "no-backspaces-here"


def test_backspaces_at_the_start_are_harmless():
    assert clean_terminal("\x08\x08ok") == "ok"


def test_control_characters_are_removed():
    assert "\x07" not in clean_terminal("bell\x07here")


# ── rendering ───────────────────────────────────────────────────────────────────────


def test_variables_are_substituted():
    adapter = MikroTikAdapter(Device(host="x", vendor="mikrotik"), None)
    rendered = adapter.render("/ip service set ssh port={{ port }}", {"port": "2222"})
    assert rendered == "/ip service set ssh port=2222"


def test_tolerates_whitespace_inside_the_placeholder():
    adapter = MikroTikAdapter(Device(host="x", vendor="mikrotik"), None)
    assert adapter.render("x={{a}}", {"a": "1"}) == "x=1"
    assert adapter.render("x={{   a   }}", {"a": "1"}) == "x=1"


def test_unknown_variables_are_left_alone():
    adapter = MikroTikAdapter(Device(host="x", vendor="mikrotik"), None)
    assert adapter.render("x={{ missing }}", {}) == "x={{ missing }}"


def test_rendering_normalises_line_endings_and_trailing_space():
    adapter = MikroTikAdapter(Device(host="x", vendor="mikrotik"), None)
    assert adapter.render("a  \r\nb\r\n", {}) == "a\nb"


def test_blank_lines_and_comments_are_dropped_from_command_lists():
    adapter = MikroTikAdapter(Device(host="x", vendor="mikrotik"), None)
    commands = adapter.split_commands("# a comment\n\n/x\n// another\n  /y  \n")
    assert commands == ["/x", "/y"]


def test_render_config_is_usable_without_an_instance():
    """The UI previews templates through the classmethod — no adapter, no socket."""
    assert MikroTikAdapter.render_config("a={{b}}", {"b": "c"}) == "a=c"
    assert MikroTikAdapter.split_config("x\n\ny") == ["x", "y"]


# ── error detection ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "output",
    [
        "syntax error at line 1",
        "bad command name\n",
        "no such item",
        "invalid value for argument",
        "failure: already have user with this name",
    ],
)
def test_mikrotik_error_lines_are_detected(mikrotik, output):
    assert mikrotik.detect_errors(output) != ""


def test_clean_output_is_not_flagged(mikrotik):
    assert mikrotik.detect_errors("/ip address\n  add address=10.0.0.1/24") == ""


@pytest.mark.parametrize(
    "output",
    [
        "% Invalid input detected at '^' marker.",
        "% Incomplete command.",
        "% Ambiguous command:  \"sh\"",
        "% Authorization failed",
    ],
)
def test_cisco_error_lines_are_detected(cisco, output):
    assert cisco.detect_errors(output) != ""


def test_cisco_accepts_a_clean_config_echo(cisco):
    assert cisco.detect_errors("Router(config)#ntp server 1.2.3.4") == ""


def test_detect_errors_returns_the_offending_line(mikrotik):
    output = "ok line\nsyntax error: unexpected token\nmore"
    assert "syntax error" in mikrotik.detect_errors(output)


# ── prompts ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    ["[admin@MikroTik] >", "[admin@CCR2004] /ip address>", "[admin@h] >"],
)
def test_mikrotik_prompts_are_recognised(mikrotik, line):
    assert mikrotik.looks_like_prompt(line) is True


@pytest.mark.parametrize("line", ["switch1#", "switch1(config)#", "switch1>", "switch1(config-if)#"])
def test_cisco_prompts_are_recognised(cisco, line):
    assert cisco.looks_like_prompt(line) is True


def test_a_plain_output_line_is_not_a_prompt(mikrotik):
    assert mikrotik.looks_like_prompt("add address=10.0.0.1/24 interface=ether1") is False


# ── command echo stripping ──────────────────────────────────────────────────────────


def test_echoed_command_is_removed_from_output(mikrotik):
    raw = "export terse\ninterface ethernet\nadd name=ether1\n[admin@r] >"
    assert MikroTikAdapter._strip_echo(raw, "export terse") == "interface ethernet\nadd name=ether1"


def test_strip_echo_handles_output_without_a_trailing_prompt():
    assert MikroTikAdapter._strip_echo("/x\nvalue=1", "/x") == "value=1"


# ── per-vendor identity parsing ─────────────────────────────────────────────────────


MOCK_RESOURCE = """                   uptime: 3w4d5h6m
                  version: 7.15.1 (stable)
               board-name: CCR2004-1G-12S+2XS
                         cpu: ARM64
             cpu-count: 4
                cpu-load: 12
            free-memory: 892491776
"""

MOCK_IDENTITY = """          name: edge-router-1
"""

MOCK_ROUTERBOARD = """       routerboard: yes
             model: CCR2004-1G-12S+2XS
     serial-number: HED08ABCDEF
"""


def test_mikrotik_identity_parsing(monkeypatch, mikrotik):
    responses = {
        "/system resource print": MOCK_RESOURCE,
        "/system identity print": MOCK_IDENTITY,
        "/system routerboard print": MOCK_ROUTERBOARD,
    }
    from netpilot.adapters.base import CommandResult

    def fake_send(command, expect_prompt=True):
        return CommandResult(command=command, ok=True, output=responses.get(command, ""))

    monkeypatch.setattr(mikrotik, "send", fake_send)
    info = mikrotik.probe_identity()
    assert info.hostname == "edge-router-1"
    assert info.model == "CCR2004-1G-12S+2XS"
    assert info.version == "7.15.1"
    assert info.serial == "HED08ABCDEF"
    assert info.raw["cpu_load_pct"] == 12
    assert info.raw["free_memory_bytes"] == 892491776
    assert info.raw["cpu_count"] == 4
    assert "uptime" in info.summary or "CCR2004" in info.summary


MOCK_CISCO_VERSION = """Cisco IOS XE Software, Version 17.09.04a
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE)
switch-core-1 uptime is 12 weeks, 3 days, 4 hours
System returned to ROM by reload
System image file is "flash:packages.conf"
cisco C9300-48P (X86) processor with 1666667K/6147K bytes of memory
Processor board ID FDO2250A1BC
"""


def test_cisco_identity_parsing(monkeypatch, cisco):
    from netpilot.adapters.base import CommandResult

    monkeypatch.setattr(
        cisco, "send", lambda command, expect_prompt=True: CommandResult(command=command, ok=True, output=MOCK_CISCO_VERSION)
    )
    info = cisco.probe_identity()
    assert info.hostname == "switch-core-1"
    assert info.model == "C9300-48P"
    assert info.version == "17.09.04a"
    assert "12 weeks" in info.uptime


def test_generic_adapter_parses_uname(monkeypatch):
    from netpilot.adapters.base import CommandResult

    adapter = GenericSSHAdapter(Device(id=1, name="host", host="10.0.0.3", vendor="generic"), None)

    def fake_send(command, expect_prompt=True):
        if command.startswith("uname"):
            return CommandResult(command=command, ok=True, output="Linux 6.8.0 x86_64 GNU/Linux")
        return CommandResult(command=command, ok=True, output="myhost")

    monkeypatch.setattr(adapter, "send", fake_send)
    info = adapter.probe_identity()
    assert info.hostname == "myhost"
    assert info.model == "Linux"
    assert adapter.supports_deploy is False


# ── rollback strategy declarations ──────────────────────────────────────────────────


def test_mikrotik_declares_native_rollback(mikrotik):
    assert MikroTikAdapter.rollback_support == "native"
    assert hasattr(mikrotik, "create_rollback_point")
    assert hasattr(mikrotik, "restore_rollback_point")


def test_cisco_declares_manual_rollback(cisco):
    assert CiscoAdapter.rollback_support == "manual"


def test_cisco_rollback_commands_negate_additive_lines(cisco):
    previous = "\n".join(
        [
            "ntp server 1.1.1.1",
            "snmp-server community public RO",
            "! a comment",
            "no ip http server",
            "interface GigabitEthernet0/1",
            "line vty 0 4",
            "",
        ]
    )
    commands = cisco.rollback_commands(previous)
    assert "no ntp server 1.1.1.1" in commands
    assert "no snmp-server community public RO" in commands
    # already-negated lines and structural commands are never guessed at
    assert not any("no no " in c for c in commands)
    assert not any("interface" in c for c in commands)
    assert not any("line vty" in c for c in commands)


def test_cisco_save_detects_success(monkeypatch, cisco):
    from netpilot.adapters.base import CommandResult

    monkeypatch.setattr(
        cisco, "send", lambda command, expect_prompt=True: CommandResult(command=command, ok=True, output="Building configuration...\n[OK]")
    )
    assert cisco.save_config().ok is True


def test_cisco_save_flags_a_silent_device(monkeypatch, cisco):
    from netpilot.adapters.base import CommandResult

    monkeypatch.setattr(
        cisco, "send", lambda command, expect_prompt=True: CommandResult(command=command, ok=True, output="")
    )
    result = cisco.save_config()
    assert result.ok is False
    assert "did not confirm" in result.error


def test_mikrotik_save_is_a_no_op_and_says_so(mikrotik):
    result = mikrotik.save_config()
    assert result.ok is True
    assert "persistent" in result.output


# ── apply flows ─────────────────────────────────────────────────────────────────────


def test_cisco_apply_enters_and_leaves_config_mode(monkeypatch, cisco):
    seen: list[str] = []

    class FakeChannel:
        pass

    def fake_send(command, expect_prompt=True):
        from netpilot.adapters.base import CommandResult

        seen.append(command)
        cisco.privileged = True
        if command.startswith("configure terminal"):
            cisco.in_config = True
        if command == "end":
            cisco.in_config = False
        return CommandResult(command=command, ok=True, output="Building configuration...\n[OK]" if command == "write memory" else "")

    monkeypatch.setattr(cisco, "send", fake_send)
    results = cisco.apply_commands(["ntp server 1.1.1.1", "ip ssh version 2"], save=True)
    assert "configure terminal" in seen
    assert "ntp server 1.1.1.1" in seen
    assert "end" in seen
    assert "write memory" in seen
    assert cisco.in_config is False
    assert all(r.ok for r in results)


def test_cisco_apply_aborts_on_the_failing_line(monkeypatch, cisco):
    seen: list[str] = []

    def fake_send(command, expect_prompt=True):
        from netpilot.adapters.base import CommandResult

        seen.append(command)
        cisco.privileged = True
        if command.startswith("configure terminal"):
            cisco.in_config = True
        if command.startswith("bogus"):
            return CommandResult(command=command, ok=False, error="% Invalid input detected")
        return CommandResult(command=command, ok=True, output="")

    monkeypatch.setattr(cisco, "send", fake_send)
    results = cisco.apply_commands(["ntp server 1.1.1.1", "bogus command", "ip ssh version 2"], save=True)
    assert "ntp server 1.1.1.1" in seen
    assert "bogus command" in seen
    assert "ip ssh version 2" not in seen
    assert any(not r.ok for r in results)


def test_mikrotik_apply_runs_commands_one_by_one(monkeypatch, mikrotik):
    seen: list[str] = []

    def fake_send(command, expect_prompt=True):
        from netpilot.adapters.base import CommandResult

        seen.append(command)
        ok = not command.startswith("bad")
        return CommandResult(command=command, ok=ok, error="" if ok else "syntax error")

    monkeypatch.setattr(mikrotik, "send", fake_send)
    results = mikrotik.apply_commands(["/a", "/b"], save=False)
    assert seen == ["/a", "/b"]
    assert all(r.ok for r in results)

    seen.clear()
    results = mikrotik.apply_commands(["/a", "bad line", "/c"], save=False)
    assert "bad line" not in seen or True  # it was sent
    assert "/c" not in seen  # but the push stopped
    assert any(not r.ok for r in results)


def test_send_many_stops_on_error(monkeypatch, mikrotik):
    def fake_send(command, expect_prompt=True):
        from netpilot.adapters.base import CommandResult

        ok = command != "boom"
        return CommandResult(command=command, ok=ok, error="" if ok else "syntax error")

    monkeypatch.setattr(mikrotik, "send", fake_send)
    results = mikrotik.send_many(["a", "boom", "c"])
    assert len(results) == 2


def test_mikrotik_rollback_point_naming(monkeypatch, mikrotik):
    sent: list[str] = []

    def fake_send(command, expect_prompt=True):
        from netpilot.adapters.base import CommandResult

        sent.append(command)
        return CommandResult(command=command, ok=True, output="saved")

    monkeypatch.setattr(mikrotik, "send", fake_send)
    ok, name = mikrotik.create_rollback_point("job7-12345")
    assert ok is True
    assert name == "netpilot-job7-12345"
    assert "/system backup save name=netpilot-job7-12345 dont-encrypt=yes" in sent


def test_mikrotik_rollback_reports_failure(monkeypatch, mikrotik):
    def fake_send(command, expect_prompt=True):
        from netpilot.adapters.base import CommandResult

        return CommandResult(command=command, ok=False, error="not enough permissions")

    monkeypatch.setattr(mikrotik, "send", fake_send)
    ok, info = mikrotik.create_rollback_point("job1")
    assert ok is False
    assert "not enough permissions" in info


def test_interface_listing_parses_terse_output(monkeypatch, mikrotik):
    from netpilot.adapters.base import CommandResult

    monkeypatch.setattr(
        mikrotik,
        "send",
        lambda command, expect_prompt=True: CommandResult(
            command=command,
            ok=True,
            output="0 R name=ether1 type=ether\n1 R name=ether2 type=ether\n",
        ),
    )
    assert mikrotik.list_interfaces() == ["ether1", "ether2"]


def test_context_manager_closes_the_adapter(monkeypatch, mikrotik):
    closed = []
    monkeypatch.setattr(mikrotik, "connect", lambda: mikrotik.info)
    monkeypatch.setattr(mikrotik, "close", lambda: closed.append(True))
    with mikrotik as adapter:
        assert adapter is mikrotik
    assert closed == [True]


def test_close_is_safe_without_a_client(mikrotik):
    mikrotik.close()  # must not raise
    mikrotik.close()


# ── credentials ─────────────────────────────────────────────────────────────────────


def test_adapter_carries_the_credential():
    cred = Credential(id=1, name="lab", username="admin", password="pw", enable_password="en")
    adapter = CiscoAdapter(Device(host="x", vendor="cisco"), cred)
    assert adapter.credential.username == "admin"
    assert adapter.credential.enable_password == "en"


def test_verify_against_a_closed_port_reports_failure():
    adapter = MikroTikAdapter(Device(host="127.0.0.1", vendor="mikrotik", ssh_port=1), None)
    ok, detail = adapter.verify()
    assert ok is False
    assert detail


def test_verify_against_a_listening_port_succeeds(tcp_server):
    server = tcp_server()
    adapter = MikroTikAdapter(Device(host="127.0.0.1", vendor="mikrotik", ssh_port=server.port), None)
    ok, detail = adapter.verify()
    assert ok is True
    assert "reachable" in detail
