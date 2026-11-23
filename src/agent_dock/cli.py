"""Command-line entry point: ``agent-dock`` / ``python -m agent_dock``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from . import __version__, paths
from . import settings as settings_mod


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_run(args: argparse.Namespace) -> int:
    from .broker.server import AlreadyRunning, Broker, BrokerThread

    cfg = settings_mod.load()
    broker = Broker(cfg)
    bt = BrokerThread(broker)
    try:
        bt.start()
    except AlreadyRunning as exc:
        print(f"agent-dock: {exc}", file=sys.stderr)
        return 0  # not a crash: keep launchd from restart-looping
    if args.demo:
        from .simulate import run_samples

        threading.Thread(
            target=run_samples,
            args=(4, 2.0),
            kwargs={"printer": lambda s: logging.info(s)},
            daemon=True,
        ).start()
    try:
        if args.console or not paths.is_macos():
            from .ui.console import ConsoleUI

            ConsoleUI(bt).run()
        else:
            from .ui.app import run_app

            run_app(bt, cfg)
    except KeyboardInterrupt:
        pass
    finally:
        bt.stop()
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from .install import InstallError, install_all

    cfg = settings_mod.load()
    python = Path(args.python) if args.python else Path(sys.executable)
    try:
        for msg in install_all(
            python=python,
            claude=not args.no_claude,
            codex=not args.no_codex,
            launch_agent=not args.no_launch_agent,
            timeout=cfg.hook_timeout_seconds,
        ):
            print(msg)
    except InstallError as exc:
        print(f"agent-dock install failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    from .install import InstallError, uninstall_all

    try:
        for msg in uninstall_all(
            claude=not args.no_claude,
            codex=not args.no_codex,
            launch_agent=not args.no_launch_agent,
        ):
            print(msg)
    except InstallError as exc:
        print(f"agent-dock uninstall finished with errors: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .doctor import run

    return run()


def cmd_simulate(args: argparse.Namespace) -> int:
    from .simulate import build_stdin, describe, run_hook, run_samples

    if args.command or args.tool:
        tool = args.tool or "Bash"
        tool_input = {"command": args.command} if args.command else {}
        data = build_stdin(
            agent=args.agent,
            tool_name=tool,
            tool_input=tool_input,
            cwd=args.cwd,
            suggest=args.suggest,
        )
        print(describe(run_hook(args.agent, data)))
        return 0
    run_samples(args.count, args.interval)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-dock", description=__doc__)
    p.add_argument("--version", action="version", version=f"agent-dock {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="start the broker and menu-bar UI (default)")
    r.add_argument("--console", action="store_true", help="terminal UI instead of AppKit")
    r.add_argument("--demo", action="store_true", help="also fire a few simulated requests")
    r.set_defaults(func=cmd_run)

    for name, fn, helptext in (
        ("install", cmd_install, "add hooks to Claude/Codex config and start at login"),
        ("uninstall", cmd_uninstall, "remove Agent Dock hooks and the LaunchAgent"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--no-claude", action="store_true")
        sp.add_argument("--no-codex", action="store_true")
        sp.add_argument("--no-launch-agent", action="store_true")
        if name == "install":
            sp.add_argument("--python", help="interpreter for hooks (default: this one)")
        sp.set_defaults(func=fn)

    for name in ("doctor", "status"):
        sub.add_parser(name, help="diagnose installation and broker").set_defaults(func=cmd_status)

    s = sub.add_parser("simulate", help="send synthetic permission requests through the real hook")
    s.add_argument("--agent", choices=["claude", "codex"], default="claude")
    s.add_argument("--tool")
    s.add_argument("--command")
    s.add_argument("--cwd", default="~/projects/demo")
    s.add_argument(
        "--suggest", action="store_true", help="include a native always-allow suggestion"
    )
    s.add_argument("--count", type=int, default=3)
    s.add_argument("--interval", type=float, default=1.0)
    s.set_defaults(func=cmd_simulate)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args(["run", *(argv or sys.argv[1:])])
    _setup_logging(args.verbose)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
