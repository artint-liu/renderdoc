"""The MCP broker: single HTTP endpoint shared by all RenderDoc instances.

Responsibilities:
  * serve the MCP protocol over HTTP (mcp_server.MCPServer, 127.0.0.1)
  * accept registrations from RenderDoc instances (extension spoke clients)
    on a second local port (length-prefixed JSON frames, wire.py)
  * route every instance-scoped tool call to the requested instance
    (tool argument ``instance`` = id from list_instances, or a PID)
  * broker-local tools: list_instances, start_renderdoc

The broker may live in its own process (rdmcp_broker.py, started BEFORE any
RenderDoc) or inside the first RenderDoc instance (see spoke.py promotion).
A Win32 named mutex guarantees exactly one broker per machine.
"""
import ctypes
import json
import os
import threading
import time

try:
    from . import mcp_server, tool_defs, wire, winprocess
except ImportError:  # loaded outside the package context
    import mcp_server
    import tool_defs
    import wire
    import winprocess


class BrokerError(Exception):
    """Agent-readable broker-side failure (becomes a tool isError)."""


# machine-wide singleton: ONE broker per machine regardless of ports, so a
# second broker on a custom port cannot silently coexist with the first
_MUTEX_NAME = "Local\\RenderDocMCP_Broker"


def acquire_mutex(_http_port=None):
    """Return an opaque handle, or None if another broker already runs."""
    if os.name != "nt":
        return object()  # non-windows: rely on the port binding alone
    try:
        import ctypes as _ct
        k32 = _ct.WinDLL("kernel32", use_last_error=True)
        handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return None
        if _ct.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return object()


def release_mutex(handle):
    if handle is None or handle is object() or os.name != "nt":
        return
    try:
        import ctypes as _ct
        _ct.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# spokes: uniform interface over a local (in-process) RenderDoc instance and
# remote ones connected through the registration port
# ---------------------------------------------------------------------------

class LocalSpoke(object):
    """A RenderDoc instance hosted in the broker's own process."""

    def __init__(self, iid, meta, dispatch, status_fn):
        self.id = iid
        self.meta = dict(meta or {})
        self.remote = False
        self._dispatch = dispatch
        self._status_fn = status_fn
        self.started_at = time.time()
        self.last_used = None

    def call(self, tool, args, timeout=0):
        content, is_err = self._dispatch(tool, args)
        return {"content": content, "isError": is_err}

    def status(self, timeout=0):
        try:
            return self._status_fn()
        except Exception as e:
            return {"reachable": False, "error": str(e)}

    def describe(self):
        return dict(self.meta)


class RemoteSpoke(object):
    """A RenderDoc instance connected over the registration port."""

    def __init__(self, iid, sock, meta, unregister_cb):
        self.id = iid
        self.meta = dict(meta or {})
        self.remote = True
        self._sock = sock
        self._unregister = unregister_cb
        self._lock = threading.Lock()
        self._reqid = 0
        self._dead = False
        self.started_at = time.time()
        self.last_used = None

    def _request(self, payload, timeout):
        if self._dead:
            raise BrokerError("instance %s is disconnected" % self.id)
        if not self._lock.acquire(True, timeout):
            raise BrokerError(
                "instance %s is busy with a previous request (waited %.0fs)"
                % (self.id, timeout))
        try:
            if self._dead:
                raise BrokerError("instance %s is disconnected" % self.id)
            self._reqid += 1
            rid = self._reqid
            payload = dict(payload)
            payload["id"] = rid
            wire.send_frame(self._sock, payload)
            if timeout > 0:
                wire.set_recv_timeout_ms(self._sock, int(timeout * 1000))
            reply = wire.recv_frame(self._sock)
            if reply is None:
                raise BrokerError("instance %s did not answer within %.0fs "
                                  "(the request may still be running)"
                                  % (self.id, timeout))
            if reply.get("type") != "result" or reply.get("id") != rid:
                raise BrokerError("instance %s protocol desync" % self.id)
            return reply
        except BrokerError:
            self._kill("request failed")
            raise
        except Exception as e:
            self._kill("request failed")
            raise BrokerError("instance %s: %s" % (self.id, e))
        finally:
            self._lock.release()

    def call(self, tool, args, timeout):
        return self._request({"type": "call", "tool": tool, "args": args},
                             timeout)

    def status(self, timeout=5.0):
        try:
            reply = self._request({"type": "status"}, timeout)
            content = reply.get("content")
            if isinstance(content, list) and content \
                    and isinstance(content[0], dict):
                out = dict(content[0])
            else:
                out = {"status": content}
            out["reachable"] = not reply.get("isError", False)
            return out
        except Exception as e:
            return {"reachable": False, "error": str(e)}

    def describe(self):
        return dict(self.meta)

    def _kill(self, why):
        if self._dead:
            return
        self._dead = True
        try:
            wire.close(self._sock)
        except Exception:
            pass
        self._sock = None
        try:
            self._unregister(self, why)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# broker
