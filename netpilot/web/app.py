"""FastAPI application: the REST API and the dashboard's data source.

The server is a thin shell around :class:`netpilot.core.App`. Two things are worth calling
out:

* **Server-Sent Events** (``/api/events/stream``) push state changes, probe results and
  deploy progress to the browser, so the dashboard is genuinely live without polling and
  without a WebSocket dependency.
* **Everything is same-origin.** The SPA is served from this app and speaks only relative
  URLs, which means it works behind a reverse proxy, in a container, or on a laptop with
  no extra CORS configuration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core import App
from ..db import DeviceExistsError
from ..models import CHECK_KINDS

log = logging.getLogger("netpilot.web")

STATIC_DIR = Path(__file__).parent / "static"

#: How long a client waits between SSE keep-alive comments.
SSE_HEARTBEAT = 15.0


class Broadcaster:
    """Fan-out of app events to every connected browser."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    def publish(self, payload: dict[str, Any]) -> None:
        """Non-async so the monitor's worker threads can publish directly."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A stalled browser must never slow down monitoring.
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


def create_app(data_dir: str | os.PathLike[str] | None = None) -> FastAPI:
    """Build the ASGI application (factory, so tests can spin up clean instances)."""
    state = {"app": None, "broadcaster": Broadcaster()}

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.app = App(data_dir=data_dir, start_monitor=True)
        broadcaster: Broadcaster = state["broadcaster"]

        def on_event(event: Any) -> None:
            broadcaster.publish({"type": "event", "data": event.to_dict()})

        def on_job(job: Any) -> None:
            broadcaster.publish({"type": "job", "data": job.to_dict()})

        def on_result(result: Any) -> None:
            broadcaster.publish(
                {
                    "type": "result",
                    "data": {
                        "device_id": result.device_id,
                        "ok": result.ok,
                        "latency_ms": result.latency_ms,
                        "kind": result.kind,
                        "ts": result.ts,
                    },
                }
            )

        application.state.app.events.on_event.append(on_event)
        application.state.app.events.on_job.append(on_job)
        application.state.app.events.on_result.append(on_result)
        application.state.broadcaster = broadcaster

        await application.state.app.start()
        log.info("netpilot %s ready (data dir: %s)", __version__, application.state.app.store.data_dir)
        try:
            yield
        finally:
            await application.state.app.stop()
            application.state.app.store.close()

    api = FastAPI(
        title="netpilot",
        version=__version__,
        description="Network monitoring dashboard and bulk configuration deployment.",
        lifespan=lifespan,
    )

    def core(request: Request) -> App:
        app = getattr(request.app.state, "app", None)
        if app is None:  # pragma: no cover - only before lifespan runs
            raise HTTPException(status_code=503, detail="netpilot core is not ready")
        return app

    # ---------------------------------------------------------------- meta & dashboard

    @api.get("/api/meta")
    def meta(request: Request) -> dict[str, Any]:
        app = core(request)
        return {
            "version": __version__,
            "vendors": app.settings_snapshot()["vendors"],
            "check_kinds": list(CHECK_KINDS),
            "data_dir": str(app.store.data_dir),
        }

    @api.get("/api/overview")
    def overview(request: Request) -> dict[str, Any]:
        return core(request).overview()

    @api.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        app = core(request)
        return {
            "status": "ok",
            "version": __version__,
            "monitor_running": app.monitor.running,
            "devices": len(app.store.list_devices()),
            "subscribers": state["broadcaster"].subscriber_count,
            "time": time.time(),
        }

    # ---------------------------------------------------------------- SSE

    @api.get("/api/events/stream")
    async def stream(request: Request) -> StreamingResponse:
        broadcaster: Broadcaster = state["broadcaster"]

        async def generator() -> AsyncIterator[str]:
            queue = await broadcaster.subscribe()
            try:
                yield f"data: {json.dumps({'type': 'hello', 'data': {'version': __version__}})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(payload, default=str)}\n\n"
            finally:
                await broadcaster.unsubscribe(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---------------------------------------------------------------- credentials

    @api.get("/api/credentials")
    def list_credentials(request: Request) -> list[dict[str, Any]]:
        return [c.to_dict(redact=True) for c in core(request).store.list_credentials()]

    @api.post("/api/credentials", status_code=201)
    def create_credential(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            cred = core(request).add_credential(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return cred.to_dict(redact=True)

    @api.put("/api/credentials/{cred_id}")
    def update_credential(request: Request, cred_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return core(request).update_credential(cred_id, payload).to_dict(redact=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.delete("/api/credentials/{cred_id}")
    def delete_credential(request: Request, cred_id: int) -> dict[str, bool]:
        core(request).store.delete_credential(cred_id)
        return {"ok": True}

    # ---------------------------------------------------------------- devices

    @api.get("/api/devices")
    def list_devices(
        request: Request,
        search: str | None = None,
        tag: str | None = None,
        details: bool = Query(False, description="include checks, sparklines and history"),
    ) -> Any:
        app = core(request)
        if details:
            cards = app.store.device_cards()
            if tag:
                wanted = {t.strip().lower() for t in tag.split(",") if t.strip()}
                cards = [c for c in cards if wanted & {t.lower() for t in c["tags"]}]
            if search:
                needle = search.lower()
                cards = [
                    c
                    for c in cards
                    if needle in c["name"].lower()
                    or needle in c["host"].lower()
                    or needle in (c.get("site") or "").lower()
                ]
            return cards
        return [d.to_dict() for d in app.store.list_devices(search=search, tags=[tag] if tag else None)]

    @api.post("/api/devices", status_code=201)
    async def create_device(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        app = core(request)
        try:
            device = await app.add_device(payload, auto_checks=payload.get("auto_checks", True))
        except DeviceExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data = device.to_dict()
        data["checks"] = [c.to_dict() for c in app.store.list_checks(device_id=device.id)]
        return data

    @api.get("/api/devices/{device_id}")
    def get_device(request: Request, device_id: int, history: int = 120) -> dict[str, Any]:
        detail = core(request).device_detail(device_id, history=history)
        if detail is None:
            raise HTTPException(status_code=404, detail="device not found")
        return detail

    @api.put("/api/devices/{device_id}")
    async def update_device(request: Request, device_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return (await core(request).update_device(device_id, payload)).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.delete("/api/devices/{device_id}")
    def delete_device(request: Request, device_id: int) -> dict[str, bool]:
        core(request).delete_device(device_id)
        return {"ok": True}

    @api.post("/api/devices/{device_id}/test")
    async def test_device(request: Request, device_id: int) -> dict[str, Any]:
        return await core(request).test_device(device_id)

    @api.post("/api/devices/{device_id}/probe")
    async def probe_device(request: Request, device_id: int) -> dict[str, Any]:
        results = await core(request).probe_device(device_id)
        return {"results": results}

    @api.get("/api/devices/{device_id}/checks")
    def device_checks(request: Request, device_id: int) -> list[dict[str, Any]]:
        return [c.to_dict() for c in core(request).store.list_checks(device_id=device_id)]

    @api.get("/api/devices/{device_id}/suggest-checks")
    def suggest_checks(request: Request, device_id: int) -> list[dict[str, Any]]:
        return core(request).suggest_checks(device_id)

    @api.post("/api/devices/{device_id}/backup")
    async def backup_device(request: Request, device_id: int) -> dict[str, Any]:
        return await core(request).backup_device(device_id)

    # ---------------------------------------------------------------- checks

    @api.post("/api/checks", status_code=201)
    def create_check(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return core(request).add_check(payload).to_dict()
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.put("/api/checks/{check_id}")
    def update_check(request: Request, check_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return core(request).update_check(check_id, payload).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.delete("/api/checks/{check_id}")
    def delete_check(request: Request, check_id: int) -> dict[str, bool]:
        core(request).delete_check(check_id)
        return {"ok": True}

    @api.get("/api/schedule")
    def schedule(request: Request) -> list[dict[str, Any]]:
        return core(request).monitor.next_runs()

    # ---------------------------------------------------------------- templates

    @api.get("/api/templates")
    def list_templates(request: Request, vendor: str | None = None) -> list[dict[str, Any]]:
        return core(request).template_library(vendor=vendor)

    @api.post("/api/templates", status_code=201)
    def create_template(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return core(request).save_template(payload).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.put("/api/templates/{template_id}")
    def update_template(request: Request, template_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        payload = {**payload, "id": template_id}
        try:
            return core(request).save_template(payload).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.delete("/api/templates/{template_id}")
    def delete_template(request: Request, template_id: int) -> dict[str, bool]:
        core(request).delete_template(template_id)
        return {"ok": True}

    @api.post("/api/templates/preview")
    def preview_template(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return core(request).preview_template(
            payload.get("template_id"),
            payload.get("body") or "",
            payload.get("vendor") or "mikrotik",
            payload.get("variables") or {},
        )

    # ---------------------------------------------------------------- deploys

    @api.get("/api/deploys")
    def list_deploys(request: Request, limit: int = 50) -> list[dict[str, Any]]:
        return [j.to_dict() for j in core(request).store.list_jobs(limit=limit)]

    @api.get("/api/deploys/{job_id}")
    def get_deploy(request: Request, job_id: int) -> dict[str, Any]:
        detail = core(request).job_detail(job_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="job not found")
        return detail

    @api.post("/api/deploys", status_code=201)
    async def create_deploy(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        app = core(request)
        try:
            job = app.create_deploy(
                body=payload.get("body") or "",
                vendor=payload.get("vendor") or "mikrotik",
                device_ids=payload.get("device_ids"),
                tags=payload.get("tags"),
                match_all=bool(payload.get("match_all", False)),
                variables=payload.get("variables") or {},
                options=payload.get("options") or {},
                template_id=payload.get("template_id"),
                template_name=payload.get("template_name"),
                triggered_by=payload.get("triggered_by") or "web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if payload.get("run", True):
            asyncio.create_task(app.run_deploy(job.id))
        return app.job_detail(job.id) or job.to_dict()

    @api.post("/api/deploys/{job_id}/cancel")
    def cancel_deploy(request: Request, job_id: int) -> dict[str, Any]:
        return {"ok": core(request).deploy.cancel(job_id)}

    @api.get("/api/deploys/targets/{target_id}")
    def deploy_target(request: Request, target_id: int) -> dict[str, Any]:
        detail = core(request).job_target_diff(target_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="target not found")
        return detail

    # ---------------------------------------------------------------- backups

    @api.get("/api/backups")
    def list_backups(request: Request, device_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return core(request).store.list_backups(device_id=device_id, limit=limit)

    @api.get("/api/backups/{backup_id}")
    def get_backup(request: Request, backup_id: int) -> dict[str, Any]:
        backup = core(request).store.get_backup(backup_id)
        if backup is None:
            raise HTTPException(status_code=404, detail="backup not found")
        return backup

    @api.post("/api/backups/{backup_id}/restore")
    async def restore_backup_endpoint(request: Request, backup_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return await core(request).restore(backup_id, dry_run=bool(payload.get("dry_run", True)))

    @api.delete("/api/backups/{backup_id}")
    def delete_backup(request: Request, backup_id: int) -> dict[str, bool]:
        core(request).store.delete_backup(backup_id)
        return {"ok": True}

    # ---------------------------------------------------------------- events & settings

    @api.get("/api/events")
    def list_events(
        request: Request,
        limit: int = 100,
        device_id: int | None = None,
        severity: str | None = None,
        unacked: bool = False,
    ) -> list[dict[str, Any]]:
        events = core(request).store.list_events(
            limit=limit, device_id=device_id, severity=severity, unacknowledged_only=unacked
        )
        return [e.to_dict() for e in events]

    @api.post("/api/events/{event_id}/ack")
    def ack_event(request: Request, event_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, bool]:
        core(request).store.acknowledge_event(event_id, bool(payload.get("acknowledged", True)))
        return {"ok": True}

    @api.post("/api/events/ack-all")
    def ack_all_events(request: Request) -> dict[str, bool]:
        core(request).store.acknowledge_all_events()
        return {"ok": True}

    @api.get("/api/settings")
    def get_settings(request: Request) -> dict[str, Any]:
        return core(request).settings_snapshot()

    @api.put("/api/settings/notify")
    def put_notify_settings(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        app = core(request)
        app.save_alerts(payload)
        return app.alerts.status()

    @api.post("/api/settings/notify/test")
    def test_notify(request: Request) -> dict[str, Any]:
        app = core(request)
        from ..models import Event

        event = Event(
            device_name="netpilot",
            kind="test",
            severity="warning",
            message="This is a netpilot test notification — routing works.",
            details={"state": "test"},
        )
        forced = app.alerts.config
        forced.cooldown_sec = 0
        return app.alerts.dispatch(event)

    # ---------------------------------------------------------------- static SPA

    if STATIC_DIR.exists():
        api.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @api.get("/", include_in_schema=False)
    def index() -> Any:
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():  # pragma: no cover - packaging mishap
            return JSONResponse(
                {"error": "dashboard assets are missing", "hint": f"expected {index_file}"},
                status_code=500,
            )
        return FileResponse(index_file)

    @api.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": f"{exc.__class__.__name__}: {exc}"}, status_code=500)

    return api


def run_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    data_dir: str | os.PathLike[str] | None = None,
    open_browser: bool = False,
    reload: bool = False,
    log_level: str = "warning",
) -> int:
    """Entry point used by ``netpilot web``."""
    import uvicorn

    if reload:
        os.environ["NETPILOT_DATA_DIR"] = str(data_dir) if data_dir else ""
        uvicorn.run(
            "netpilot.web.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            log_level=log_level,
        )
        return 0

    application = create_app(data_dir=data_dir)
    if open_browser:
        import threading
        import webbrowser

        url_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{url_host}:{port}/")).start()

    uvicorn.run(application, host=host, port=port, log_level=log_level)
    return 0


app = None  # populated lazily by uvicorn's factory import when used as a module


def get_app() -> FastAPI:
    """Return a process-wide instance (``uvicorn netpilot.web.app:get_app --factory``)."""
    return create_app(data_dir=os.environ.get("NETPILOT_DATA_DIR") or None)
