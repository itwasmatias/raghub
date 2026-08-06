"""Disabled-by-default local Windows display helper."""

import argparse
import json

from federation.power_action import PowerAction
from federation.windows_display_native import (
    PlatformProbe,
    SystemPlatformProbe,
)


VERSION = "0.1"


def _parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument(
        "--action",
        choices=(PowerAction.DISPLAY_OFF.value, PowerAction.DISPLAY_ON.value),
    )
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--confirm-real-execution", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--minimum-idle-seconds", type=float)
    parser.add_argument("--allow-when-locked", action="store_true")
    return parser


def main(argv=None, *, platform=None):
    args = _parser().parse_args(argv)
    if args.help:
        print(
            "Windows display helper 0.1; disabled unless explicitly enabled. "
            "Actions: display_off, display_on."
        )
        return 0
    if args.version:
        print(f"windows-display-helper {VERSION} (disabled by default)")
        return 0
    platform = platform or SystemPlatformProbe()
    if not isinstance(platform, PlatformProbe):
        raise TypeError("platform must be a PlatformProbe")
    if not platform.is_windows():
        print(json.dumps({"ok": False, "code": "unsupported_platform"}))
        return 2
    if args.dry_run and args.action is not None:
        print(
            json.dumps(
                {
                    "ok": True,
                    "code": "dry_run",
                    "attempted": False,
                    "action": args.action,
                },
                sort_keys=True,
            )
        )
        return 0
    if not args.enable or not args.confirm_real_execution or args.action is None:
        print(json.dumps({"ok": False, "code": "disabled"}))
        return 2
    print(json.dumps({"ok": False, "code": "governed_composition_required"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