# ---------------------------------------------------------------------------

class Broker(object):
    def __init__(self, http_port=30007, reg_port=30008, log_cb=None,
                 name="renderdoc-mcp", version="0.3.0", rd_exe=None,
                 rd_exe_source=None, on_quit=None):
        self._http_port = http_port
        self._reg_port = reg_port
        self._log = log_cb if log_cb is not None else (lambda m: None)
        self._name = name
        self._version = version
        # called after the broker stopped because a "quit" frame arrived on
        # the registration port (rdmcp_broker.py --quit); None in RD-hosted
        # mode (the hosting instance just stops serving)
        self._on_quit = on_quit
        # qrenderdoc.exe to launch, fixed at startup: --exe arg > env var
        # (see rdmcp_broker.py); the RD-hosted broker falls back to the
        # hosting qrenderdoc when neither is set (try_host_broker)
        self._rd_exe = rd_exe
        self._rd_exe_source = rd_exe_source
        self._spokes = {}          # id -> LocalSpoke | RemoteSpoke
        self._lock = threading.RLock()
        self._next_id = 1
        self._http = None
        self._reg_listen = None
        self._reg_thread = None
        self._stop = False
        self._mutex = None
        self._call_timeout = _env_float("RENDERDOC_MCP_CALL_TIMEOUT", 300.0)
        self._status_timeout = _env_float("RENDERDOC_MCP_STATUS_TIMEOUT", 8.0)

    @property
    def url(self):
        return "http://127.0.0.1:%d%s" % (self._http_port, mcp_server.MCP_PATH)

    @property
    def reg_port(self):
        return self._reg_port

    @property
    def http_port(self):
        return self._http_port

    def local_instance_id(self):
        with self._lock:
            for iid, s in self._spokes.items():
                if not s.remote:
                    return iid
        return None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if self._http is not None:
            return
        self._stop = False
        self._mutex = acquire_mutex(self._http_port)
        if self._mutex is None:
            raise BrokerError("another RenderDoc MCP broker is already "
                              "running on this machine (port %d)"
                              % self._http_port)
        registry = {}
        for name, d in tool_defs.TOOL_DEFS.items():
            entry = {"description": d["description"], "schema": d["schema"]}
            if name == "list_instances":
                entry["call"] = self._tool_list_instances
            elif name == "start_renderdoc":
                entry["call"] = self._tool_start_renderdoc
            elif name == "shutdown_instance":
                entry["call"] = self._tool_shutdown_instance
            else:
                entry["call"] = (lambda n:
                                 lambda args: self._route(n, args))(name)
            registry[name] = entry
        try:
            self._http = mcp_server.MCPServer(
                registry, log_cb=self._log, port=self._http_port,
                name=self._name, version=self._version)
            self._http.start()
            self._reg_listen = wire.listen("127.0.0.1", self._reg_port)
        except Exception:
            self._shutdown_ports()
            raise
        self._reg_thread = threading.Thread(target=self._reg_loop, daemon=True)
        self._reg_thread.start()
        self._log("[broker] listening: MCP %s | instances 127.0.0.1:%d"
                  % (self.url, self._reg_port))

    def stop(self):
        self._stop = True
        self._shutdown_ports()
        with self._lock:
            spokes = list(self._spokes.values())
            self._spokes.clear()
        for s in spokes:
            if s.remote:
                s._kill("broker stopped")
        if self._mutex is not None:
            release_mutex(self._mutex)
            self._mutex = None
        self._log("[broker] stopped")

    def _shutdown_ports(self):
        if self._http is not None:
            try:
                self._http.stop()
            except Exception:
                pass
            self._http = None
        if self._reg_listen is not None:
            try:
                wire.close(self._reg_listen)
            except Exception:
                pass
            self._reg_listen = None

    # -- registration ------------------------------------------------------

    def register_local(self, meta, dispatch, status_fn):
        """Register the RenderDoc instance living in this process."""
        with self._lock:
            iid = "rd%d" % self._next_id
            self._next_id += 1
            spoke = LocalSpoke(iid, meta, dispatch, status_fn)
            self._spokes[iid] = spoke
        self._log("[broker] local instance %s registered (pid %s)"
                  % (iid, meta.get("pid")))
        return spoke

    def _unregister(self, spoke, why=""):
        with self._lock:
            if self._spokes.get(spoke.id) is spoke:
                del self._spokes[spoke.id]
                self._log("[broker] instance %s disconnected%s"
                          % (spoke.id, " (%s)" % why if why else ""))

    def _reg_loop(self):
        w = mcp_server._ws()
        while not self._stop:
            addr = wire._sockaddr_in()
            alen = ctypes.c_int(ctypes.sizeof(addr))
            client = w.accept(self._reg_listen, ctypes.byref(addr),
                              ctypes.byref(alen))
            if client == -1:
                err = w.WSAGetLastError()
                if err == wire.WSAEWOULDBLOCK:
                    time.sleep(0.05)
                    continue
                if not self._stop:
                    self._log("[broker] accept failed: WSA %d" % err)
                break
            # accepted sockets inherit the listener's non-blocking mode on
            # Windows - switch to blocking + a receive timeout HERE (same
            # thread as accept) before handing the socket to its handler
            wire.set_blocking(client, True)
            wire.set_recv_timeout_ms(client, 10000)
            t = threading.Thread(target=self._handshake, args=(client,),
                                 daemon=True)
            t.start()

    def _handshake(self, sock):
        try:
            frame = wire.recv_frame(sock)
            if not frame:
                wire.close(sock)
                return
            if frame.get("type") == "quit":
                # graceful remote shutdown (rdmcp_broker.py --quit): ack
                # first, then take the endpoint down in the background so
                # this reply reliably gets out
                self._log("[broker] quit requested via registration port")
                try:
                    wire.send_frame(sock, {"type": "bye"})
                except Exception:
                    pass
                wire.close(sock)
                self._shutdown_from_quit()
                return
            if frame.get("type") != "register":
                wire.close(sock)
                return
            meta = dict((k, frame.get(k))
                        for k in ("pid", "exe", "version", "hostname"))
            with self._lock:
                iid = "rd%d" % self._next_id
                self._next_id += 1
                try:
                    wire.send_frame(sock, {
                        "type": "welcome", "instance": iid,
                        "endpoint": self.url,
                        "broker_pid": os.getpid()})
                except Exception:
                    wire.close(sock)
                    return
                self._spokes[iid] = RemoteSpoke(iid, sock, meta,
                                                self._unregister)
            self._log("[broker] instance %s registered (pid %s, exe %s)"
                      % (iid, meta.get("pid"), meta.get("exe")))
        except Exception as e:
            self._log("[broker] registration handshake failed: %s" % e)
            wire.close(sock)

    def _shutdown_from_quit(self):
        def _finish():
            time.sleep(0.2)  # let the quit ack flush before ports go down
            try:
                self.stop()
            except Exception:
                pass
            cb = self._on_quit
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass
        threading.Thread(target=_finish, daemon=True).start()

    # -- routing -----------------------------------------------------------

    def _snapshot(self):
        with self._lock:
            return list(self._spokes.values())

    def _resolve(self, inst):
        spokes = self._snapshot()
        ids = ", ".join(s.id for s in spokes) or "<none>"
        if not spokes:
            raise BrokerError(
                "No RenderDoc instance is connected to this MCP broker. "
                "Start qrenderdoc (the renderdoc_mcp extension connects "
                "automatically) or call start_renderdoc.")
        if inst is not None and str(inst).strip() != "":
            key = str(inst).strip()
            for s in spokes:
                if s.id.lower() == key.lower():
                    return s
            if key.isdigit():  # accept a raw PID as well
                for s in spokes:
                    try:
                        if int(s.meta.get("pid", -1)) == int(key):
                            return s
                    except (TypeError, ValueError):
                        pass
            raise BrokerError("Unknown RenderDoc instance %r. Connected: %s. "
                              "Use list_instances to get valid ids."
                              % (inst, ids))
        if len(spokes) == 1:
            return spokes[0]
        raise BrokerError(
            "Multiple RenderDoc instances are connected (%s). Pass the "
            "'instance' argument to choose one (ids from list_instances)."
            % ids)

    def _route(self, name, args):
        args = dict(args or {})
        inst = args.pop("instance", None)
        spoke = self._resolve(inst)
        spoke.last_used = time.time()
        reply = spoke.call(name, args, self._call_timeout)
        content = reply.get("content")
        if not content:
            content = [{"type": "text", "text": "empty response"}]
        # normalize to proper MCP content items (protects against a
        # misbehaving instance returning raw dicts)
        norm = []
        for item in content:
            if isinstance(item, dict) and \
                    item.get("type") in ("text", "image", "resource"):
                norm.append(item)
            else:
                norm.append({"type": "text",
                             "text": json.dumps(item, ensure_ascii=False,
                                                default=str)})
        return {"content": norm,
                "isError": bool(reply.get("isError", False))}

    # -- broker-local tools --------------------------------------------------

    def _tool_list_instances(self, args):
        spokes = self._snapshot()
        results = [None] * len(spokes)
        threads = []
        for i, s in enumerate(spokes):
            if s.remote:
                def _probe(i=i, sp=s):
                    results[i] = sp.status(timeout=self._status_timeout)
                t = threading.Thread(target=_probe, daemon=True)
                t.start()
                threads.append(t)
            else:
                results[i] = s.status()
        for t in threads:
            t.join(self._status_timeout + 2.0)
        # spokes that died while probing were already unregistered - don't
        # report them as present
        alive = set(id(s) for s in self._snapshot())
        instances = []
        for i, s in enumerate(spokes):
            if id(s) not in alive:
                continue
            st = results[i]
            if st is None:
                st = {"reachable": False,
                      "error": "no answer within %.0fs"
                               % self._status_timeout}
            out = {"instance": s.id, "remote": s.remote}
            out.update(s.describe())
            out["started_at"] = _iso_time(s.started_at)
            out["last_used"] = _iso_time(s.last_used) \
                if s.last_used is not None else None
            # has_capture_connection: whether a launched program is still
            # connected to that instance (from the spoke status if reported)
            out["has_capture_connection"] = st.get("app_connected")
            out.update(st)
            instances.append(out)
        out = {"endpoint": self.url,
               "broker_pid": os.getpid(),
               "rd_exe": self._rd_exe,
               "rd_exe_source": self._rd_exe_source,
               "instances": instances}
        return tool_defs.text_result(out)

    def _tool_shutdown_instance(self, args):
        args = dict(args or {})
        inst = args.get("instance")
        spoke = self._resolve(inst)
        try:
            pid = int(spoke.meta.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        self._log("[broker] shutdown_instance %s (pid %s)" % (spoke.id, pid))
        graceful_note = ""
        try:
            reply = spoke.call("shutdown_instance", {},
                               min(self._call_timeout, 15.0))
            if reply.get("isError"):
                content = reply.get("content") or []
                if content and isinstance(content[0], dict):
                    graceful_note = str(content[0].get("text", ""))[:200]
        except Exception as e:
            graceful_note = str(e)
        if spoke.remote:
            # a different process: wait for it to exit, kill if needed
            method = "graceful"
            if not _wait_exit(pid, 5.0):
                method = "kill"
                try:
                    winprocess.terminate(pid)
                except Exception as e:
                    graceful_note += " terminate failed: %s" % e
                if not _wait_exit(pid, 3.0):
                    method = "kill (unverified)"
                    graceful_note += (" process did not confirm exit; "
                                      "removed from instance table anyway")
            self._drop_spoke(spoke)
        else:
            # our own process: respond first, then finish shutting down in
            # the background so the HTTP reply gets out
            method = "graceful"
            graceful_note += (" this broker is hosted by the instance being "
                              "shut down; the process exits after this "
                              "response (force-killed if graceful close "
                              "stalls)")
            t = threading.Thread(target=self._finish_own_shutdown,
                                 args=(pid,), daemon=True)
            t.start()
            self._drop_spoke(spoke)
        out = {"shutdown": True, "method": method, "pid": pid,
               "instance": spoke.id}
        if graceful_note:
            out["note"] = graceful_note.strip()
        return tool_defs.text_result(out)

    def _drop_spoke(self, spoke):
        with self._lock:
            if self._spokes.get(spoke.id) is spoke:
                del self._spokes[spoke.id]
        if spoke.remote:
            try:
                spoke._kill("shutdown")
            except Exception:
                pass
        self._log("[broker] instance %s removed from instance table"
                  % spoke.id)

    def _finish_own_shutdown(self, pid):
        time.sleep(1.0)
        if not _wait_exit(pid, 5.0):
            try:
                winprocess.terminate(pid)
            except Exception:
                pass

    def _tool_start_renderdoc(self, args):
        if not self._rd_exe:
            raise BrokerError(
                "No qrenderdoc exe configured. Restart the broker with "
                "--exe <path to qrenderdoc.exe> or set the "
                "RENDERDOC_MCP_RD_EXE environment variable.")
        exe = self._rd_exe
        workdir = args.get("working_dir") or None
        extra = args.get("args") or ""
        wait_seconds = _env_float2(args.get("wait_seconds"), 45.0)
        self._log("[broker] start_renderdoc: %s %s" % (exe, extra))
        # let the child's extension find THIS broker even on custom ports
        child_env = {"RENDERDOC_MCP_PORT": str(self._http_port),
                     "RENDERDOC_MCP_REG_PORT": str(self._reg_port)}
        pid = winprocess.spawn(exe, extra, workdir, extra_env=child_env)
        out = {"started": True, "pid": pid, "exe": exe,
               "exe_source": self._rd_exe_source, "endpoint": self.url}
        if wait_seconds > 0:
            deadline = time.time() + wait_seconds
            found = None
            while time.time() < deadline and found is None:
                time.sleep(0.5)
                for s in self._snapshot():
                    try:
                        if int(s.meta.get("pid", -1)) == pid:
                            found = s.id
                            break
                    except (TypeError, ValueError):
                        pass
            out["registered_instance"] = found
            if found is None:
                out["note"] = ("qrenderdoc did not register within %.0fs; "
                               "it may still be starting - check "
                               "list_instances" % wait_seconds)
        return tool_defs.text_result(out)


def _iso_time(ts):
    """Unix timestamp -> local ISO 'YYYY-MM-DDTHH:MM:SS' (None-safe)."""
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    except Exception:
        return None


def _wait_exit(pid, timeout):
    """True when the process is gone (or pid unknown) within timeout."""
    if not pid:
        return True
    deadline = time.time() + timeout
    while True:
        try:
            if not winprocess.alive(pid):
                return True
        except Exception:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _env_float(var, default):
    try:
        return float(os.environ.get(var, default))
    except (TypeError, ValueError):
        return default


def _env_float2(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def try_host_broker(http_port, reg_port, log_cb, host_meta=None,
                    host_dispatch=None, host_status=None):
    """Try to become the system's MCP broker. Returns (broker, None) on
    success or (None, reason) when another broker exists / ports are busy."""
    # RD-hosted broker has no CLI arg: RENDERDOC_MCP_RD_EXE env, else the
    # hosting qrenderdoc.exe itself (same build => has the extension)
    rd_exe = os.environ.get("RENDERDOC_MCP_RD_EXE")
    rd_exe_source = None
    if rd_exe:
        rd_exe_source = "env"
    else:
        rd_exe = (host_meta or {}).get("exe")
        if rd_exe:
            rd_exe_source = "host"
    b = Broker(http_port, reg_port, log_cb=log_cb, rd_exe=rd_exe,
               rd_exe_source=rd_exe_source)
    try:
        b.start()
    except BrokerError as e:
        return None, str(e)
    except OSError as e:
        return None, "failed to bind ports %d/%d: %s" % (http_port, reg_port, e)
    except Exception as e:
        return None, "broker start failed: %s" % e
    if host_dispatch is not None:
        b.register_local(host_meta, host_dispatch, host_status)
    return b, None
