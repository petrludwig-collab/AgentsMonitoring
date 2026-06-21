"""``python -m agentsmon <command>``

  setup       interactive wizard (auto-detect agents, write config, install service)
  status      print live agent + daemon status
  keepalive   one keepalive pass (restart anything dead); --loop to run forever
  dashboard   serve the live status web page
  service     print/install the OS service for boot persistence
  doctor      sanity-check config + tools
  uninstall   stop everything and remove config/state/service
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentsmon", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    from . import __version__
    p.add_argument("-V", "--version", action="version", version=f"agentsmon {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="interactive setup wizard")
    sub.add_parser("status", help="print live status")
    ka = sub.add_parser("keepalive", help="restart anything that died")
    ka.add_argument("--loop", action="store_true", help="run continuously")
    db = sub.add_parser("dashboard", help="serve the status web page")
    db.add_argument("--host"); db.add_argument("--port", type=int)
    sub.add_parser("service", help="print/install the boot service")
    sub.add_parser("doctor", help="diagnose config + tools")
    un = sub.add_parser("uninstall", help="stop and remove everything")
    un.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    if args.command == "status":
        from . import status
        print(status.render())
        return 0
    if args.command == "keepalive":
        from . import keepalive
        return keepalive.run(loop=args.loop)
    if args.command == "dashboard":
        from . import dashboard, config
        cfg = config.load()
        dashboard.serve(args.host or cfg["dashboard"]["host"],
                        args.port or cfg["dashboard"]["port"])
        return 0
    if args.command == "setup":
        from . import wizard
        return wizard.run()
    if args.command == "service":
        from . import service
        return service.main()
    if args.command == "doctor":
        from . import doctor
        return doctor.run()
    if args.command == "uninstall":
        from . import uninstaller
        return uninstaller.run(yes=args.yes)
    p.error("unknown command")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
