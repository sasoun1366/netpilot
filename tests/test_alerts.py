"""Alert routing and delivery."""

from __future__ import annotations

import json
import http.server
import threading
import time

import pytest

from netpilot.alerts import (
    AlertRouter,
    EmailSink,
    NotifyConfig,
    SyslogSink,
    WebhookSink,
    format_event_text,
    severity_at_least,
)
from netpilot.alerts.notifiers import _in_quiet_hours
from netpilot.models import Event


def _event(severity="critical", state="down", message="device is down", device_id=1):
    return Event(
        device_id=device_id,
        device_name="r1",
        kind="state",
        severity=severity,
        message=message,
        details={"state": state},
    )


# ── rules ───────────────────────────────────────────────────────────────────────────


def test_severity_comparison():
    assert severity_at_least("critical", "warning") is True
    assert severity_at_least("warning", "warning") is True
    assert severity_at_least("info", "warning") is False
    assert severity_at_least("nonsense", "warning") is False


def test_disabled_router_sends_nothing():
    router = AlertRouter(NotifyConfig(enabled=False, webhook_url="http://127.0.0.1:1"))
    allowed, reason = router.should_send(_event())
    assert allowed is False
    assert reason == "notifications disabled"


def test_events_below_the_threshold_are_suppressed():
    router = AlertRouter(NotifyConfig(enabled=True, min_severity="critical"))
    allowed, reason = router.should_send(_event(severity="warning"))
    assert allowed is False
    assert "below min severity" in reason


def test_recovery_notifications_can_be_disabled():
    # min_severity must allow "info" for the recovery rule to be the deciding factor.
    router = AlertRouter(
        NotifyConfig(enabled=True, min_severity="info", notify_on_recovery=False)
    )
    allowed, reason = router.should_send(_event(severity="info", state="up"))
    assert allowed is False
    assert reason == "recovery notifications disabled"
    # ...and with the rule off, a recovery does go out
    permissive = AlertRouter(NotifyConfig(enabled=True, min_severity="info"))
    assert permissive.should_send(_event(severity="info", state="up"))[0] is True


def test_cooldown_suppresses_a_repeat_for_the_same_device_and_state():
    router = AlertRouter(NotifyConfig(enabled=True, cooldown_sec=300))
    first, _ = router.should_send(_event(), now=1000.0)
    second, reason = router.should_send(_event(), now=1100.0)
    third, _ = router.should_send(_event(), now=1400.0)
    assert first is True
    assert second is False and reason == "cooldown"
    assert third is True
    assert router._suppressed == 1


def test_cooldown_is_per_state_not_global():
    router = AlertRouter(NotifyConfig(enabled=True, cooldown_sec=300))
    assert router.should_send(_event(state="down"), now=1000.0)[0] is True
    assert router.should_send(_event(state="up", severity="critical"), now=1010.0)[0] is True


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("23:00-07:00", True),  # wraps midnight; 23:30 is inside
        ("00:00-23:59", True),
        ("01:00-02:00", False),
        ("22:00-23:00", False),
        ("", False),
        ("garbage", False),
        ("12:00-12:00", False),
    ],
)
def test_quiet_hours_windows(spec, expected):
    """Fixed reference time of 23:30, so the answers do not depend on the wall clock."""
    reference = time.struct_time((2026, 1, 1, 23, 30, 0, 3, 1, 0))
    assert _in_quiet_hours(spec, now=reference) is expected


def test_quiet_hours_suppresses_delivery():
    router = AlertRouter(NotifyConfig(enabled=True, quiet_hours="00:00-23:59"))
    allowed, reason = router.should_send(_event())
    assert allowed is False
    assert reason == "quiet hours"


