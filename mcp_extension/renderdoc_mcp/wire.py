"""Length-prefixed JSON framing over winsock for the internal
broker <-> RenderDoc-instance protocol.

qrenderdoc's embedded python (3.6) ships without the ``_socket`` C extension,
so all networking goes through ws2_32 directly via ctypes. All sockets here
are blocking; receive timeouts use SO_RCVTIMEO (Windows wants milliseconds).
Only 127.0.0.1 is ever used.

Frame layout: u32 little-endian payload length, then UTF-8 JSON payload.
"""
import ctypes
import json
import time
from ctypes import c_int, c_ulong, byref, create_string_buffer

try:
    from .mcp_server import (_ws, _sockaddr_in, _htons, _addr_u32,
                             AF_INET, SOCK_STREAM, SOCKET_ERROR,
                             WSAEWOULDBLOCK)
except ImportError:  # loaded outside the package context
    from mcp_server import (_ws, _sockaddr_in, _htons, _addr_u32,
                            AF_INET, SOCK_STREAM, SOCKET_ERROR,
                            WSAEWOULDBLOCK)

SOL_SOCKET = 0xFFFF
SO_RCVTIMEO = 0x1006
WSAETIMEDOUT = 10060
FIONBIO = -2147195266  # 0x8004667E as signed long

MAX_FRAME = 64 * 1024 * 1024

_started = [False]


def _startup():
    if _started[0]:
        return
    w = _ws()
    data = create_string_buffer(408)
    if w.WSAStartup(0x0202, data) != 0:
        raise OSError("WSAStartup failed: %d" % w.WSAGetLastError())
    _started[0] = True


class ConnectionClosed(Exception):
    """The peer went away or the stream is unrecoverably desynced."""


def connect(host, port):
    """Open a blocking TCP connection. Raises OSError on failure."""
    _startup()
    w = _ws()
    s = w.socket(AF_INET, SOCK_STREAM, 0)
    if s == SOCKET_ERROR:
        raise OSError("socket() failed: WSA %d" % w.WSAGetLastError())
    addr = _sockaddr_in(AF_INET, _htons(port), _addr_u32(host))
    if w.connect(s, byref(addr), ctypes.sizeof(addr)) == SOCKET_ERROR:
        err = w.WSAGetLastError()
        w.closesocket(s)
        raise OSError("connect %s:%d failed (WSA %d)" % (host, port, err))
    return s


def close(s):
    if s is not None and s != SOCKET_ERROR:
        _rxbuf.pop(s, None)  # handle numbers get reused; drop stale bytes
        try:
            _ws().closesocket(s)
        except Exception:
            pass


def set_recv_timeout_ms(s, ms):
    """Set SO_RCVTIMEO in milliseconds (0 = infinite)."""
    w = _ws()
    v = c_ulong(int(ms) & 0xFFFFFFFF)
    if w.setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, byref(v), 4) == SOCKET_ERROR:
        raise OSError("SO_RCVTIMEO failed: WSA %d" % w.WSAGetLastError())


def set_blocking(s, blocking=True):
    """Sockets accepted from a non-blocking listener inherit its mode on
    Windows; force blocking mode for the framed internal protocol."""
    w = _ws()
    w.ioctlsocket(s, FIONBIO, byref(c_ulong(0 if blocking else 1)))


def _send_all(s, data):
    w = _ws()
    total = len(data)
    view = create_string_buffer(data)
    sent = 0
    while sent < total:
        n = w.send(s, ctypes.byref(view, sent), total - sent, 0)
        if n == SOCKET_ERROR:
            err = w.WSAGetLastError()
            if err == WSAEWOULDBLOCK:
                time.sleep(0.005)
                continue
            raise ConnectionClosed("send failed: WSA %d" % err)
        if n == 0:
            raise ConnectionClosed("send returned 0")
        sent += n


def _recv_some(s):
    """Read whatever is available. Returns bytes, or None on timeout with
    nothing consumed. Raises ConnectionClosed on error / peer close."""
    w = _ws()
    chunk = create_string_buffer(65536)
    n = w.recv(s, chunk, 65536, 0)
    if n == SOCKET_ERROR:
        err = w.WSAGetLastError()
        if err in (WSAEWOULDBLOCK, WSAETIMEDOUT):
            return None
        raise ConnectionClosed("recv failed: WSA %d" % err)
    if n == 0:
        raise ConnectionClosed("connection closed by peer")
    return chunk.raw[:n]


def _recv_exact(s, n):
    """Read EXACTLY n bytes... note: raw recv may return more; callers that
    frame messages must use recv_frame() which keeps the excess."""
    buf = b""
    while len(buf) < n:
        got = _recv_some(s)
        if got is None:
            if buf:
                # timed out mid-frame: stream is desynced, unusable
                raise ConnectionClosed("timeout mid-frame (%d/%d bytes)"
                                       % (len(buf), n))
            return None
        buf += got
    return buf


# per-connection leftover bytes between recv_frame calls (a single recv can
# contain the tail of one frame and the head of the next)
_rxbuf = {}


def _parse_buffered(s):
    b = _rxbuf.get(s, b"")
    if len(b) < 4:
        return None
    n = b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
    if n <= 0 or n > MAX_FRAME:
        raise ConnectionClosed("bad frame length %d" % n)
    if len(b) < 4 + n:
        return None
    body = b[4:4 + n]
    rest = b[4 + n:]
    if rest:
        _rxbuf[s] = rest
    else:
        _rxbuf.pop(s, None)
    return json.loads(body.decode("utf-8"))


def send_frame(s, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    n = len(data)
    if n > MAX_FRAME:
        raise ValueError("frame too large: %d bytes" % n)
    prefix = bytes(bytearray([n & 0xFF, (n >> 8) & 0xFF,
                              (n >> 16) & 0xFF, (n >> 24) & 0xFF]))
    _send_all(s, prefix)
    _send_all(s, data)


def recv_frame(s):
    """Read one frame. Returns the decoded dict, or None if the idle timeout
    fired before a complete frame arrived (the connection stays usable and
    partially received bytes are kept for the next call)."""
    while True:
        frame = _parse_buffered(s)
        if frame is not None:
            return frame
        got = _recv_some(s)
        if got is None:
            return None  # idle timeout; buffered partial frame persists
        _rxbuf[s] = _rxbuf.get(s, b"") + got


def listen(host, port, backlog=16, reuse_addr=False):
    """Create a non-blocking listening socket (mirrors mcp_server's)."""
    _startup()
    w = _ws()
    s = w.socket(AF_INET, SOCK_STREAM, 0)
    if s == SOCKET_ERROR:
        raise OSError("socket() failed: WSA %d" % w.WSAGetLastError())
    if reuse_addr:
        w.setsockopt(s, SOL_SOCKET, 4, byref(c_int(1)), 4)  # SO_REUSEADDR
    addr = _sockaddr_in(AF_INET, _htons(port), _addr_u32(host))
    if w.bind(s, byref(addr), ctypes.sizeof(addr)) == SOCKET_ERROR:
        err = w.WSAGetLastError()
        w.closesocket(s)
        raise OSError("bind %s:%d failed: WSA %d" % (host, port, err))
    if w.listen(s, backlog) == SOCKET_ERROR:
        err = w.WSAGetLastError()
        w.closesocket(s)
        raise OSError("listen failed: WSA %d" % err)
    w.ioctlsocket(s, FIONBIO, byref(c_ulong(1)))
    return s
