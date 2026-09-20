"""netpilot command line.

Two audiences:

* **Interactive use** — ``netpilot web`` / ``netpilot gui`` start a UI; ``netpilot monitor``
  runs the headless engine (systemd/Docker friendly).
* **Scripting** — ``netpilot devices``, ``netpilot deploy``, ``netpilot backup`` are
  machine-readable (``--json``) so netpilot can live inside an existing automation chain.

Run ``netpilot doctor`` first on a new host: it checks ICMP capability, SNMP availability
and the data directory, which are the three things that most often surprise people.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Any

from . import __version__

log = logging.getLogger("netpilot")


# --------------------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------------------


class Palette:
    """Tiny ANSI helper that degrades to plain text when not on a TTY."""

    enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    @classmethod
    def wrap(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text

    @classmethod
    def green(cls, text: str) -> str:
        return cls.wrap("32", text)

    @classmethod
    def red(cls, text: str) -> str:
        return cls.wrap("31", text)

    @classmethod
    def yellow(cls, text: str) -> str:
        return cls.wrap("33", text)

    @classmethod
    def dim(cls, text: str) -> str:
        return cls.wrap("2", text)

    @classmethod
    def bold(cls, text: str) -> str:
        return cls.wrap("1", text)


STATE_MARK = {
    "up": Palette.green("● up"),
    "down": Palette.red("● down"),
    "degraded": Palette.yellow("● degraded"),
    "unknown": Palette.dim("○ unknown"),
}


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _emit(payload: Any, as_json: bool) -> bool:
    """Print JSON when asked; return True when the caller should skip table output."""
    if as_json:
        _print_json(payload)
        return True
    return False


def _table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("─" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def _app(args: argparse.Namespace, start_monitor: bool = True):
    from .core import App

    data_dir = args.data_dir or None
    return App(data_dir=data_dir, start_monitor=start_monitor)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check that this host can actually do what netpilot needs."""
    from .adapters.registry import available_vendors
    from .monitoring.icmp import icmp_available
    from .security import load_or_create_key

    checks: list[tuple[str, bool, str]] = []

    # Prove ICMP by actually sending an echo to the loopback address: testing for the
    # presence of a `ping` binary is not the same as being allowed to use it.
    from .monitoring.icmp import ping as icmp_ping

    icmp_ok, icmp_why = icmp_available()
    probe = icmp_ping("127.0.0.1", count=1, timeout=1.0)
    works = probe.received > 0
    if works:
        detail = f"echo works via {probe.method}"
    else:
        detail = probe.error or icmp_why
    checks.append(("ICMP", works, detail))

    try:
        import pysnmp  # noqa: F401

        checks.append(("SNMP", True, "pysnmp available"))
    except ImportError:
        checks.append(("SNMP", False, 'pysnmp missing — pip install "netpilot[snmp]"'))

    try:
        import paramiko

        checks.append(("SSH", True, f"paramiko {paramiko.__version__}"))
    except ImportError:
        checks.append(("SSH", False, "paramiko missing — device sessions will not work"))

    app = _app(args, start_monitor=False)
    try:
        app.store.set_setting("doctor", time.time())
        checks.append(("Storage", True, f"{app.store.path} (writable)"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("Storage", False, f"{exc}"))
    finally:
        app.store.close()

    key_ok = True
    key_note = "encrypted credential storage active"
    try:
        load_or_create_key(args.data_dir or os.path.expanduser("~/.netpilot"))
    except Exception as exc:  # noqa: BLE001
        key_ok, key_note = False, str(exc)
    checks.append(("Secrets", key_ok, key_note))

    if args.json:
        _print_json([{"check": c, "ok": ok, "detail": d} for c, ok, d in checks])
    else:
        print(Palette.bold(f"netpilot {__version__} — environment check\n"))
        for name, ok, detail in checks:
            mark = Palette.green("PASS") if ok else Palette.red("FAIL")
            print(f"  [{mark}] {name:<8} {detail}")
        print()
        vendors = ", ".join(v["label"] for v in available_vendors())
        print(f"  Vendors: {vendors}")
        if any(not ok for _n, ok, _d in checks):
            print(
                "\n"
                + Palette.yellow(
                    "  ICMP is optional: set params.fallback on an ICMP check to probe TCP instead."
                )
            )
            return 1
    return 0 if all(ok for _n, ok, _d in checks) else 1


def cmd_devices(args: argparse.Namespace) -> int:
    app = _app(args, start_monitor=False)
    try:
        if args.add:
            return _add_device(app, args)
        devices = app.store.list_devices(search=args.search)
        cards = app.store.device_cards()
        states = app.store.all_states()

        payload = []
        for card in cards:
            if args.search and not any(d.id == card["id"] for d in devices):
                continue
            payload.append(
                {
                    "id": card["id"],
                    "name": card["name"],
                    "host": card["host"],
                    "vendor": card["vendor"],
                    "site": card["site"],
                    "tags": card["tags"],
                    "state": card["state"]["state"],
                    "latency_ms": card["state"]["last_latency_ms"],
                    "availability_24h": card["availability_24h"],
                    "enabled": card["enabled"],
                }
            )
        if _emit(payload, args.json):
            return 0

        rows = []
        for item in payload:
            mark = STATE_MARK.get(item["state"], item["state"])
            latency = f"{item['latency_ms']:.1f} ms" if item["latency_ms"] is not None else "—"
            avail = f"{item['availability_24h']}%" if item["availability_24h"] is not None else "—"
            rows.append(
                [
                    item["id"],
                    item["name"],
                    item["host"],
                    item["vendor"],
                    mark,
                    latency,
                    avail,
                    ",".join(item["tags"]),
                ]
            )
        print(
            _table(
                [[str(c) for c in r] for r in rows],
                ["ID", "NAME", "HOST", "VENDOR", "STATE", "LATENCY", "24H", "TAGS"],
            )
        )
        print(Palette.dim(f"\n{len(rows)} device(s) in {app.store.path}"))
    finally:
        app.store.close()
    return 0


def _add_device(app: Any, args: argparse.Namespace) -> int:
    host = args.add or args.host
    payload = {
        "name": args.name or host,
        "host": host,
        "vendor": args.vendor,
        "ssh_port": args.ssh_port,
        "tags": [t for t in (args.tag or "").split(",") if t],
        "site": args.site or "",
        "notes": args.notes or "",
        "mgmt_url": args.mgmt_url,
    }
    if args.credential:
        found = [c for c in app.store.list_credentials() if c.name == args.credential]
        if not found:
            names = ", ".join(c.name for c in app.store.list_credentials()) or "(none)"
            print(Palette.red(f"no credential named {args.credential!r}. Available: {names}"))
            return 2
        payload["credential_id"] = found[0].id

    from .db import DeviceExistsError

    try:
        device = _run(app.add_device(payload))
    except DeviceExistsError as exc:
        print(Palette.red(f"already in the inventory: {exc}"))
        return 2

    if args.test:
        print(f"Testing {device.name} ({device.host}) ...")
        result = _run(app.test_device(device.id))
        if result.get("ok"):
            print(Palette.green(f"  connected in {result['elapsed_ms']} ms — {result['summary']}"))
        else:
            print(Palette.red(f"  failed: {result.get('error')}"))
            return 1

    if args.json:
        _print_json(device.to_dict())
    else:
        print(f"{Palette.green('added')} device #{device.id} {device.name} ({device.host}, {device.vendor})")
        checks = app.store.list_checks(device_id=device.id)
        print(Palette.dim(f"  {len(checks)} monitor(s) attached automatically:"))
        for check in checks:
            print(Palette.dim(f"    - {check.kind:<5} {check.label} every {check.interval_sec}s"))
        print(Palette.dim("  it is being monitored from now on; run 'netpilot devices' to see state"))
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Run the monitoring engine in the foreground (no web server)."""
    app = _app(args, start_monitor=True)

    def on_event(event: Any) -> None:
        if args.json:
            print(json.dumps(event.to_dict(), default=str), flush=True)
        else:
            stamp = time.strftime("%H:%M:%S", time.localtime(event.ts))
            colour = {"critical": Palette.red, "warning": Palette.yellow, "info": Palette.green}.get(
                event.severity, lambda t: t
            )
            print(f"{Palette.dim(stamp)} {colour(event.severity.ljust(8))} {event.message}", flush=True)

    app.events.on_event.append(on_event)

    async def main() -> int:
        await app.start()
        count = len(app.store.list_checks(enabled_only=True))
        if not args.json:
            print(Palette.bold(f"netpilot monitor — {count} check(s) scheduled"))
            print(Palette.dim(f"database: {app.store.path}"))
            print(Palette.dim("Ctrl-C to stop\n"))
        stop = asyncio.Event()

        def handle_signal(*_: Any) -> None:
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handle_signal)
            except NotImplementedError:  # pragma: no cover - Windows
                signal.signal(sig, handle_signal)

        await stop.wait()
        if not args.json:
            print(Palette.dim(f"\nstopping — {app.monitor.checks_run} checks run"))
        await app.stop()
        return 0

    return _run(main())


def cmd_deploy(args: argparse.Namespace) -> int:
    app = _app(args, start_monitor=False)

    async def main() -> int:
        body = args.body
        if args.file:
            with open(args.file) as handle:
                body = handle.read()
        # Only the operator's overrides are collected here; the template's own defaults
        # are merged in by the deploy engine, so we never mutate the library.
        variables: dict[str, str] = {}
        for pair in args.var or []:
            key, _, value = pair.partition("=")
            if key.strip():
                variables[key.strip()] = value.strip()

        template_id = None
        if args.template:
            matches = [t for t in app.template_library() if t["name"] == args.template]
            if not matches:
                print(Palette.red(f"no template named {args.template!r}"))
                print(Palette.dim("  run 'netpilot templates' to see what is available"))
                return 2
            template = matches[0]
            template_id = template["id"]
            body = template["body"]
            vendor = template["vendor"]
        else:
            vendor = args.vendor or "mikrotik"

        if not body:
            print(Palette.red("nothing to deploy: pass --template, --body or --file"))
            return 2

        if not (args.device or args.tag or args.all):
            # Pushing to the whole inventory because a flag was forgotten is not a
            # recoverable mistake, so an explicit selector is required.
            print(Palette.red("no targets selected"))
            print(Palette.dim("  choose with --device ID, --tag NAME, or --all"))
            return 2

        job = app.create_deploy(
            body=body,
            vendor=vendor,
            device_ids=[int(i) for i in (args.device or [])] or None,
            tags=[t for t in (args.tag or "").split(",") if t],
            match_all=args.all,
            variables=variables,
            options={
                "dry_run": args.dry_run,
                "save_config": not args.no_save,
                "backup_before": not args.no_backup,
                "auto_rollback": not args.no_rollback,
                "max_parallel": args.parallel,
                "stop_on_first_failure": args.stop_on_error,
            },
            template_id=template_id,
            template_name=args.template,
            triggered_by="cli",
        )
        if not args.json:
            # --json output must stay pure JSON on stdout, so the human summary is
            # suppressed entirely rather than merely being interleaved.
            print(
                f"{Palette.bold(f'job #{job.id}')} queued for {job.total} device(s)"
                f"{' (DRY RUN)' if args.dry_run else ''}"
            )
            if args.dry_run:
                print(Palette.yellow("  dry run: nothing will be written to any device"))

        await app.run_deploy(job.id)
        detail = app.job_detail(job.id)

        if _emit(detail, args.json):
            return 0 if detail["failed"] == 0 else 1

        for target in detail["targets"]:
            status = target["status"]
            colour = {
                "ok": Palette.green,
                "failed": Palette.red,
                "rolled-back": Palette.yellow,
                "skipped": Palette.dim,
            }.get(status, lambda t: t)
            print(f"\n{colour(status.upper().ljust(12))} {target['device_name']} ({target['host']})")
            if target.get("error"):
                print(f"  {Palette.red('error')}: {target['error'][:400]}")
            output = (target.get("output") or "").strip()
            if output and (args.verbose or status != "ok"):
                for line in output.splitlines()[:40]:
                    # keep genuine blank lines blank rather than printing stray spaces
                    print(f"  {line}" if line.strip() else "")
        print()
        skipped = detail.get("skipped") or 0
        summary = f"result: {detail['succeeded']} ok, {detail['failed']} failed"
        if skipped:
            summary += f", {skipped} skipped"
        print(f"{summary} of {detail['total']}")
        return 0 if detail["failed"] == 0 else 1

    return _run(main())


def cmd_backup(args: argparse.Namespace) -> int:
    app = _app(args, start_monitor=False)

    async def main() -> int:
        if args.list:
            backups = app.store.list_backups(device_id=args.device, limit=args.limit)
            if _emit(backups, args.json):
                return 0
            rows = [
                [
                    b["id"],
                    b["device_name"],
                    b["host"],
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(b["created_at"])),
                    f"{b['byte_size'] / 1024:.1f} KiB",
                    b["source"],
                ]
                for b in backups
            ]
            print(_table([[str(c) for c in r] for r in rows], ["ID", "DEVICE", "HOST", "WHEN", "SIZE", "SOURCE"]))
            return 0

        # A backup only reads, so an empty selection simply means "everything enabled".
        selector_given = bool(args.device or args.tag)
        targets = app.resolve_targets(
            [int(i) for i in (args.device or [])] or None,
            [t for t in (args.tag or "").split(",") if t],
            match_all=args.all or not selector_given,
        )
        if not targets:
            print(Palette.red("no devices matched"))
            return 2

        failures = 0
        results: list[dict[str, Any]] = []
        for device in targets:
            result = await app.backup_device(device.id)
            result["device"] = device.name
            result["host"] = device.host
            results.append(result)
            if result.get("ok"):
                if not args.json:
                    print(
                        f"{Palette.green('captured')} {device.name:<20} "
                        f"{result['bytes']:>8} bytes  ({result['lines']} lines)"
                    )
            else:
                failures += 1
                if not args.json:
                    print(f"{Palette.red('failed  ')} {device.name:<20} {result['error'][:120]}")
        if args.json:
            print(json.dumps(results, indent=2, default=str))
            return 0 if failures == 0 else 1
        print()
        print(f"{len(targets) - failures}/{len(targets)} captured")
        return 0 if failures == 0 else 1

    return _run(main())


def cmd_templates(args: argparse.Namespace) -> int:
    app = _app(args, start_monitor=False)
    try:
        if args.show:
            match = [t for t in app.template_library() if t["name"] == args.show]
            if not match:
                print(Palette.red(f"no template named {args.show!r}"))
                return 2
            template = match[0]
            if args.json:
                _print_json(template)
                return 0
            print(Palette.bold(f"{template['name']}  [{template['vendor']}]"))
            print(Palette.dim(template["description"]))
            print()
            print(template["body"])
            print()
            if template["variables"]:
                print(Palette.dim("variables:"))
                for key, value in template["variables"].items():
                    print(f"  {key} = {value}")
            return 0

        library = app.template_library(vendor=args.vendor)
        if _emit(library, args.json):
            return 0
        rows = [
            [t["name"], t["vendor"], "built-in" if t["builtin"] else "custom", len(t["variables"]), t["description"][:52]]
            for t in library
        ]
        print(_table([[str(c) for c in r] for r in rows], ["NAME", "VENDOR", "ORIGIN", "VARS", "DESCRIPTION"]))
        print(Palette.dim(f"\n{len(rows)} template(s) — 'netpilot templates --show <name>' prints the body"))
    finally:
        app.store.close()
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    app = _app(args, start_monitor=False)
    try:
        events = [
            e.to_dict()
            for e in app.store.list_events(
                limit=args.limit, device_id=args.device, severity=args.severity, unacknowledged_only=args.unacked
            )
        ]
        if _emit(events, args.json):
            return 0
        rows = [
            [
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"])),
                e["severity"],
                e["kind"],
                e["device_name"],
                e["message"][:70],
            ]
            for e in events
        ]
        print(_table([[str(c) for c in r] for r in rows], ["WHEN", "SEVERITY", "KIND", "DEVICE", "MESSAGE"]))
    finally:
        app.store.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print(Palette.red('the web UI needs extra dependencies: pip install "netpilot[web]"'))
        return 2
    from .web.app import run_server

    return run_server(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        open_browser=args.open,
        reload=args.reload,
        log_level=args.log_level,
    )


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .desktop.app import run_gui
    except ImportError as exc:
        print(Palette.red(f'the desktop UI needs PyQt6: pip install "netpilot[gui]" ({exc})'))
        return 2
    return run_gui(data_dir=args.data_dir)


def cmd_version(_args: argparse.Namespace) -> int:
    print(f"netpilot {__version__}")
    return 0


# --------------------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------------------


def _common_flags() -> argparse.ArgumentParser:
    """Flags accepted both before *and* after a subcommand.

    ``netpilot devices --json`` and ``netpilot --json devices`` should both work — people
    type them both ways. The trick is ``default=SUPPRESS``: without it the subparser would
    reset the value the top-level parser already parsed.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable JSON output",
    )
    common.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=argparse.SUPPRESS,
        help="extra detail",
    )
    common.add_argument(
        "--data-dir",
        default=argparse.SUPPRESS,
        help="storage directory (default: ~/.netpilot)",
    )
    common.add_argument(
        "--log-level",
        default=argparse.SUPPRESS,
        help="python logging level (default: warning)",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_flags()
    parser = argparse.ArgumentParser(
        prog="netpilot",
        description="Monitor your network and push configuration to it — from one inventory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  netpilot doctor                     check what this host can do
  netpilot web                        start the dashboard on http://127.0.0.1:8787
  netpilot devices --add 10.0.0.1 --vendor mikrotik --credential lab --test
  netpilot monitor                    run monitoring headless (systemd / docker)
  netpilot deploy --template "NTP servers" --tag core --dry-run
  netpilot deploy --template "NTP servers" --tag core
  netpilot backup --tag core
  netpilot events --unacked
""",
    )
    parser.add_argument("--version", action="version", version=f"netpilot {__version__}")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("NETPILOT_HOME"),
        help="storage directory (default: ~/.netpilot)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", "-v", action="store_true", help="extra detail")
    parser.add_argument("--log-level", default=os.environ.get("NETPILOT_LOG", "warning"))
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("doctor", parents=[common], help="verify ICMP/SNMP/SSH/storage on this host")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("devices", parents=[common], help="list, add and inspect devices")
    p.add_argument("--add", metavar="HOST", help="add a device by host or IP")
    p.add_argument("--name")
    p.add_argument("--vendor", default="mikrotik")
    p.add_argument("--ssh-port", type=int, default=22)
    p.add_argument("--credential", help="existing credential name to attach")
    p.add_argument("--tag", help="comma-separated tags")
    p.add_argument("--site")
    p.add_argument("--notes")
    p.add_argument("--mgmt-url", dest="mgmt_url", help="management URL to probe over HTTP(S)")
    p.add_argument("--test", action="store_true", help="SSH in and read identity right after adding")
    p.add_argument("--search", help="filter by name/host/site/tag")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("monitor", parents=[common], help="run the monitoring engine in the foreground")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("deploy", parents=[common], help="push configuration to many devices at once")
    p.add_argument("--template", help="template name from the library")
    p.add_argument("--body", help="inline configuration block")
    p.add_argument("--file", help="read the configuration block from a file")
    p.add_argument("--device", action="append", help="device id (repeatable)")
    p.add_argument("--tag", help="target devices carrying this tag")
    p.add_argument("--all", action="store_true", help="every enabled device (be deliberate)")
    p.add_argument("--vendor", default="mikrotik", help="vendor for ad-hoc bodies")
    p.add_argument("--var", action="append", metavar="KEY=VALUE", help="template variable (repeatable)")
    p.add_argument("--dry-run", action="store_true", default=False, help="render and report, change nothing")
    p.add_argument("--no-save", action="store_true", help="skip the vendor save step (write memory)")
    p.add_argument("--no-backup", action="store_true", help="skip capturing the running config first")
    p.add_argument("--no-rollback", action="store_true", help="skip creating a rollback checkpoint")
    p.add_argument("--parallel", type=int, default=5)
    p.add_argument("--stop-on-error", action="store_true")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("backup", parents=[common], help="capture device configurations")
    p.add_argument("--device", action="append", help="device id (repeatable)")
    p.add_argument("--tag", help="only devices with this tag")
    p.add_argument("--all", action="store_true", help="every enabled device (default)")
    p.add_argument("--list", action="store_true", help="list stored backups instead of capturing")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("templates", parents=[common], help="browse the configuration library")
    p.add_argument("--vendor", help="filter by vendor")
    p.add_argument("--show", metavar="NAME", help="print one template in full")
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("events", parents=[common], help="read the alert/event feed")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--device", type=int)
    p.add_argument("--severity", choices=["info", "warning", "critical"])
    p.add_argument("--unacked", action="store_true", help="only unacknowledged events")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("web", parents=[common], help="start the web dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--open", action="store_true", help="open a browser window")
    p.add_argument("--reload", action="store_true", help="auto-reload for development")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("gui", parents=[common], help="start the desktop application")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("version", parents=[common], help="print the version")
    p.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    # Shared flags may have been supplied after the subcommand; normalise them here so
    # every command can read args.json / args.verbose unconditionally.
    args.json = getattr(args, "json", False)
    args.verbose = getattr(args, "verbose", False)
    if not getattr(args, "data_dir", None):
        args.data_dir = os.environ.get("NETPILOT_HOME")
    if not getattr(args, "log_level", None):
        args.log_level = os.environ.get("NETPILOT_LOG", "warning")

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard for friendly CLI errors
        if getattr(args, "verbose", False):
            raise
        print(Palette.red(f"error: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
