"""IconFetch.

  iconfetch                 open the window
  iconfetch list            show every app, its icon and what IconFetch found (--fresh: look online again)
  iconfetch apply [ID…]     apply the suggested icons (or the best find for each ID)
  iconfetch revert [ID…]    put back the original icons (all of IconFetch's changes without IDs)
  iconfetch refresh         catch up with app updates and removals (runs by itself after installs)
  iconfetch shape           give every app's icon one shape: an icon pack Ada's look wears (--off: as before)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


def _scan(ids: list[str], fresh: bool = False):
    from .apps import installed_apps
    from .sources import Finder

    apps = installed_apps()
    if ids:
        wanted = {i if i.endswith(".desktop") else i + ".desktop" for i in ids}
        apps = [a for a in apps if a.id in wanted]
        missing = wanted - {a.id for a in apps}
        for desktop_id in sorted(missing):
            print(f"{desktop_id}: no such app", file=sys.stderr)
    with Finder(fresh=fresh) as finder, ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(finder.candidates, apps))
    return apps, results


def _reshape() -> None:
    """Bring the same-shape pack, if there is one, up to date with the icons apps show now."""
    from .shapes import refresh

    report = refresh()
    if report is not None and report.made:
        print(f"Same shape: {len(report.made)} icon{'s' * (len(report.made) != 1)} made again")


def cmd_list(args) -> int:
    from .install import suggested

    apps, results = _scan(args.ids, args.fresh)
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

    apps, results = _scan(args.ids, args.fresh)
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
    if done:
        _reshape()
    return 0


def cmd_refresh(args) -> int:
    from .install import SYNC_TEXT, sync

    for name, kind in sync():
        print(f"{name}: {SYNC_TEXT[kind]}")
    _reshape()
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
    if done:
        _reshape()
    return 0


def cmd_shape(args) -> int:
    from . import shapes
    from .apps import on_ada

    if args.off:
        shapes.take_off()
        print("Ada's look shows the icons it wore before.")
        return 0
    report = shapes.build(folder=args.out, plain=args.plain)
    for name, why in report.left_out:
        print(f"{name}: left out ({why})", file=sys.stderr)
    shaped = len(report.made) + len(report.kept)
    print(f"{shaped} icon{'s' * (shaped != 1)} in one shape, in {report.folder}")
    if args.out or args.no_wear or not on_ada():
        return 0
    try:
        shapes.wear()
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print("Ada's look wears them now. `iconfetch shape --off` puts back the icons it wore before.")
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
        ("refresh", cmd_refresh, "catch up with app updates and removals"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("ids", nargs="*", metavar="ID", help="desktop ID, e.g. firefox_firefox")
        if name in ("list", "apply"):
            sub.add_argument("--fresh", action="store_true", help="look online again instead of using finds from the last week")
        sub.set_defaults(handler=handler)
    shape = commands.add_parser("shape", help="give every app's icon one shape (an Ada icon pack)")
    shape.add_argument("--off", action="store_true", help="put back the icons Ada's look wore before")
    shape.add_argument("--plain", action="store_true", help="set odd shapes on grey tiles, not tiles in their own colour")
    shape.add_argument("--no-wear", action="store_true", help="make the pack, but leave Ada's look as it is")
    shape.add_argument("--out", type=Path, metavar="DIR", help="write the pack here instead (Ada's look is left alone)")
    shape.set_defaults(handler=cmd_shape)
    args = parser.parse_args(argv)
    if args.command:
        return args.handler(args)
    from .ui import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
