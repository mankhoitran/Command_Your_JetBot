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
from jetbot_core.web import serve, _local_ips
from jetbot_core.monitor import serve_monitor, start_monitor_thread


def _announce(label, scheme, host, port):
    """Print bind address plus real URLs. 0.0.0.0 is not openable in a browser."""
    print("%s bound on %s://%s:%s" % (label, scheme, host, port))
    bind_all = str(host) in ("0.0.0.0", "::", "::0", "")
    ips = _local_ips() if bind_all else [host]
    seen = set()
    for ip in ips:
        if ip in seen:
            continue
        seen.add(ip)
        print("  open %s://%s:%s" % (scheme, ip, port))
    if scheme == "https":
        print("  HTTPS is on (self-signed). Accept the browser warning.")
        print("  Plain http:// on this port is reset — use https://")


def _port_free(host, port):
    """Fail fast if another console already owns the web port."""
    import socket
    family = socket.AF_INET6 if ":" in str(host) else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="JetBot cognitive runtime")
    parser.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--monitor-port", type=int, default=None)
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
    if args.monitor_port is not None:
        cfg.setdefault("monitor", {})["port"] = int(args.monitor_port)

    host = cfg["web"]["host"]
    port = int(cfg["web"]["port"])
    mon_cfg = cfg.get("monitor") or {}
    mon_enabled = bool(mon_cfg.get("enabled", True))
    mon_port = int(mon_cfg.get("port", 8090))

    if not _port_free(host, port):
        print("JetBot console already running on %s:%s — refusing second instance "
              "(it would fight over camera/I2C/port)" % (host, port))
        return 2
    if mon_enabled and not _port_free(host, mon_port):
        print("Pipeline monitor port %s:%s is busy — refusing second instance" % (host, mon_port))
        return 2

    runtime = JetBotRuntime(cfg)
    install_signal_handlers(runtime)
    runtime.start()
    server = serve(runtime, host=host, port=port)
    scheme = "https" if cfg.get("web", {}).get("ssl_enabled") else "http"
    _announce("JetBot console", scheme, host, port)
    monitor = None
    if mon_enabled:
        monitor = serve_monitor(
            runtime, host=host, port=mon_port,
            ssl_enabled=bool(mon_cfg.get("ssl_enabled", cfg.get("web", {}).get("ssl_enabled"))),
            ssl_cert=cfg.get("web", {}).get("ssl_cert"),
            ssl_key=cfg.get("web", {}).get("ssl_key"),
        )
        start_monitor_thread(monitor)
        mon_scheme = "https" if mon_cfg.get("ssl_enabled", cfg.get("web", {}).get("ssl_enabled")) else "http"
        _announce("Pipeline monitor", mon_scheme, host, mon_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
        for srv in (server, monitor):
            if srv is None:
                continue
            try:
                srv.shutdown()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
