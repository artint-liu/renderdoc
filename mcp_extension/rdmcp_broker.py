"""Standalone RenderDoc MCP broker.

Run with a normal system python BEFORE starting qrenderdoc:

    python rdmcp_broker.py --exe <path to qrenderdoc.exe>
    python rdmcp_broker.py --port 30007 --reg-port 30008 --exe ...

The qrenderdoc.exe that `start_renderdoc` will launch is fixed at broker
startup: --exe argument first, then the RENDERDOC_MCP_RD_EXE environment
variable. If neither is set the broker REFUSES to start. The chosen path is
printed at startup and reported by list_instances.

RenderDoc instances (the renderdoc_mcp extension in every qrenderdoc built
from this workspace) connect to the broker automatically.

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
    ap.add_argument("--exe", dest="rd_exe", default=None, metavar="PATH",
                    help="Path to qrenderdoc.exe that start_renderdoc will "
                         "launch (default: RENDERDOC_MCP_RD_EXE env var; "
                         "the broker refuses to start without one)")
    ap.add_argument("--port", type=int, default=int(
        os.environ.get("RENDERDOC_MCP_PORT", "30007")),
        help="HTTP port of the MCP endpoint (default 30007)")
    ap.add_argument("--reg-port", type=int, default=None,
                    help="instance registration port (default: port+1)")
    ns = ap.parse_args()
    reg_port = ns.reg_port if ns.reg_port is not None else ns.port + 1

    def log(msg):
        print(time.strftime("[%H:%M:%S] ") + msg, flush=True)

    # qrenderdoc exe: --exe arg > env var; refuse to start without one
    rd_exe = ns.rd_exe or os.environ.get("RENDERDOC_MCP_RD_EXE")
    rd_exe_source = "startup-param" if ns.rd_exe else (
        "env" if os.environ.get("RENDERDOC_MCP_RD_EXE") else None)
    if not rd_exe:
        print("ERROR: no qrenderdoc.exe configured. Pass --exe <path> or "
              "set RENDERDOC_MCP_RD_EXE, e.g.\n"
              "  rdmcp_broker.py --exe "
              "D:\\MyCodes\\renderdoc\\x64\\Development\\qrenderdoc.exe",
              file=sys.stderr, flush=True)
        return 2
    if not os.path.isfile(rd_exe):
        print("ERROR: qrenderdoc.exe not found: %s" % rd_exe,
              file=sys.stderr, flush=True)
        return 2
    rd_exe = os.path.abspath(rd_exe)

    b = broker.Broker(ns.port, reg_port, log_cb=log, rd_exe=rd_exe,
                      rd_exe_source=rd_exe_source)
    try:
        b.start()
    except broker.BrokerError as e:
        print("ERROR: %s" % e, file=sys.stderr, flush=True)
        return 1
    except OSError as e:
        print("ERROR: %s" % e, file=sys.stderr, flush=True)
        return 1
    log("MCP endpoint  : %s" % b.url)
    log("qrenderdoc exe: %s (%s)" % (rd_exe, rd_exe_source))
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
