"""Minimal MCP (Model Context Protocol) server over Streamable HTTP.

Implements just enough of the protocol for tool usage by an AI agent:
initialize / tools/list / tools/call / ping, plus notification handling.
Runs on 127.0.0.1 only.

Transport note: qrenderdoc's embedded python (3.6) ships without the
``_socket`` C extension, so ``http.server`` / ``socket`` are unusable here.
This module therefore implements a small blocking HTTP/1.1 server directly
over winsock via ctypes. ctypes calls release the GIL, so the RenderDoc UI
and replay threads keep running while we wait on recv/accept.
"""
import ctypes
import json
import threading
import time
import traceback
from ctypes import c_int, c_long, c_uint32, c_ulong, byref, create_string_buffer

MCP_PATH = "/mcp"
PROTOCOL_VERSION = "2024-11-05"

MAX_REQUEST_SIZE = 64 * 1024 * 1024

# winsock constants
AF_INET = 2
SOCK_STREAM = 1
SOCKET_ERROR = -1
FIONBIO = -2147195266  # 0x8004667E as signed long
WSAEWOULDBLOCK = 10035

_ws2_32 = None


def _ws():
    global _ws2_32
    if _ws2_32 is None:
        _ws2_32 = ctypes.WinDLL("ws2_32")
    return _ws2_32


class _sockaddr_in(ctypes.Structure):
    _fields_ = [("sin_family", ctypes.c_ushort),
                ("sin_port", ctypes.c_ushort),
                ("sin_addr", c_uint32),
                ("sin_zero", ctypes.c_uint8 * 8)]


def _htons(v):
    return ((v & 0xFF) << 8) | (v >> 8)


def _addr_u32(host):
    parts = [int(p) for p in host.split(".")]
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        raise ValueError("bad host: %r" % host)
    # network byte order, viewed as little-endian uint32
    return parts[0] | (parts[1] << 8) | (parts[2] << 16) | (parts[3] << 24)


