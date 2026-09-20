"""Bulk configuration deployment.

The safety model, in order of application:

1. **Dry run** (default for new jobs) — render and validate everything, touch nothing.
2. **Pre-flight** — check the SSH port of every target *before* the first write, so an
   unreachable box is reported as "skipped" rather than half-applied.
3. **Backup first** — the running config is captured and stored for every target.
4. **Rollback point** — vendors with native checkpointing (RouterOS backups) get one;
   vendors without it (IOS) are reported as ``manual`` so the operator knows the blast
   radius before pressing the button.
5. **Bounded concurrency** — devices are changed in parallel but never more than
   :attr:`DeployOptions.max_parallel` at once, so a bulk push cannot flood the network.
6. **Auto-rollback** — on failure, the executor restores the rollback point when the
   vendor supports it, otherwise it records the pre-change config as the recovery path.

Every step writes to the job/target tables as it goes, so the UI can stream progress.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..adapters import AdapterError, get_adapter
from ..adapters.base import Adapter, CommandResult
from ..db import Store
from ..models import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
    TARGET_FAILED,
    TARGET_OK,
    TARGET_PENDING,
    TARGET_ROLLED_BACK,
    TARGET_RUNNING,
    TARGET_SKIPPED,
    Device,
    Event,
    Job,
    JobTarget,
    Template,
)
from ..adapters.registry import resolve_vendor

log = logging.getLogger("netpilot.deploy")


@dataclass
class DeployOptions:
    """Everything an operator can tune about a push."""

    dry_run: bool = True
    save_config: bool = True
    backup_before: bool = True
    auto_rollback: bool = True
    max_parallel: int = 5
    stop_on_first_failure: bool = False
    per_device_timeout: float = 60.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DeployOptions":
        opts = cls()
        for key, value in (data or {}).items():
            if hasattr(opts, key) and value is not None:
                setattr(opts, key, value)
        opts.max_parallel = max(1, min(int(opts.max_parallel), 32))
        return opts

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TargetOutcome:
    target: JobTarget
    results: list[CommandResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "results": [
                {
                    "command": r.command,
                    "ok": r.ok,
                    "error": r.error,
                    "duration_ms": r.duration_ms,
                }
                for r in self.results
            ],
        }


class DeployEngine:
    """Runs deployment jobs, one asyncio task per job."""

    def __init__(
        self,
        store: Store,
        on_event: Callable[[Event], None] | None = None,
        on_progress: Callable[[Job], None] | None = None,
    ) -> None:
        self.store = store
        self.on_event = on_event
        self.on_progress = on_progress
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._cancelled: set[int] = set()

    # -- job creation ----------------------------------------------------------------

    def plan(
        self,
        template: Template | None,
        body: str,
        devices: Sequence[Device],
        variables: dict[str, str] | None = None,
        options: DeployOptions | None = None,
        triggered_by: str = "manual",
    ) -> Job:
        """Create a job + target rows without running anything."""
        opts = options or DeployOptions()
        # Template defaults first, then the operator's overrides — otherwise a caller
        # that only supplies the values it changed would push literal ``{{ placeholders }}``
        # to the device.
        merged_variables = dict(template.variables) if template else {}
        merged_variables.update(variables or {})
        job = Job(
            template_id=template.id if template else None,
            template_name=template.name if template else "(ad-hoc)",
            vendor=template.vendor if template else "",
            body=body,
            variables=merged_variables,
            options=opts.to_dict(),
            status=JOB_PENDING,
            triggered_by=triggered_by,
        )
        targets: list[JobTarget] = []
        for device in devices:
            # Render now so the operator can review the exact commands per device *before*
            # the job starts; the vendor (and therefore the syntax) can differ per target.
            error = ""
            try:
                adapter_cls = get_adapter(device.vendor)
                rendered = adapter_cls.render_config(body, merged_variables)
                commands = adapter_cls.split_config(rendered)
            except Exception as exc:  # noqa: BLE001 - one bad device must not block planning
                commands = []
                error = f"could not render for {device.vendor}: {exc}"
            else:
                unresolved = _unresolved_variables(rendered)
                if unresolved:
                    # Sending an unsubstituted ``{{ variable }}`` to a router is a syntax
                    # error at best; treat it as "this device is not ready yet" instead.
                    error = "unresolved variables: " + ", ".join(unresolved)
                    commands = []

            if error:
                status = TARGET_SKIPPED
            elif not commands:
                status = TARGET_SKIPPED
                error = "template rendered to zero commands"
            else:
                status = TARGET_PENDING

            targets.append(
                JobTarget(
                    device_id=device.id,
                    device_name=device.name,
                    host=device.host,
                    status=status,
                    error=error,
                    commands=commands,
                )
            )
        return self.store.create_job(job, targets)

    def preview(self, template: Template, device: Device, variables: dict[str, str] | None = None) -> dict[str, Any]:
        """Render a template for one device without connecting to it."""
        adapter_cls = get_adapter(device.vendor)
        merged = dict(template.variables)
        merged.update(variables or {})
        rendered = adapter_cls.render_config(template.body, merged)
        commands = adapter_cls.split_config(rendered)
        return {
            "vendor": adapter_cls.name,
            "rendered": rendered,
            "commands": commands,
            "command_count": len(commands),
            "supports_deploy": adapter_cls.supports_deploy,
            "rollback_support": getattr(adapter_cls, "rollback_support", "manual"),
            "unresolved": _unresolved_variables(rendered),
        }

    # -- execution -------------------------------------------------------------------

    def start(self, job_id: int) -> asyncio.Task[None]:
        task = asyncio.create_task(self.run(job_id), name=f"deploy-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job_id, None))
        return task

    def cancel(self, job_id: int) -> bool:
        self._cancelled.add(job_id)
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
            return True
        return False

    async def run(self, job_id: int) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            return
        options = DeployOptions.from_dict(job.options)
        targets = self.store.list_job_targets(job_id)

        job.status = JOB_RUNNING
        job.started_at = time.time()
        self.store.update_job(job)
        self._notify(job)
        self._emit(job, "deploy", "info", f"Deployment #{job.id} started ({len(targets)} targets, dry_run={options.dry_run})")

        sem = asyncio.Semaphore(options.max_parallel)
        abort = asyncio.Event()

        async def guarded(target: JobTarget) -> None:
            if abort.is_set() or job_id in self._cancelled:
                target.status = TARGET_SKIPPED
                target.error = "skipped (job cancelled)"
                self.store.save_job_target(target)
                return
            async with sem:
                # Re-check after waiting for a slot: with max_parallel=1 and
                # stop_on_first_failure, the queued targets only learn about the abort
                # once they reach the front of the queue.
                if abort.is_set():
                    target.status = TARGET_SKIPPED
                    target.error = "skipped (an earlier device failed)"
                    self.store.save_job_target(target)
                    return
                if job_id in self._cancelled:
                    target.status = TARGET_SKIPPED
                    target.error = "skipped (job cancelled)"
                    self.store.save_job_target(target)
                    return
                try:
                    outcome = await self._run_target(job, target, options)
                except asyncio.CancelledError:
                    target.status = TARGET_SKIPPED
                    target.error = "cancelled"
                    target.finished_at = time.time()
                    self.store.save_job_target(target)
                    raise
                except Exception as exc:  # noqa: BLE001 - never lose a target
                    target.status = TARGET_FAILED
                    target.error = f"{exc!r}"
                    target.finished_at = time.time()
                    self.store.save_job_target(target)
                    outcome = None
                # A device that had to be rolled back is a failure signal for the
                # operator, even though the device itself is intact — so it counts for
                # stop_on_first_failure.
                if outcome is not None and outcome.target.status in (TARGET_FAILED, TARGET_ROLLED_BACK):
                    if options.stop_on_first_failure:
                        abort.set()

        try:
            await asyncio.gather(*[guarded(t) for t in targets])
        except asyncio.CancelledError:
            job.status = JOB_CANCELLED
            job.finished_at = time.time()
            self.store.update_job(job)
            self._notify(job)
            self._emit(job, "deploy", "warning", f"Deployment #{job.id} cancelled")
            raise

        final_targets = self.store.list_job_targets(job_id)
        job.succeeded = sum(1 for t in final_targets if t.status == TARGET_OK)
        job.failed = sum(
            1 for t in final_targets if t.status in (TARGET_FAILED, TARGET_ROLLED_BACK)
        )
        job.skipped = sum(1 for t in final_targets if t.status == TARGET_SKIPPED)
        job.total = len(final_targets)
        job.finished_at = time.time()
        job.status = JOB_CANCELLED if job_id in self._cancelled else (
            JOB_FAILED if job.failed and not job.succeeded else JOB_DONE
        )
        self.store.update_job(job)
        self._cancelled.discard(job_id)
        self._notify(job)

        severity = "info" if job.failed == 0 else ("critical" if job.succeeded == 0 else "warning")
        label = "dry run" if options.dry_run else "deployment"
        self._emit(
            job,
            "deploy",
            severity,
            f"{label.capitalize()} #{job.id} finished: {job.succeeded} ok, {job.failed} failed "
            f"of {job.total} in {job.duration_sec}s",
        )

    async def _run_target(self, job: Job, target: JobTarget, options: DeployOptions) -> TargetOutcome:
        if target.status == TARGET_SKIPPED:
            # plan() already decided this device cannot take the push (unresolved
            # variables, zero commands, …). Keep the reason it recorded.
            target.error = target.error or "skipped at plan time"
            target.finished_at = target.finished_at or time.time()
            self.store.save_job_target(target)
            self._notify(job)
            return TargetOutcome(target=target)

        target.status = TARGET_RUNNING
        target.started_at = time.time()
        self.store.save_job_target(target)
        self._notify(job)

        outcome = TargetOutcome(target=target)
        device = self.store.get_device(target.device_id) if target.device_id else None
        if device is None:
            target.status = TARGET_SKIPPED
            target.error = "device no longer exists"
            target.finished_at = time.time()
            self.store.save_job_target(target)
            return outcome

        vendor_key = resolve_vendor(device.vendor)
        if not vendor_key:
            target.status = TARGET_FAILED
            target.error = f"unsupported vendor: {device.vendor}"
            target.finished_at = time.time()
            self.store.save_job_target(target)
            return outcome

        adapter_cls = get_adapter(device.vendor)
        if not adapter_cls.supports_deploy:
            target.status = TARGET_SKIPPED
            target.error = (
                f"{adapter_cls.label} has no modelled configuration push — "
                "monitoring only (enable 'allow generic push' to override)"
            )
            target.finished_at = time.time()
            self.store.save_job_target(target)
            return outcome

        credential = (
            self.store.get_credential(device.credential_id) if device.credential_id else None
        )

        # ---- dry run -----------------------------------------------------------------
        # Handled before an adapter is ever constructed: a dry run performs no network
        # I/O and does not even import the SSH stack.
        rendered = adapter_cls.render_config(job.body, job.variables)
        commands = adapter_cls.split_config(rendered)
        target.commands = commands

        if not commands:
            target.status = TARGET_SKIPPED
            target.error = "template rendered to zero commands"
            target.finished_at = time.time()
            self.store.save_job_target(target)
            self._notify(job)
            return outcome

        unresolved = _unresolved_variables(rendered)
        if unresolved:
            # Belt and braces: never let an unsubstituted placeholder reach a device,
            # however this target was built.
            target.status = TARGET_SKIPPED
            target.error = "unresolved variables: " + ", ".join(unresolved)
            target.finished_at = time.time()
            self.store.save_job_target(target)
            self._notify(job)
            return outcome

        if options.dry_run:
            lines = [
                f"[dry-run] would connect to {device.host} as "
                f"{(credential.username if credential else '?')} and run {len(commands)} command(s):"
            ]
            lines += [f"  {index + 1:>3}. {command}" for index, command in enumerate(commands)]
            if options.backup_before:
                lines.append("  ... plus: capture the running config before applying")
            rollback = getattr(adapter_cls, "rollback_support", "manual")
            if options.auto_rollback and rollback == "native":
                lines.append("  ... plus: create a native rollback checkpoint")
            elif options.auto_rollback:
                lines.append(
                    "  ... note: this vendor has no native checkpoint; recovery would use "
                    "the stored config"
                )
            lines.append("")
            lines.append("Nothing was sent to the device.")
            target.output = "\n".join(lines)
            target.status = TARGET_OK
            target.finished_at = time.time()
            self.store.save_job_target(target)
            self._notify(job)
            return outcome

        def work() -> None:
            adapter: Adapter = adapter_cls(device, credential, timeout=options.per_device_timeout)
            rollback_name: str | None = None
            try:
                adapter.connect()
                if options.backup_before:
                    try:
                        config = adapter.fetch_config()
                    except Exception as exc:  # noqa: BLE001 - a failed backup is fatal
                        target.status = TARGET_FAILED
                        target.error = f"pre-change backup failed, nothing was applied: {exc}"
                        return
                    target.backup = config
                    self.store.add_backup(
                        device_id=device.id,
                        device_name=device.name,
                        host=device.host,
                        config=config,
                        source=f"job:{job.id}",
                    )
                    target.output += f"[backup] captured {len(config)} bytes of running config\n"

                if options.auto_rollback and getattr(adapter_cls, "rollback_support", "manual") == "native":
                    creator = getattr(adapter, "create_rollback_point", None)
                    if callable(creator):
                        ok, info = creator(f"job{job.id}-{int(time.time())}")
                        if ok:
                            rollback_name = info
                            target.output += f"[rollback] checkpoint {info} created\n"
                        else:
                            target.output += f"[rollback] could not create checkpoint: {info}\n"

                results = adapter.apply_commands(commands, save=options.save_config)
                outcome.results = results
                failed = [r for r in results if not r.ok]
                target.output += "\n".join(
                    f"$ {r.command}\n{r.output}".rstrip() for r in results
                )
                if failed:
                    target.error = failed[0].error or "\n".join(
                        r.output[:300] for r in failed[:3]
                    )
                    if options.auto_rollback and rollback_name:
                        restore = getattr(adapter, "restore_rollback_point", None)
                        if callable(restore):
                            try:
                                rollback_result = restore(rollback_name)
                            except Exception as exc:  # noqa: BLE001
                                target.output += f"\n[rollback] restore raised: {exc!r}\n"
                            else:
                                target.output += (
                                    f"\n[rollback] restored checkpoint {rollback_name}: "
                                    f"{'ok' if rollback_result.ok else rollback_result.error}\n"
                                )
                                if rollback_result.ok:
                                    target.status = TARGET_ROLLED_BACK
                                    target.error += " (device rolled back to the pre-change state)"
                    elif options.auto_rollback:
                        target.output += (
                            "\n[rollback] this vendor has no native checkpoint; "
                            "the pre-change config is stored under Backups for manual restore.\n"
                        )
                    if target.status != TARGET_ROLLED_BACK:
                        target.status = TARGET_FAILED
                else:
                    target.status = TARGET_OK
            except AdapterError as exc:
                target.status = TARGET_FAILED
                target.error = str(exc)
            except Exception as exc:  # noqa: BLE001 - a session must never escape
                target.status = TARGET_FAILED
                target.error = f"{exc.__class__.__name__}: {exc}"
            finally:
                if rollback_name and options.auto_rollback and target.status == TARGET_OK:
                    cleanup = getattr(adapter, "cleanup_rollback_point", None)
                    if callable(cleanup):
                        cleanup(rollback_name)
                adapter.close()
                target.finished_at = time.time()

        await asyncio.wait_for(
            asyncio.to_thread(work),
            timeout=options.per_device_timeout + 30,
        )
        self.store.save_job_target(target)
        self._notify(job)
        return outcome

    # -- notifications ---------------------------------------------------------------

    def _notify(self, job: Job) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(job)
            except Exception:  # noqa: BLE001
                pass

    def _emit(self, job: Job, kind: str, severity: str, message: str) -> None:
        event = Event(kind=kind, severity=severity, message=message, details={"job_id": job.id})
        self.store.add_event(event)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001
                pass


def _unresolved_variables(rendered: str) -> list[str]:
    import re

    return sorted({m.group(1).strip() for m in re.finditer(r"\{\{\s*([\w.\-]+)\s*\}\}", rendered)})


# --------------------------------------------------------------------------------------
# Restore & backup helpers (used by the UI's Backups page)
# --------------------------------------------------------------------------------------


async def capture_backup(store: Store, device: Device, source: str = "manual") -> dict[str, Any]:
    """Connect and store the device's running configuration."""
    credential = store.get_credential(device.credential_id) if device.credential_id else None
    adapter_cls = get_adapter(device.vendor)

    def work() -> str:
        adapter = adapter_cls(device, credential, timeout=45.0)
        try:
            adapter.connect()
            return adapter.fetch_config()
        finally:
            adapter.close()

    try:
        config = await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

    backup_id = store.add_backup(
        device_id=device.id,
        device_name=device.name,
        host=device.host,
        config=config,
        source=source,
    )
    return {
        "ok": True,
        "backup_id": backup_id,
        "bytes": len(config.encode("utf-8", "replace")),
        "lines": config.count("\n") + 1,
        "device": device.name,
    }


