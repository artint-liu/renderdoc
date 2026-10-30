"""RenderDoc-instance side of the MCP broker architecture (spoke client).

Every qrenderdoc running the renderdoc_mcp extension starts one SpokeClient.
It connects to the local broker's registration port, registers itself (and
gets an instance id like 'rd1'), then serves routed tool calls. If no broker
is running it first tries to BECOME the broker - the instance then hosts the
HTTP MCP endpoint itself and registers as its own first instance.

Reconnect loop: if the broker disappears (its RenderDoc was closed, or the
standalone broker exited), the client retries and, when no broker answers,
promotes itself - so an agent never loses the endpoint while any instance
with this extension is alive.
"""
import threading
import time
import traceback

try:
    from . import broker as broker_mod
    from . import tool_defs
    from . import wire
except ImportError:  # loaded outside the package context
    import broker as broker_mod
    import tool_defs
    import wire


class SpokeClient(object):
    def __init__(self, reg_host, reg_port, http_port, dispatch, status_fn,
                 meta, log_cb=None, on_instance=None, on_hosted=None,
                 on_stopped=None):
        """
        dispatch(tool, args) -> (content_list, is_error)
        status_fn()          -> dict (reported for list_instances)
        meta                 -> {"pid","exe","version","hostname"}
        on_instance(iid, welcome)   called after successful registration
        on_hosted(broker_obj)       called when this process became broker
        """
        self._reg_host = reg_host
        self._reg_port = reg_port
        self._http_port = http_port
        self._dispatch = dispatch
        self._status_fn = status_fn
        self._meta = dict(meta or {})
        self._log = log_cb if log_cb is not None else (lambda m: None)
        self._on_instance = on_instance
        self._on_hosted = on_hosted
        self._on_stopped = on_stopped
        self._stop = False
        self._thread = None
        self._sock = None
        self._broker = None
        self._instance_id = None
        self._last_hostfail_log = 0.0

    # -- public ------------------------------------------------------------

    @property
    def instance_id(self):
        return self._instance_id

    @property
    def hosting(self):
        return self._broker is not None

    def start(self):
        if self._thread is not None:
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        b = self._broker
        if b is not None:
            self._broker = None
            try:
                b.stop()
            except Exception:
                pass
        sock = self._sock
        self._sock = None
        if sock is not None:
            wire.close(sock)
        if self._on_stopped is not None:
            try:
                self._on_stopped()
            except Exception:
                pass

    # -- worker ------------------------------------------------------------

    def _run(self):
        first = True
        while not self._stop:
            sock = None
            try:
                sock = wire.connect(self._reg_host, self._reg_port)
            except Exception:
                sock = None
            if sock is None:
                if first:
                    self._log("[spoke] no broker on 127.0.0.1:%d, "
                              "trying to host it" % self._reg_port)
                    first = False
                if self._try_host():
                    return  # hosting: done until stop()
                # keep retrying so a broker that appears later (restart,
                # slow startup) picks this instance up automatically
                now = time.time()
                if now - self._last_hostfail_log >= 30.0:
                    self._last_hostfail_log = now
                    self._log("[spoke] registration retry continues every "
                              "2s (broker will pick this instance up "
                              "automatically once it is up)")
                time.sleep(2.0)
                continue
            first = False
            try:
                self._register_and_serve(sock)
            except Exception as e:
                self._log("[spoke] connection ended: %s" % e)
            finally:
                wire.close(sock)
                if self._sock is sock:
                    self._sock = None
            self._instance_id = None
            if self._stop:
                return
            self._log("[spoke] reconnecting to broker...")
            time.sleep(0.5)

    def _try_host(self):
        b, reason = broker_mod.try_host_broker(
            self._http_port, self._reg_port, self._log,
            host_meta=self._meta, host_dispatch=self._dispatch,
            host_status=self._status_fn)
        if b is None:
            now = time.time()
            if reason and now - self._last_hostfail_log >= 30.0:
                self._last_hostfail_log = now
                self._log("[spoke] cannot host broker: %s" % reason)
            return False
        self._broker = b
        iid = b.local_instance_id()
        self._instance_id = iid
        self._log("[spoke] this RenderDoc now hosts the MCP broker as "
                  "instance %s at %s" % (iid, b.url))
        if self._on_hosted is not None:
            try:
                self._on_hosted(b)
            except Exception:
                traceback.print_exc()
        return True

    def _register_and_serve(self, sock):
        wire.send_frame(sock, dict([("type", "register")] +
                                   list(self._meta.items())))
        wire.set_recv_timeout_ms(sock, 10000)
        welcome = wire.recv_frame(sock)
        if not welcome or welcome.get("type") != "welcome":
            raise ConnectionError("bad registration reply: %r" % (welcome,))
        self._sock = sock
        self._instance_id = welcome.get("instance")
        self._log("[spoke] registered as instance %s (broker at %s)"
                  % (self._instance_id, welcome.get("endpoint")))
        if self._on_instance is not None:
            try:
                self._on_instance(self._instance_id, welcome)
            except Exception:
                traceback.print_exc()
        # serve loop: a dead broker shows up as a recv error (TCP FIN/RST);
        # idle timeouts just mean "no work right now", so no keepalive needed
        wire.set_recv_timeout_ms(sock, 15000)
        while not self._stop:
            frame = wire.recv_frame(sock)
            if frame is None:
                continue
            ftype = frame.get("type")
            fid = frame.get("id")
            if ftype == "call":
                content, is_err = self._safe_dispatch(
                    frame.get("tool"), frame.get("args") or {})
                self._reply(sock, fid, content, is_err)
            elif ftype == "status":
                try:
                    st = self._status_fn()
                except Exception as e:
                    st = {"reachable": False, "error": str(e)}
                self._reply(sock, fid, tool_defs.text_result(st), False)
            else:
                self._log("[spoke] ignoring frame type %r" % ftype)

    def _reply(self, sock, fid, content, is_err):
        try:
            wire.send_frame(sock, {"type": "result", "id": fid,
                                   "content": content, "isError": bool(is_err)})
        except Exception as e:
            raise ConnectionError("reply failed: %s" % e)

    def _safe_dispatch(self, tool, args):
        """Never let an exception escape without a reply frame, or the
        broker would desync waiting for a result."""
        try:
            content, is_err = self._dispatch(tool, args)
            if content is None:
                content = [{"type": "text", "text": "empty response"}]
            return content, is_err
        except Exception as e:
            tb = traceback.format_exc()
            self._log("[spoke] tool %s failed: %s" % (tool, e))
            return ([{"type": "text",
                      "text": "%s: %s\n%s" % (type(e).__name__, e, tb)}],
                    True)

