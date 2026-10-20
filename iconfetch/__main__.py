"""IconFetch.

  iconfetch                 open the window
  iconfetch list            show every app, its icon and what IconFetch found
  iconfetch apply [ID…]     apply the suggested icons (or the best find for each ID)
  iconfetch revert [ID…]    put back the original icons (all of IconFetch's changes without IDs)
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor


def _scan(ids: list[str]):
    from .apps import installed_apps
    from .sources import Finder

    apps = installed_apps()
    if ids:
        wanted = {i if i.endswith(".desktop") else i + ".desktop" for i in ids}
        apps = [a for a in apps if a.id in wanted]
        missing = wanted - {a.id for a in apps}
        for desktop_id in sorted(missing):
            print(f"{desktop_id}: no such app", file=sys.stderr)
    finder = Finder()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(finder.candidates, apps))
    return apps, results


def cmd_list(args) -> int:
    from .install import suggested

    apps, results = _scan(args.ids)
    for app, (found, problems) in zip(apps, results):
        best = found[0] if found else None
        mark = "*" if suggested(app, best) else ("✓" if app.applied else " ")
        find = f"{best.text} ({best.origin})" if best else "nothing found"
        if len(found) > 1:
            find += f"  +{len(found) - 1}: " + ", ".join(c.source for c in found[1:])
        if problems and not found:
            find += f"  [{'; '.join(problems)}]"
        print(f"{mark} {app.name:<26.26} {app.state.text:<30.30} {find}")
    print("\n* would be applied by `iconfetch apply`   ✓ changed by IconFetch")
    return 0


def cmd_apply(args) -> int:
    from .install import apply, suggested

    apps, results = _scan(args.ids)
    done = 0
    for app, (found, problems) in zip(apps, results):
        best = found[0] if found else None
        if best is None:
            if args.ids:
                print(f"{app.name}: {'; '.join(problems) or 'no icon found'}", file=sys.stderr)
            continue
        if args.ids or suggested(app, best):
            apply(app, best)
            done += 1
            print(f"{app.name}: {best.text} ({best.origin})")
    print(f"Applied {done} icon{'s' * (done != 1)}.")
    return 0


def cmd_revert(args) -> int:
    from .apps import installed_apps
    from .install import revert

    wanted = {i if i.endswith(".desktop") else i + ".desktop" for i in args.ids}
    done = 0
    for app in installed_apps():
        if app.applied and (not wanted or app.id in wanted):
            revert(app)
            done += 1
            print(f"{app.name}: original icon back")
    print(f"Reverted {done} app{'s' * (done != 1)}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import __version__

    parser = argparse.ArgumentParser(prog="iconfetch", description="Give installed apps their real icons.")
    parser.add_argument("--version", action="version", version=f"IconFetch {__version__}")
    commands = parser.add_subparsers(dest="command")
    for name, handler, text in (
        ("list", cmd_list, "show apps and the icons found for them"),
        ("apply", cmd_apply, "apply suggested icons, or the best find for the given app IDs"),
        ("revert", cmd_revert, "put original icons back"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("ids", nargs="*", metavar="ID", help="desktop ID, e.g. firefox_firefox")
        sub.set_defaults(handler=handler)
    args = parser.parse_args(argv)
    if args.command:
        return args.handler(args)
    from .ui import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
