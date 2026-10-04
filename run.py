#!/usr/bin/env python3
"""Start the JetBot cognitive runtime + web console."""

from __future__ import print_function

import argparse
import logging
import os
import sys

if sys.version_info[0] < 3:
    # On this Nano, `python` is 2.7. Re-exec with python3.
    os.execv("/usr/bin/python3", ["/usr/bin/python3", os.path.abspath(__file__)] + sys.argv[1:])

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from jetbot_core.config import load_config
from jetbot_core.runtime import JetBotRuntime, install_signal_handlers
from jetbot_core.web import serve


def main(argv=None):
    parser = argparse.ArgumentParser(description="JetBot cognitive runtime")
    parser.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    if args.host:
        cfg["web"]["host"] = args.host
    if args.port:
        cfg["web"]["port"] = int(args.port)

    runtime = JetBotRuntime(cfg)
    install_signal_handlers(runtime)
    runtime.start()
    host = cfg["web"]["host"]
    port = int(cfg["web"]["port"])
    server = serve(runtime, host=host, port=port)
    print("JetBot console: http://%s:%s" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
        try:
            server.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