class MCPServer(object):
    def __init__(self, tools, log_cb=None, port=30007, host="127.0.0.1",
                 name="renderdoc-mcp", version="0.1.0"):
        """
        tools: ordered dict of name -> {"description", "schema", "call"}
               where call(args: dict) -> list of MCP content items or str.
        log_cb: callable(str) for request logging.
        """
        self._tools = tools
        self._log = log_cb if log_cb is not None else (lambda m: None)
        self._host = host
        self._port = port
        self._name = name
        self._version = version
        self._listen = None
        self._thread = None
        self._stop = False
        self._started = False

    @property
    def url(self):
        return "http://%s:%d%s" % (self._host, self._port, MCP_PATH)

    # ------------------------------------------------------------------
    # socket helpers
    # ------------------------------------------------------------------

    def _startup(self):
        if self._started:
            return
        wsa = _ws()
        data = create_string_buffer(408)
        if wsa.WSAStartup(0x0202, data) != 0:
            raise OSError("WSAStartup failed: %d" % wsa.WSAGetLastError())
        self._started = True

    def _create_listen(self):
        w = _ws()
        s = w.socket(AF_INET, SOCK_STREAM, 0)
        if s == SOCKET_ERROR:
            raise OSError("socket() failed: %d" % w.WSAGetLastError())
        # reuse addr to make restarts painless
        w.setsockopt(s, 0xFFFF, 4, byref(c_int(1)), 4)  # SOL_SOCKET/SO_REUSEADDR
        addr = _sockaddr_in(AF_INET, _htons(self._port), _addr_u32(self._host))
        if w.bind(s, byref(addr), ctypes.sizeof(addr)) == SOCKET_ERROR:
            err = w.WSAGetLastError()
            w.closesocket(s)
            raise OSError("bind %s:%d failed: WSA error %d"
                          % (self._host, self._port, err))
        if w.listen(s, 16) == SOCKET_ERROR:
            err = w.WSAGetLastError()
            w.closesocket(s)
            raise OSError("listen failed: WSA error %d" % err)
        w.ioctlsocket(s, FIONBIO, byref(c_ulong(1)))
        return s

    def _close(self, s):
        if s is not None and s != SOCKET_ERROR:
            _ws().closesocket(s)

    def _send_all(self, s, data):
        w = _ws()
        view = create_string_buffer(data)
        total = len(data)
        sent = 0
        while sent < total:
            n = w.send(s, ctypes.byref(view, sent), total - sent, 0)
            if n == SOCKET_ERROR:
                err = w.WSAGetLastError()
                if err == WSAEWOULDBLOCK:
                    time.sleep(0.005)
                    continue
                raise OSError("send failed: WSA error %d" % err)
            sent += n

    def _recv_request(self, s):
        """Read one HTTP request. Returns (method, path, headers, body) or None."""
        w = _ws()
        buf = b""
        while True:
            if len(buf) > MAX_REQUEST_SIZE:
                raise OSError("request too large")
            chunk = create_string_buffer(65536)
            n = w.recv(s, chunk, 65536, 0)
            if n == SOCKET_ERROR:
                err = w.WSAGetLastError()
                if err == WSAEWOULDBLOCK:
                    time.sleep(0.005)
                    continue
                return None
            if n == 0:
                return None  # closed before a full request
            buf += chunk.raw[:n]
            if b"\r\n\r\n" not in buf:
                continue
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3:
                return None
            method, path = parts[0], parts[1]
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                chunk = create_string_buffer(65536)
                n = w.recv(s, chunk, 65536, 0)
                if n == SOCKET_ERROR:
                    err = w.WSAGetLastError()
                    if err == WSAEWOULDBLOCK:
                        time.sleep(0.005)
                        continue
                    return None
                if n == 0:
                    return None
                rest += chunk.raw[:n]
            return method, path, headers, rest[:length]

    # ------------------------------------------------------------------
    # server lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Bind and serve. Raises OSError if the port is already in use."""
        if self._listen is not None:
            return
        self._stop = False
        self._startup()
        try:
            self._listen = self._create_listen()
        except OSError:
            self._stop = True
            raise
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._log("[MCP] listening on " + self.url)

    def stop(self):
        if self._listen is None:
            return
        self._log("[MCP] server stopped (" + self.url + ")")
        self._stop = True
        self._close(self._listen)  # unblocks accept
        self._listen = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _serve(self):
        w = _ws()
        try:
            while not self._stop:
                addr = _sockaddr_in()
                alen = c_int(ctypes.sizeof(addr))
                client = w.accept(self._listen, byref(addr), byref(alen))
                if client == SOCKET_ERROR:
                    err = w.WSAGetLastError()
                    if err == WSAEWOULDBLOCK:
                        time.sleep(0.05)
                        continue
                    if not self._stop:
                        self._log("[MCP] accept failed: WSA error %d" % err)
                    break
                try:
                    self._handle_client(client)
                except Exception as e:
                    self._log("[MCP] client handler error: %s" % e)
                finally:
                    self._close(client)
        except Exception as e:
            if not self._stop:
                self._log("[MCP] server thread died: %s" % e)

    def _handle_client(self, s):
        try:
            req = self._recv_request(s)
        except Exception as e:
            self._log("[MCP] recv error: %s" % e)
            return
        if req is None:
            return
        method, path, headers, body = req

        cors = [("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS"),
                ("Access-Control-Allow-Headers",
                 "Content-Type, Mcp-Session-Id, Mcp-Protocol-Version")]

        def respond(code, payload, content_type="application/json"):
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            elif payload is None:
                payload = b""
            reason = {200: "OK", 202: "Accepted", 204: "No Content",
                      400: "Bad Request", 404: "Not Found",
                      405: "Method Not Allowed"}.get(code, "OK")
            head = "HTTP/1.1 %d %s\r\n" % (code, reason)
            if code != 204:
                head += "Content-Type: %s\r\n" % content_type
            head += "Content-Length: %d\r\n" % len(payload)
            for k, v in cors:
                head += "%s: %s\r\n" % (k, v)
            head += "Connection: close\r\n\r\n"
            self._send_all(s, head.encode("latin-1") + payload)

        if method == "OPTIONS":
            respond(204, None)
            return
        if path != MCP_PATH:
            respond(404, '{"error":"not found"}')
            return
        if method != "POST":
            if method == "DELETE":
                respond(200, "{}")
            else:
                # no server-initiated SSE stream; streamable HTTP clients
                # must accept 405 here
                respond(405, '{"error":"POST JSON-RPC messages"}')
            return

        try:
            msg = json.loads(body.decode("utf-8"))
        except Exception as e:
            respond(400, json.dumps({"error": "invalid JSON: %s" % e}))
            return

        try:
            resp = self._handle(msg)
        except Exception as e:
            self._log("handler error: %s" % e)
            resp = {"jsonrpc": "2.0", "id": msg.get("id")
                    if isinstance(msg, dict) else None,
                    "error": {"code": -32603,
                              "message": "Internal error: %s" % e}}
        if resp is None:
            respond(202, None)
        else:
            respond(200, json.dumps(resp, ensure_ascii=False))

    # ------------------------------------------------------------------
    # JSON-RPC / MCP dispatch
    # ------------------------------------------------------------------

    def _handle(self, msg):
        if isinstance(msg, list):
            outs = [r for r in (self._handle_one(m) for m in msg)
                    if r is not None]
            if not outs:
                return None
            return outs[0] if len(outs) == 1 else outs
        return self._handle_one(msg)

    def _handle_one(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Not a JSON-RPC 2.0 message")
        method = msg.get("method", "")
        mid = msg.get("id")
        # notifications get no response
        if "id" not in msg:
            return None

        result = None
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self._name, "version": self._version},
                "instructions": (
                    "This server exposes tools to inspect the capture "
                    "currently open in a running RenderDoc UI instance. "
                    "Start with get_capture_info / list_drawcalls, then use "
                    "get_drawcall_params, get_screenshot, get_pixel_history "
                    "and get_shader_disassembly to analyse specific events."),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [
                {"name": name, "description": tool["description"],
                 "inputSchema": tool["schema"]}
                for name, tool in self._tools.items()]}
        elif method == "tools/call":
            params = msg.get("params") or {}
            result = self._call_tool(str(params.get("name", "")),
                                     params.get("arguments") or {})
        else:
            return self._error(mid, -32601, "Method not found: %s" % method)

        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _error(self, mid, code, message):
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}

    def _call_tool(self, name, args):
        self._log("[MCP] tools/call %s %s" %
                  (name, json.dumps(args, ensure_ascii=False)[:300]))
        tool = self._tools.get(name)
        if tool is None:
            return {"content": [{"type": "text",
                                 "text": "Unknown tool: %s" % name}],
                    "isError": True}
        try:
            content = tool["call"](args)
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                raise ValueError("tool returned invalid content type")
            return {"content": content, "isError": False}
        except Exception as e:
            self._log("[MCP] tool %s failed: %s" % (name, e))
            text = "%s: %s" % (type(e).__name__, e)
            tb = traceback.format_exc()
            return {"content": [{"type": "text", "text": text + "\n" + tb}],
                    "isError": True}