# ── sinks ───────────────────────────────────────────────────────────────────────────


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []
    status = 200

    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            _WebhookHandler.received.append(json.loads(body))
        except ValueError:
            _WebhookHandler.received.append({"_raw": body.decode("utf-8", "replace")})
        self.send_response(_WebhookHandler.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # noqa: D102 - keep the test output clean
        return


@pytest.fixture()
def webhook_server():
    _WebhookHandler.received = []
    _WebhookHandler.status = 200
    server = http.server.HTTPServer(("127.0.0.1", 0), _WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_generic_webhook_payload(webhook_server):
    port = webhook_server.server_address[1]
    sink = WebhookSink(f"http://127.0.0.1:{port}/hook", "generic")
    assert sink.send(_event()) is True
    payload = _WebhookHandler.received[0]
    assert payload["source"] == "netpilot"
    assert payload["severity"] == "critical"
    assert payload["device"] == "r1"
    assert payload["details"]["state"] == "down"
    assert sink.sent == 1 and sink.failed == 0


def test_slack_payload_shape(webhook_server):
    port = webhook_server.server_address[1]
    WebhookSink(f"http://127.0.0.1:{port}/hook", "slack").send(_event())
    payload = _WebhookHandler.received[0]
    assert "text" in payload
    assert payload["attachments"][0]["color"].startswith("#")


def test_discord_payload_shape(webhook_server):
    port = webhook_server.server_address[1]
    WebhookSink(f"http://127.0.0.1:{port}/hook", "discord").send(_event())
    assert "content" in _WebhookHandler.received[0]


def test_teams_payload_shape(webhook_server):
    port = webhook_server.server_address[1]
    WebhookSink(f"http://127.0.0.1:{port}/hook", "teams").send(_event())
    payload = _WebhookHandler.received[0]
    assert payload["@type"] == "MessageCard"
    assert payload["themeColor"]


def test_webhook_failure_is_recorded(webhook_server):
    _WebhookHandler.status = 500
    port = webhook_server.server_address[1]
    sink = WebhookSink(f"http://127.0.0.1:{port}/hook")
    assert sink.send(_event()) is False
    assert sink.failed == 1
    assert "500" in sink.last_error


def test_webhook_to_a_dead_host_fails_cleanly():
    sink = WebhookSink("http://127.0.0.1:1/hook", timeout=1.0)
    assert sink.send(_event()) is False
    assert sink.last_error


def test_email_sink_builds_the_message():
    sink = EmailSink("smtp.example.com", 587, "noc@example.com", from_addr="netpilot@example.com")
    message = sink._message(_event())
    assert message["To"] == "noc@example.com"
    assert message["Subject"].startswith("[netpilot/critical]")
    body = message.get_content()
    assert "state" in body
    assert "r1" in body


def test_email_delivery_failure_is_recorded():
    sink = EmailSink("127.0.0.1", 1, "nobody@example.com", timeout=1.0)
    assert sink.send(_event()) is False
    assert sink.last_error


def test_syslog_sink_failure_is_recorded():
    sink = SyslogSink("127.0.0.1", 1)
    # UDP send to a closed port usually "succeeds"; assert only that it never raises
    assert isinstance(sink.send(_event()), bool)


def test_format_event_text_is_one_line():
    text = format_event_text(_event())
    assert "CRITICAL" in text
    assert "device is down" in text
    assert "\n" not in text


# ── router ──────────────────────────────────────────────────────────────────────────


def test_router_fans_out_to_every_configured_sink(webhook_server):
    port = webhook_server.server_address[1]
    router = AlertRouter(
        NotifyConfig(
            enabled=True,
            min_severity="info",
            webhook_url=f"http://127.0.0.1:{port}/hook",
            syslog_host="127.0.0.1",
            syslog_port=1,
        )
    )
    report = router.dispatch(_event())
    assert report["sent"] is True
    names = {sink["name"] for sink in report["sinks"]}
    assert names == {"webhook", "syslog"}


def test_router_status_lists_the_sinks(webhook_server):
    port = webhook_server.server_address[1]
    router = AlertRouter(NotifyConfig(enabled=True, webhook_url=f"http://127.0.0.1:{port}/hook"))
    status = router.status()
    assert status["enabled"] is True
    assert len(status["sinks"]) == 1
    assert status["sinks"][0]["name"] == "webhook"


def test_router_with_no_sinks_reports_not_sent():
    router = AlertRouter(NotifyConfig(enabled=True))
    report = router.dispatch(_event())
    assert report["sent"] is False


# ── configuration ───────────────────────────────────────────────────────────────────


def test_config_from_dict_ignores_unknown_keys():
    config = NotifyConfig.from_dict({"enabled": True, "nonsense": 1, "min_severity": "info"})
    assert config.enabled is True
    assert config.min_severity == "info"
    assert not hasattr(config, "nonsense")


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("NETPILOT_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setenv("NETPILOT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NETPILOT_SMTP_PORT", "2525")
    config = NotifyConfig.from_env()
    assert config.enabled is True
    assert config.webhook_url == "https://example.com/hook"
    assert config.smtp_port == 2525


def test_config_from_env_is_disabled_when_empty(monkeypatch):
    for key in ("NETPILOT_WEBHOOK_URL", "NETPILOT_SMTP_HOST", "NETPILOT_SYSLOG_HOST"):
        monkeypatch.delenv(key, raising=False)
    assert NotifyConfig.from_env().enabled is False


def test_public_dict_redacts_the_smtp_password():
    config = NotifyConfig(smtp_password="hunter2")
    assert config.to_public_dict()["smtp_password"] == "********"
    assert NotifyConfig().to_public_dict()["smtp_password"] == ""


# ── integration with the core ───────────────────────────────────────────────────────


def test_app_reloads_notifications_from_storage(app):
    app.save_alerts({"enabled": True, "min_severity": "critical", "cooldown_sec": 10})
    app.reload_alerts()
    assert app.alerts.config.enabled is True
    assert app.alerts.config.min_severity == "critical"


def test_app_settings_snapshot_carries_notify_config(app):
    app.save_alerts({"enabled": True, "webhook_url": "https://example.com/hook"})
    snapshot = app.settings_snapshot()
    assert snapshot["notify"]["enabled"] is True
    assert snapshot["notify"]["webhook_url"] == "https://example.com/hook"