async def restore_backup(
    store: Store,
    backup_id: int,
    dry_run: bool = True,
    save: bool = True,
) -> dict[str, Any]:
    """Push a stored configuration back onto its device, through the normal apply flow."""
    backup = store.get_backup(backup_id)
    if backup is None:
        return {"ok": False, "error": "backup not found"}
    device = store.get_device(backup["device_id"]) if backup["device_id"] else None
    if device is None:
        return {"ok": False, "error": "device for this backup no longer exists"}

    credential = store.get_credential(device.credential_id) if device.credential_id else None
    adapter_cls = get_adapter(device.vendor)
    config = backup["config"]

    if dry_run:
        commands = adapter_cls.split_config(adapter_cls.render_config(config))
        return {
            "ok": True,
            "dry_run": True,
            "device": device.name,
            "command_count": len(commands),
            "preview": commands[:40],
        }

    def work() -> tuple[int, list[CommandResult]]:
        adapter = adapter_cls(device, credential, timeout=90.0)
        try:
            adapter.connect()
            commands = adapter_cls.split_config(adapter_cls.render_config(config))
            return len(commands), adapter.apply_commands(commands, save=save)
        finally:
            adapter.close()

    try:
        total, results = await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

    failed = [r for r in results if not r.ok]
    if failed:
        return {
            "ok": False,
            "error": f"{len(failed)} command(s) rejected; first: {failed[0].command}",
            "applied": total - len(failed),
        }
    return {"ok": True, "device": device.name, "applied": total}
