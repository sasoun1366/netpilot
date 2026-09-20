"""The HTTP API and the dashboard assets."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from netpilot.web.app import create_app


@pytest.fixture()
def client(tmp_path):
    application = create_app(data_dir=tmp_path / "webdata")
    with TestClient(application) as test_client:
        yield test_client


def _add_device(client, **overrides):
    payload = {"name": "r1", "host": "127.0.0.1", "vendor": "mikrotik", "tags": ["core"]}
    payload.update(overrides)
    response = client.post("/api/devices", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ── meta & dashboard ────────────────────────────────────────────────────────────────


def test_meta_lists_vendors_and_check_kinds(client):
    meta = client.get("/api/meta").json()
    assert "mikrotik" in [v["name"] for v in meta["vendors"]]
    assert "icmp" in meta["check_kinds"]


def test_health_is_public_and_cheap(client):
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["monitor_running"] is True


def test_overview_shape(client):
    overview = client.get("/api/overview").json()
    for key in ("devices_total", "states", "devices", "events", "recent_jobs", "tags", "monitor_running"):
        assert key in overview


# ── devices ─────────────────────────────────────────────────────────────────────────


def test_creating_a_device_attaches_monitors_immediately(client):
    """The whole point of the tool: add it and it is being watched."""
    device = _add_device(client)
    assert device["id"] == 1
    actions = [check["kind"] for check in device["checks"]]
    assert "icmp" in actions and "tcp" in actions


def test_creating_a_device_can_skip_the_default_monitors(client):
    device = _add_device(client, auto_checks=False)
    assert device["checks"] == []


def test_duplicate_device_returns_409(client):
    _add_device(client)
    response = client.post(
        "/api/devices", json={"name": "dup", "host": "127.0.0.1", "vendor": "mikrotik"}
    )
    assert response.status_code == 409
    assert "already in the inventory" in response.json()["detail"]


def test_device_without_host_is_rejected(client):
    response = client.post("/api/devices", json={"name": "x", "vendor": "mikrotik"})
    assert response.status_code == 400


def test_unknown_vendor_is_rejected(client):
    response = client.post("/api/devices", json={"name": "x", "host": "10.0.0.1", "vendor": "nokia"})
    assert response.status_code == 400
    assert "unknown vendor" in response.json()["detail"]


def test_list_devices_and_filtering(client):
    _add_device(client, host="127.0.0.1", name="a", tags=["core"], site="tokyo")
    _add_device(client, host="127.0.0.2", name="b", tags=["edge"], site="osaka")
    assert len(client.get("/api/devices").json()) == 2
    assert len(client.get("/api/devices?search=tokyo").json()) == 1
    assert len(client.get("/api/devices?tag=edge").json()) == 1
    detailed = client.get("/api/devices?details=true").json()
    assert "sparkline" in detailed[0]
    assert "state" in detailed[0]


def test_device_detail_includes_everything_the_ui_needs(client):
    device = _add_device(client)
    detail = client.get(f"/api/devices/{device['id']}").json()
    for key in ("state", "checks", "check_states", "history", "results", "events", "backups", "templates"):
        assert key in detail
    assert detail["state"]["state"] == "unknown"


def test_device_detail_of_a_missing_device_is_404(client):
    assert client.get("/api/devices/999").status_code == 404


def test_updating_a_device(client):
    device = _add_device(client)
    response = client.put(
        f"/api/devices/{device['id']}",
        json={"name": "renamed", "tags": ["core", "site-a"], "site": "osaka"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "renamed"
    assert client.get(f"/api/devices/{device['id']}").json()["tags"] == ["core", "site-a"]


def test_deleting_a_device(client):
    device = _add_device(client)
    assert client.delete(f"/api/devices/{device['id']}").json()["ok"] is True
    assert client.get(f"/api/devices/{device['id']}").status_code == 404


def test_probing_a_device_returns_results(client):
    device = _add_device(client, host="127.0.0.1")
    response = client.post(f"/api/devices/{device['id']}/probe")
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    assert all("ok" in r for r in results)


def test_suggested_checks_can_be_inspected_before_accepting(client):
    device = _add_device(client, auto_checks=False)
    suggestions = client.get(f"/api/devices/{device['id']}/suggest-checks").json()
    assert {s["kind"] for s in suggestions} == {"icmp", "tcp"}


# ── checks ──────────────────────────────────────────────────────────────────────────


def test_check_crud(client):
    device = _add_device(client)
    created = client.post(
        "/api/checks",
        json={
            "device_id": device["id"],
            "kind": "tcp",
            "label": "web",
            "params": {"port": 8080},
            "interval_sec": 45,
        },
    )
    assert created.status_code == 201
    check = created.json()
    assert check["params"]["port"] == 8080

    updated = client.put(f"/api/checks/{check['id']}", json={"interval_sec": 90, "enabled": False})
    assert updated.json()["interval_sec"] == 90
    assert updated.json()["enabled"] is False

    assert client.delete(f"/api/checks/{check['id']}").json()["ok"] is True
    remaining = client.get(f"/api/devices/{device['id']}/checks").json()
    # the two monitors attached when the device was created are still there
    assert [c["kind"] for c in remaining] == ["icmp", "tcp"]


def test_schedule_endpoint_reports_countdowns(client):
    _add_device(client)
    schedule = client.get("/api/schedule").json()
    assert len(schedule) == 2
    assert all("next_run_in" in row for row in schedule)


# ── templates ───────────────────────────────────────────────────────────────────────


def test_builtin_templates_are_listed(client):
    templates = client.get("/api/templates").json()
    assert len(templates) >= 100 // 8
    assert all("builtin" in t for t in templates)
    mikrotik = [t for t in templates if t["vendor"] == "mikrotik"]
    assert any(t["name"] == "NTP servers" for t in mikrotik)


def test_templates_can_be_filtered_by_vendor(client):
    templates = client.get("/api/templates?vendor=cisco").json()
    assert templates
    assert {t["vendor"] for t in templates} <= {"cisco", "generic"}


def test_template_crud_overrides_a_builtin(client):
    response = client.post(
        "/api/templates",
        json={
            "name": "NTP servers",
            "vendor": "mikrotik",
            "body": "/custom {{ server }}",
            "variables": {"server": "custom.ntp"},
            "description": "customised",
        },
    )
    assert response.status_code == 201
    templates = client.get("/api/templates?vendor=mikrotik").json()
    matches = [t for t in templates if t["name"] == "NTP servers"]
    assert len(matches) == 1  # the built-in is shadowed, not duplicated
    assert matches[0]["builtin"] is False
    assert matches[0]["body"] == "/custom {{ server }}"


def test_preview_renders_commands_and_reports_unresolved(client):
    response = client.post(
        "/api/templates/preview",
        json={
            "body": "/ip service set ssh port={{ port }}\n/set x {{ missing }}",
            "vendor": "mikrotik",
            "variables": {"port": "2222"},
        },
    )
    payload = response.json()
    assert payload["commands"][0] == "/ip service set ssh port=2222"
    assert payload["unresolved"] == ["missing"]
    assert payload["rollback_support"] == "native"


def test_preview_does_not_require_a_device(client):
    response = client.post(
        "/api/templates/preview",
        json={"body": "ntp server 1.1.1.1", "vendor": "cisco"},
    )
    assert response.status_code == 200
    assert response.json()["commands"] == ["ntp server 1.1.1.1"]


# ── deploys ─────────────────────────────────────────────────────────────────────────


def test_deploy_requires_targets(client):
    response = client.post(
        "/api/deploys",
        json={"body": "/system ntp client set enabled=yes", "vendor": "mikrotik", "device_ids": []},
    )
    assert response.status_code == 400
    assert "no devices matched" in response.json()["detail"]


def test_deploy_plan_is_returned_and_runnable(client):
    device = _add_device(client)
    response = client.post(
        "/api/deploys",
        json={
            "body": "/system ntp client set enabled=yes",
            "vendor": "mikrotik",
            "device_ids": [device["id"]],
            "options": {"dry_run": True},
            "run": False,
        },
    )
    assert response.status_code == 201
    job = response.json()
    assert job["total"] == 1
    assert job["status"] == "pending"
    assert len(job["targets"]) == 1

    detail = client.get(f"/api/deploys/{job['id']}").json()
    assert detail["targets"][0]["commands"] == ["/system ntp client set enabled=yes"]
    assert "backup" not in detail["targets"][0]

    listing = client.get("/api/deploys").json()
    assert listing[0]["id"] == job["id"]


def test_deploy_detail_of_a_missing_job_is_404(client):
    assert client.get("/api/deploys/999").status_code == 404


def test_generic_vendor_cannot_be_deployed_to(client):
    device = _add_device(client, vendor="generic", host="10.0.0.5")
    response = client.post(
        "/api/deploys",
        json={"body": "ls", "vendor": "generic", "device_ids": [device["id"]], "run": False},
    )
    assert response.status_code == 400
    assert "support configuration push" in response.json()["detail"]


# ── backups ─────────────────────────────────────────────────────────────────────────


def test_backup_listing_and_deletion(client):
    device = _add_device(client)
    application = client.app
    store = application.state.app.store
    backup_id = store.add_backup(device["id"], device["name"], device["host"], "hostname r1\n")

    listed = client.get("/api/backups").json()
    assert len(listed) == 1
    assert listed[0]["id"] == backup_id
    assert "config" not in listed[0]  # listings stay small

    full = client.get(f"/api/backups/{backup_id}").json()
    assert full["config"] == "hostname r1\n"

    assert client.delete(f"/api/backups/{backup_id}").json()["ok"] is True
    assert client.get(f"/api/backups/{backup_id}").status_code == 404


def test_restore_dry_run_needs_no_connection(client):
    device = _add_device(client)
    store = client.app.state.app.store
    backup_id = store.add_backup(device["id"], device["name"], device["host"], "hostname r1\nset x 1\n")
    response = client.post(f"/api/backups/{backup_id}/restore", json={"dry_run": True})
    payload = response.json()
    assert payload["ok"] is True
    assert payload["command_count"] == 2


def test_restore_of_a_missing_backup(client):
    response = client.post("/api/backups/999/restore", json={"dry_run": True})
    assert response.json()["ok"] is False


# ── events & settings ───────────────────────────────────────────────────────────────


def test_event_feed_and_acknowledgement(client):
    device = _add_device(client)
    store = client.app.state.app.store
    from netpilot.models import Event

    store.add_event(Event(severity="critical", message="down", device_id=device["id"]))
    store.add_event(Event(severity="info", message="up", device_id=device["id"]))

    everything = client.get("/api/events").json()
    assert {"down", "up"} <= {e["message"] for e in everything}
    critical = client.get("/api/events?severity=critical").json()
    assert [e["message"] for e in critical] == ["down"]
    assert len(client.get("/api/events?unacked=true").json()) == len(everything)

    client.post("/api/events/ack-all")
    assert client.get("/api/events?unacked=true").json() == []


def test_acknowledging_a_single_event(client):
    store = client.app.state.app.store
    from netpilot.models import Event

    event = store.add_event(Event(severity="warning", message="blip"))
    assert client.post(f"/api/events/{event.id}/ack", json={}).json()["ok"] is True
    assert store.unacknowledged_count() == 0


def test_settings_snapshot_and_notify_update(client):
    snapshot = client.get("/api/settings").json()
    assert snapshot["monitor_running"] is True
    assert "vendors" in snapshot
    assert "notify" in snapshot

    updated = client.put(
        "/api/settings/notify",
        json={"enabled": False, "min_severity": "critical", "cooldown_sec": 60},
    )
    assert updated.status_code == 200
    assert client.get("/api/settings").json()["notify"]["min_severity"] == "critical"


def test_notification_test_endpoint_reports_why_nothing_was_sent(client):
    response = client.post("/api/settings/notify/test")
    payload = response.json()
    assert payload["sent"] is False
    assert payload["reason"]


# ── credentials ─────────────────────────────────────────────────────────────────────


def test_credential_lifecycle_never_returns_the_secret(client):
    created = client.post(
        "/api/credentials",
        json={"name": "lab", "username": "admin", "password": "hunter2"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["password"] == "********"

    listed = client.get("/api/credentials").json()
    assert listed[0]["password"] == "********"
    assert "hunter2" not in json.dumps(listed)

    # the secret is nevertheless usable server-side
    store = client.app.state.app.store
    assert store.get_credential(body["id"]).password == "hunter2"

    updated = client.put(f"/api/credentials/{body['id']}", json={"username": "netops"})
    assert updated.json()["username"] == "netops"

    assert client.delete(f"/api/credentials/{body['id']}").json()["ok"] is True


def test_credential_requires_a_name(client):
    response = client.post("/api/credentials", json={"username": "admin"})
    assert response.status_code == 400


def test_updating_a_missing_credential_is_404(client):
    assert client.put("/api/credentials/999", json={"name": "x"}).status_code == 404


# ── streaming & assets ──────────────────────────────────────────────────────────────


def test_sse_endpoint_streams_over_a_real_socket(tmp_path):
    """SSE is tested against a live server: the in-process test client cannot stream an
    endless response, and "it streams in production" is exactly what needs proving."""
    import socket
    import threading

    import uvicorn

    server_socket = socket.socket()
    server_socket.bind(("127.0.0.1", 0))
    port = server_socket.getsockname()[1]
    server_socket.close()

    config = uvicorn.Config(
        create_app(data_dir=tmp_path / "sse"),
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 20
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"

        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                f"GET /api/events/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Accept: text/event-stream\r\nConnection: close\r\n\r\n".encode()
            )
            sock.settimeout(10)
            received = b""
            while b"\r\n\r\n" not in received or b"data:" not in received:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk

        text = received.decode("utf-8", "replace")
        assert "200 OK" in text
        assert "text/event-stream" in text
        assert "data:" in text
        payload = json.loads(text.split("data:", 1)[1].split("\n", 1)[0].strip())
        assert payload["type"] == "hello"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_broadcaster_fans_out_to_subscribers():
    """The in-process half of SSE: publish/subscribe without a socket."""
    import asyncio

    from netpilot.web.app import Broadcaster

    async def scenario():
        broadcaster = Broadcaster()
        first = await broadcaster.subscribe()
        second = await broadcaster.subscribe()
        assert broadcaster.subscriber_count == 2

        broadcaster.publish({"type": "event", "data": {"message": "device down"}})
        assert (await first.get())["data"]["message"] == "device down"
        assert (await second.get())["data"]["message"] == "device down"

        await broadcaster.unsubscribe(first)
        assert broadcaster.subscriber_count == 1

    asyncio.run(scenario())


def test_broadcaster_drops_messages_for_a_stalled_client():
    import asyncio

    from netpilot.web.app import Broadcaster

    async def scenario():
        broadcaster = Broadcaster()
        queue = await broadcaster.subscribe()
        for index in range(queue.maxsize + 50):
            # must not raise or block, even though nobody is draining the queue
            broadcaster.publish({"type": "result", "data": {"n": index}})
        assert queue.qsize() == queue.maxsize

    asyncio.run(scenario())


def test_dashboard_assets_are_served_locally(client):
    """No CDN: everything must come from this server so it works offline."""
    index = client.get("/")
    assert index.status_code == 200
    assert "netpilot" in index.text

    for asset in ("/static/style.css", "/static/app.js"):
        response = client.get(asset)
        assert response.status_code == 200
        assert len(response.text) > 500

    for external in ("http://cdn", "https://cdn", "unpkg.com", "jsdelivr"):
        assert external not in index.text
        assert external not in client.get("/static/app.js").text


def test_spa_uses_only_relative_urls(client):
    script = client.get("/static/app.js").text
    assert "http://localhost" not in script
    assert "/api/overview" in script


# ── regression: the per-device deploy box needs real template bodies ────────────────


def test_device_detail_templates_are_renderable(client):
    """Both UIs build a per-device deploy box from this payload.

    A built-in template has no database id, so the body and variables must travel with
    the listing — otherwise the preview silently renders nothing.
    """
    device = _add_device(client)
    detail = client.get(f"/api/devices/{device['id']}").json()
    templates = detail["templates"]
    assert templates
    for template in templates:
        assert template["body"].strip(), template["name"]
        assert isinstance(template["variables"], dict)
        assert template["vendor"] in ("mikrotik", "generic")

    ntp = next(t for t in templates if t["name"] == "NTP servers")
    preview = client.post(
        "/api/templates/preview",
        json={
            "template_id": ntp["id"],
            "body": ntp["body"],
            "vendor": ntp["vendor"],
            "variables": ntp["variables"],
        },
    ).json()
    assert preview["command_count"] > 0
    assert any("pool.ntp.org" in command for command in preview["commands"])


def test_deploy_by_template_name_alone(client):
    """The SPA and CLI both name built-in templates; neither should have to resend the body."""
    device = _add_device(client, vendor="mikrotik")
    response = client.post(
        "/api/deploys",
        json={
            "template_name": "NTP servers",
            "vendor": "mikrotik",
            "device_ids": [device["id"]],
            "options": {"dry_run": True},
            "run": False,
        },
    )
    assert response.status_code == 201, response.text
    job = response.json()
    commands = job["targets"][0]["commands"]
    assert any("pool.ntp.org" in command for command in commands), commands
    assert not any("{{" in command for command in commands), commands
