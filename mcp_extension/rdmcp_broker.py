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

Stop an already-running broker gracefully:

    python rdmcp_broker.py --quit

--quit checks whether a broker is running (machine-wide mutex + the
registration port). With none running it just reports that and exits; with
one running it sends a quit request over the registration port, waits for
the ack and exits once the endpoint is down. Exit code 0 = nothing to do /
broker stopped, 1 = broker running but could not be stopped.
"""
import argparse
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.join(_HERE, "renderdoc_mcp")
for _p in (_PKG, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broker  # noqa: E402
import wire  # noqa: E402


def _quit_running_broker(reg_port, http_port, wait_seconds=10.0):
    """Ask the running broker (if any) to exit. Returns (was_running,
    stopped, note)."""
    try:
        sock = wire.connect("127.0.0.1", reg_port)
    except Exception:
        sock = None
    if sock is None:
        # nothing answers the registration port: the machine-wide mutex is
        # the authoritative "is a broker running" check
        mutex = broker.acquire_mutex(http_port)
        if mutex is None:
            return True, False, (
                "a broker holds the singleton mutex but its registration "
                "port %d is unreachable - it may run on a custom port; "
                "retry with a matching --port" % reg_port)
        broker.release_mutex(mutex)
        return False, False, "no RenderDoc MCP broker is running"
    try:
        try:
            wire.set_recv_timeout_ms(sock, int(wait_seconds * 1000))
        except Exception:
            pass
        try:
            wire.send_frame(sock, {"type": "quit"})
        except Exception as e:
            return True, False, "failed to send the quit request: %s" % e
        try:
            reply = wire.recv_frame(sock)
        except wire.ConnectionClosed:
            reply = None
        if reply is None or reply.get("type") != "bye":
            return True, False, ("the running broker did not acknowledge "
                                 "the quit request (older version? stop it "
                                 "manually, e.g. Stop-Process -Id <pid>)")
        # acked: wait until the HTTP endpoint actually goes down
        deadline = time.time() + 5.0
        down = False
        while time.time() < deadline:
            try:
                probe = wire.connect("127.0.0.1", http_port)
            except Exception:
                down = True
                break
            wire.close(probe)
            time.sleep(0.25)
        note = "broker acknowledged quit and shut down"
        if not down:
            note += " (endpoint %d still answering after 5s)" % http_port
        return True, True, note
    finally:
        wire.close(sock)


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
    ap.add_argument("--quit", action="store_true",
                    help="ask an already-running broker to exit, then exit; "
                         "with none running just report that (exit 0)")
    ns = ap.parse_args()
    reg_port = ns.reg_port if ns.reg_port is not None else ns.port + 1

    if ns.quit:
        was_running, stopped, note = _quit_running_broker(reg_port, ns.port)
        print("[quit] %s" % note, flush=True)
        return 0 if (stopped or not was_running) else 1

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

    quit_evt = threading.Event()

    def _on_quit():
        log("broker stopped by a --quit request")
        quit_evt.set()

    b = broker.Broker(ns.port, reg_port, log_cb=log, rd_exe=rd_exe,
                      rd_exe_source=rd_exe_source, on_quit=_on_quit)
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
        while not quit_evt.wait(1.0):
            pass
        return 0
    except KeyboardInterrupt:
        log("shutting down")
        b.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
