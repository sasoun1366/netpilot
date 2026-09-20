"""Alert delivery.

netpilot is offline-first: the dashboard and the event feed always work with no
configuration. *Notifications* (webhook / email / syslog) are opt-in and configured either
from the Settings page in the UI or through ``NETPILOT_WEBHOOK_URL`` /
``NETPILOT_SMTP_*`` environment variables.

Delivery is best-effort and never blocks the monitor: sinks are invoked from worker
threads and every failure is recorded in the sink's ``last_error`` for the UI to show.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from ..models import Event

log = logging.getLogger("netpilot.alerts")

#: Lower number = more severe. Used by the ``min_severity`` rule.
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def severity_at_least(severity: str, minimum: str) -> bool:
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(minimum, 0)


@dataclass
class NotifyConfig:
    """Rules + destinations for outbound alerts."""

    enabled: bool = False
    min_severity: str = "warning"
    #: Suppress repeat alerts for the same device+state within this window (seconds).
    cooldown_sec: int = 300
    notify_on_recovery: bool = True
    #: "HH:MM-HH:MM" local time; alerts are silenced inside this window.
    quiet_hours: str = ""
    webhook_url: str = ""
    webhook_kind: str = "generic"  # generic | slack | discord | teams
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "netpilot@localhost"
    smtp_to: str = ""
    smtp_tls: bool = True
    syslog_host: str = ""
    syslog_port: int = 514

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "NotifyConfig":
        cfg = cls()
        for key, value in (data or {}).items():
            if hasattr(cfg, key) and value is not None:
                setattr(cfg, key, value)
        return cfg

    @classmethod
    def from_env(cls) -> "NotifyConfig":
        cfg = cls()
        cfg.webhook_url = os.environ.get("NETPILOT_WEBHOOK_URL", "")
        cfg.webhook_kind = os.environ.get("NETPILOT_WEBHOOK_KIND", "generic")
        cfg.smtp_host = os.environ.get("NETPILOT_SMTP_HOST", "")
        cfg.smtp_port = int(os.environ.get("NETPILOT_SMTP_PORT", "587"))
        cfg.smtp_user = os.environ.get("NETPILOT_SMTP_USER", "")
        cfg.smtp_password = os.environ.get("NETPILOT_SMTP_PASSWORD", "")
        cfg.smtp_to = os.environ.get("NETPILOT_SMTP_TO", "")
        cfg.smtp_from = os.environ.get("NETPILOT_SMTP_FROM", "netpilot@localhost")
        cfg.syslog_host = os.environ.get("NETPILOT_SYSLOG_HOST", "")
        cfg.enabled = bool(cfg.webhook_url or cfg.smtp_host or cfg.syslog_host)
        return cfg

    def to_public_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["smtp_password"] = "********" if self.smtp_password else ""
        return data


def _in_quiet_hours(spec: str, now: time.struct_time | None = None) -> bool:
    """True when *now* falls inside an ``HH:MM-HH:MM`` window (wraps midnight)."""
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return False
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except (TypeError, ValueError):
        return False
    lt = now or time.localtime()
    cur = lt.tm_hour * 60 + lt.tm_min
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def format_event_text(event: Event) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.ts))
    return f"[{event.severity.upper()}] {stamp} {event.message}"


class WebhookSink:
    """POSTs a JSON payload to a webhook, with native Slack/Discord/Teams shapes."""

    name = "webhook"

    def __init__(self, url: str, kind: str = "generic", timeout: float = 10.0) -> None:
        self.url = url
        self.kind = kind
        self.timeout = timeout
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self.last_sent_at: float | None = None

    def payload(self, event: Event) -> dict[str, Any]:
        text = format_event_text(event)
        if self.kind == "slack":
            return {"text": text, "attachments": [{"color": _color(event.severity), "text": text}]}
        if self.kind == "discord":
            return {"content": text}
        if self.kind == "teams":
            return {
                "@type": "MessageCard",
                "@context": "https://schema.org/extensions",
                "summary": event.message,
                "themeColor": _color(event.severity).lstrip("#"),
                "title": f"netpilot: {event.severity}",
                "text": event.message,
            }
        return {
            "source": "netpilot",
            "severity": event.severity,
            "message": event.message,
            "device": event.device_name,
            "kind": event.kind,
            "ts": event.ts,
            "details": event.details,
        }

    def send(self, event: Event) -> bool:
        body = json.dumps(self.payload(event)).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "netpilot/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                if 200 <= resp.status < 300:
                    self.sent += 1
                    self.last_sent_at = time.time()
                    self.last_error = ""
                    return True
                self.failed += 1
                self.last_error = f"HTTP {resp.status}"
                return False
        except (urllib.error.URLError, OSError) as exc:
            self.failed += 1
            self.last_error = str(exc)[:200]
            log.warning("webhook delivery failed: %s", exc)
            return False

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": self.url,
            "sent": self.sent,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_sent_at": self.last_sent_at,
        }


class EmailSink:
    """Sends alerts over SMTP (STARTTLS by default)."""

    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        to_addr: str,
        from_addr: str = "netpilot@localhost",
        user: str = "",
        password: str = "",
        use_tls: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.to_addr = to_addr
        self.from_addr = from_addr
        self.user = user
        self.password = password
        self.use_tls = use_tls
        self.timeout = timeout
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self.last_sent_at: float | None = None

    def _message(self, event: Event) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"[netpilot/{event.severity}] {event.message}"
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        lines = [
            format_event_text(event),
            "",
            f"device : {event.device_name or '-'}",
            f"kind   : {event.kind}",
        ]
        for key, value in (event.details or {}).items():
            lines.append(f"{key:<7}: {value}")
        msg.set_content("\n".join(lines))
        return msg

    def send(self, event: Event) -> bool:
        msg = self._message(event)
        try:
            if self.port == 465:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as smtp:
                    if self.user:
                        smtp.login(self.user, self.password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                    if self.use_tls:
                        smtp.starttls(context=ssl.create_default_context())
                    if self.user:
                        smtp.login(self.user, self.password)
                    smtp.send_message(msg)
            self.sent += 1
            self.last_sent_at = time.time()
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - smtplib raises a wide surface
            self.failed += 1
            self.last_error = str(exc)[:200]
            log.warning("email delivery failed: %s", exc)
            return False

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": f"{self.host}:{self.port} -> {self.to_addr}",
            "sent": self.sent,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_sent_at": self.last_sent_at,
        }


class SyslogSink:
    """Emits RFC3164-ish UDP syslog lines — handy for feeding an existing SIEM."""

    name = "syslog"

    def __init__(self, host: str, port: int = 514) -> None:
        self.host = host
        self.port = port
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self.last_sent_at: float | None = None

    def send(self, event: Event) -> bool:
        import socket

        facility = 23 << 3  # local7
        sev = {"critical": 2, "warning": 4, "info": 6}.get(event.severity, 6)
        pri = facility + sev
        stamp = time.strftime("%b %d %H:%M:%S", time.localtime(event.ts))
        line = f"<{pri}>{stamp} netpilot: {format_event_text(event)}"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(5.0)
                sock.sendto(line.encode(), (self.host, self.port))
            self.sent += 1
            self.last_sent_at = time.time()
            self.last_error = ""
            return True
        except OSError as exc:
            self.failed += 1
            self.last_error = str(exc)[:200]
            return False

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": f"{self.host}:{self.port}",
            "sent": self.sent,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_sent_at": self.last_sent_at,
        }


def _color(severity: str) -> str:
    return {"critical": "#d64545", "warning": "#d99a2b", "info": "#2f9e5e"}.get(severity, "#7a8699")


@dataclass
class AlertRouter:
    """Applies the rule set, then fans a qualifying event out to every sink."""

    config: NotifyConfig = field(default_factory=NotifyConfig)
    _last_sent: dict[str, float] = field(default_factory=dict)
    _suppressed: int = 0

    def sinks(self) -> list[Any]:
        out: list[Any] = []
        if self.config.webhook_url:
            out.append(WebhookSink(self.config.webhook_url, self.config.webhook_kind))
        if self.config.smtp_host and self.config.smtp_to:
            out.append(
                EmailSink(
                    host=self.config.smtp_host,
                    port=int(self.config.smtp_port),
                    to_addr=self.config.smtp_to,
                    from_addr=self.config.smtp_from,
                    user=self.config.smtp_user,
                    password=self.config.smtp_password,
                    use_tls=bool(self.config.smtp_tls),
                )
            )
        if self.config.syslog_host:
            out.append(SyslogSink(self.config.syslog_host, int(self.config.syslog_port)))
        return out

    def should_send(self, event: Event, now: float | None = None) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "notifications disabled"
        if not severity_at_least(event.severity, self.config.min_severity):
            return False, f"below min severity ({self.config.min_severity})"
        if _in_quiet_hours(self.config.quiet_hours):
            return False, "quiet hours"
        state = str((event.details or {}).get("state", ""))
        if event.severity == "info" and not self.config.notify_on_recovery:
            return False, "recovery notifications disabled"
        key = f"{event.device_id}:{state}"
        last = self._last_sent.get(key)
        stamp = now if now is not None else time.time()
        if last is not None and stamp - last < self.config.cooldown_sec:
            self._suppressed += 1
            return False, "cooldown"
        self._last_sent[key] = stamp
        return True, "ok"

    def dispatch(self, event: Event) -> dict[str, Any]:
        """Route one event. Returns a small report (also used by the UI's Test button)."""
        allowed, reason = self.should_send(event)
        if not allowed:
            return {"sent": False, "reason": reason, "sinks": []}
        results = []
        for sink in self.sinks():
            ok = sink.send(event)
            results.append({"name": sink.name, "ok": ok, "error": sink.last_error})
        return {"sent": any(r["ok"] for r in results), "reason": "ok", "sinks": results}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "min_severity": self.config.min_severity,
            "cooldown_sec": self.config.cooldown_sec,
            "quiet_hours": self.config.quiet_hours,
            "suppressed": self._suppressed,
            "sinks": [s.status() for s in self.sinks()],
        }
