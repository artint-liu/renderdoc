"""Standalone RenderDoc MCP broker.

Run with a normal system python BEFORE starting qrenderdoc:

    python rdmcp_broker.py [--port 30007] [--reg-port 30008]

The MCP endpoint is then http://127.0.0.1:<port>/mcp. RenderDoc instances
(the renderdoc_mcp extension in every qrenderdoc) connect to the broker
automatically, and/or call the start_renderdoc tool to spawn qrenderdoc.exe
itself (pass exe=..., or set RENDERDOC_MCP_RD_EXE).

If a RenderDoc instance already hosts a broker, starting a standalone one
fails on purpose (one broker per machine).
"""
import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.join(_HERE, "renderdoc_mcp")
for _p in (_PKG, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broker  # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description="Standalone RenderDoc MCP broker")
    ap.add_argument("--port", type=int, default=int(
        os.environ.get("RENDERDOC_MCP_PORT", "30007")),
        help="HTTP port of the MCP endpoint (default 30007)")
    ap.add_argument("--reg-port", type=int, default=None,
                    help="instance registration port (default: port+1)")
    ns = ap.parse_args()
    reg_port = ns.reg_port if ns.reg_port is not None else ns.port + 1

    def log(msg):
        print(time.strftime("[%H:%M:%S] ") + msg, flush=True)

    b = broker.Broker(ns.port, reg_port, log_cb=log)
    try:
        b.start()
    except broker.BrokerError as e:
        print("ERROR: %s" % e, file=sys.stderr, flush=True)
        return 1
    except OSError as e:
        print("ERROR: %s" % e, file=sys.stderr, flush=True)
        return 1
    log("MCP endpoint : %s" % b.url)
    log("waiting for RenderDoc instances to register (they connect "
        "automatically), or call start_renderdoc via MCP")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("shutting down")
        b.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
