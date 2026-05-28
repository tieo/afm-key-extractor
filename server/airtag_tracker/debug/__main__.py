"""CLI front-end for the snapshot+replay harness.

Usage (from inside the container, since QMP sockets are under /tmp)::

    docker exec afm-key-extractor-airtag-tracker-1 sh -c \
        "cd /app && PYTHONPATH=server uv run python -m airtag_tracker.debug <cmd> ..."

Commands:

    snapshot <label>       save VM state under <label>
    restore <label>        restore VM to <label>
    list                   list snapshots in the running VM
    delete <label>         delete <label>
    replay <state>         run a single handler against the VM
        [--restore <label>] [--apple-email EMAIL] [--apple-password PW]
    screendump <path>      dump current framebuffer to PPM

The CLI talks directly to the QEMU monitor / engine handlers — no HTTP.
Same operations are exposed via /api/debug/* if HTTP is preferred.
"""

from __future__ import annotations

import argparse
import json
import sys


def _cmd_snapshot(args: argparse.Namespace) -> int:
    from .. import vm
    print(json.dumps(vm.snapshot.save(args.label), indent=2))
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    from .. import vm
    print(json.dumps(vm.snapshot.load(args.label), indent=2))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from .. import vm
    rows = vm.snapshot.list_all()
    if not rows:
        print("(no snapshots)")
        return 0
    for r in rows:
        print(f"{r['id']:<4} {r['tag']:<32} {r['raw']}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    from .. import vm
    print(json.dumps(vm.snapshot.delete(args.label), indent=2))
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from ..automation import engine
    from ..automation.context import AutomationContext
    from ..automation.states import FlowKind, InstallState, RuntimeState
    from ..vm_password import ensure as ensure_vm_password
    from .. import vm

    state = _resolve_state(args.state)
    if state is None:
        print(f"unknown state: {args.state!r}", file=sys.stderr)
        return 2

    if args.restore:
        vm.snapshot.load(args.restore)

    from ..config import APPLE_EMAIL, APPLE_PASSWORD
    flow = FlowKind.INSTALL if isinstance(state, InstallState) else FlowKind.RUNTIME
    ctx = AutomationContext(
        flow_kind=flow,
        vm_password=ensure_vm_password(),
        apple_email=args.apple_email or APPLE_EMAIL,
        apple_password=args.apple_password or APPLE_PASSWORD,
        initial_state=state,
    )
    handler = engine._get_handler(state)
    print(f"running handler for {state.value}")
    try:
        next_state = handler(ctx)
    except Exception as e:
        print(f"handler raised: {e}", file=sys.stderr)
        return 1
    print(f"→ {next_state.value if next_state else 'None'}")
    return 0


def _cmd_screendump(args: argparse.Namespace) -> int:
    from .. import qmp
    qmp.screendump(args.path)
    print(f"wrote {args.path}")
    return 0


def _resolve_state(name: str):
    from ..automation.states import InstallState, RuntimeState
    try:
        return InstallState(name)
    except ValueError:
        pass
    try:
        return RuntimeState(name)
    except ValueError:
        pass
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="airtag-debug")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="save VM state under a label")
    s.add_argument("label")
    s.set_defaults(func=_cmd_snapshot)

    s = sub.add_parser("restore", help="restore VM to a labeled snapshot")
    s.add_argument("label")
    s.set_defaults(func=_cmd_restore)

    s = sub.add_parser("list", help="list snapshots in the running VM")
    s.set_defaults(func=_cmd_list)

    s = sub.add_parser("delete", help="delete a snapshot")
    s.add_argument("label")
    s.set_defaults(func=_cmd_delete)

    s = sub.add_parser("replay", help="run a single handler against the VM")
    s.add_argument("state", help="InstallState or RuntimeState value (e.g. sa_create_account)")
    s.add_argument("--restore", help="snapshot label to load before running")
    s.add_argument("--apple-email", default="")
    s.add_argument("--apple-password", default="")
    s.set_defaults(func=_cmd_replay)

    s = sub.add_parser("screendump", help="dump current framebuffer to a PPM file")
    s.add_argument("path")
    s.set_defaults(func=_cmd_screendump)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
