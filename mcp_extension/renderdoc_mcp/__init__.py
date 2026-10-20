"""RenderDoc MCP Server - UI extension.

Adds a 'Tools -> RenderDoc MCP Server' menu item. Selecting it opens a
visible server window and starts a local MCP server that lets AI agents
inspect the capture open in this RenderDoc instance.

Uniqueness: a Win32 named mutex (plus the port binding) guarantees only one
server instance runs per system/port. A second RenderDoc instance will show
'server already running' instead of starting a duplicate.
"""
import os

import qrenderdoc as qrd

try:
    from . import mcp_server, rd_tools
except ImportError:
    # loaded outside the package context: add this file's directory to
    # sys.path and import the submodules as top-level modules
    import os as _os
    import sys as _sys

    _here = _os.path.dirname(_os.path.abspath(__file__))
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    import mcp_server
    import rd_tools

_PORT = int(os.environ.get("RENDERDOC_MCP_PORT", "30007"))
_MUTEX_NAME = "Local\\RenderDocMCP_%d" % _PORT

_state = {
    "server": None,       # mcp_server.MCPServer
    "mutex": None,        # win32 HANDLE (or None on non-windows)
    "top": None,          # docked window widget
    "status_label": None,
    "toggle_btn": None,   # Start/Stop toggle button
    "log_box": None,
    "mqt": None,          # MiniQtHelper
}
_log_lines = []


# ---------------------------------------------------------------------------
# logging (thread-safe, marshalled onto the UI thread)
# ---------------------------------------------------------------------------

def _ui_log(msg):
    _log_lines.append(msg)
    del _log_lines[:-400]
    text = "\n".join(_log_lines)

    mqt = _state["mqt"]
    box = _state["log_box"]
    if mqt is None or box is None:
        print("[renderdoc_mcp] " + msg)
        return

    def update():
        m = _state["mqt"]
        b = _state["log_box"]
        if m is not None and b is not None:
            m.SetWidgetText(b, text)

    mqt.InvokeOntoUIThread(update)


def _set_running_ui(running, status_msg):
    """Update the status label and the toggle button (thread-safe)."""
    mqt = _state["mqt"]
    if mqt is None:
        return

    def update():
        m = _state["mqt"]
        if m is None:
            return
        label = _state["status_label"]
        if label is not None:
            m.SetWidgetText(label, status_msg)
        btn = _state["toggle_btn"]
        if btn is not None:
            m.SetWidgetText(btn, "Stop Server" if running else "Start Server")

    mqt.InvokeOntoUIThread(update)


# ---------------------------------------------------------------------------
# process uniqueness via win32 named mutex
# ---------------------------------------------------------------------------

def _acquire_mutex():
    if os.name != "nt":
        return object()  # non-windows: rely on the port binding alone
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return None
        # ERROR_ALREADY_EXISTS == 183
        if ctypes.get_last_error() == 183:
            k32.CloseHandle(handle)
            return None
        return handle
    except Exception as e:
        _ui_log("[MCP] mutex unavailable (%s), falling back to port binding "
                "for uniqueness" % e)
        return object()


def _release_mutex():
    mutex = _state["mutex"]
    _state["mutex"] = None
    if mutex is None or os.name != "nt":
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(mutex)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# server lifecycle
# ---------------------------------------------------------------------------

def _start_server():
    if _state["server"] is not None:
        _ui_log("[MCP] server already running")
        return
    mutex = _acquire_mutex()
    if mutex is None:
        _ui_log("[MCP] another RenderDoc MCP Server instance is already "
                "running on this machine (port %d). Not starting a "
                "duplicate." % _PORT)
        _set_running_ui(False, "Status: another instance is already running "
                               "on port %d. Close it there first." % _PORT)
        return
    try:
        srv = mcp_server.MCPServer(rd_tools.TOOLS, log_cb=_ui_log,
                                   port=_PORT)
        srv.start()
    except OSError as e:
        _release_mutex()
        _ui_log("[MCP] failed to bind port %d: %s" % (_PORT, e))
        _set_running_ui(False, "Status: failed to bind port %d (%s). Is the "
                               "port in use? Set RENDERDOC_MCP_PORT to "
                               "change it." % (_PORT, e))
        return
    _state["server"] = srv
    _state["mutex"] = mutex
    _set_running_ui(True, "Status: RUNNING at %s\n"
                          "Serving the capture open in this RenderDoc "
                          "window." % srv.url)


def _stop_server(reason=""):
    srv = _state["server"]
    if srv is not None:
        srv.stop()
        _state["server"] = None
        _ui_log("[MCP] server stopped" + (" (%s)" % reason if reason else ""))
    _release_mutex()
    if _state["mqt"] is not None and _state["status_label"] is not None:
        _set_running_ui(False, "Status: STOPPED" +
                        (" (%s)" % reason if reason else "") +
                        ". Click 'Start Server' to restart at "
                        "http://127.0.0.1:%d/mcp" % _PORT)


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------

def _toggle_clicked(ctx=None, widget=None, text=None):
    if _state["server"] is not None:
        _stop_server("user stop")
    else:
        _start_server()


def _clear_clicked(ctx=None, widget=None, text=None):
    del _log_lines[:]
    _ui_log("[MCP] log cleared")


def _window_closed(ctx=None, widget=None, text=None):
    _stop_server("window closed")
    _state["top"] = None
    _state["log_box"] = None
    _state["status_label"] = None
    _state["toggle_btn"] = None
    _state["mqt"] = None


def _open_window(ctx, data):
    rd_tools.set_context(ctx)
    mqt = ctx.Extensions().GetMiniQtHelper()
    _state["mqt"] = mqt

    top = mqt.CreateToplevelWidget("RenderDoc MCP Server", _window_closed)

    status = mqt.CreateLabel()
    mqt.SetWidgetText(status, "Status: starting...")

    buttons = mqt.CreateHorizontalContainer()
    toggle_btn = mqt.CreateButton(_toggle_clicked)
    mqt.SetWidgetText(toggle_btn, "Start Server")
    clear_btn = mqt.CreateButton(_clear_clicked)
    mqt.SetWidgetText(clear_btn, "Clear Log")
    mqt.AddWidget(buttons, toggle_btn)
    mqt.AddWidget(buttons, clear_btn)

    log_box = mqt.CreateTextBox(True)
    mqt.SetWidgetText(log_box, "")
    mqt.SetWidgetEnabled(log_box, False)

    mqt.AddWidget(top, status)
    mqt.AddWidget(top, buttons)
    mqt.AddWidget(top, log_box)

    _state["top"] = top
    _state["status_label"] = status
    _state["toggle_btn"] = toggle_btn
    _state["log_box"] = log_box

    ctx.AddDockWindow(top, qrd.DockReference.NewFloatingArea, None)

    _ui_log("[MCP] window opened, server starting on port %d "
            "(endpoint /mcp)" % _PORT)
    _start_server()


# ---------------------------------------------------------------------------
# extension entry points
# ---------------------------------------------------------------------------

def register(version, pyrenderdoc):
    print("[renderdoc_mcp] registered in RenderDoc %s "
          "(Tools -> RenderDoc MCP Server, port %d)"
          % (version, _PORT))
    pyrenderdoc.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools, ["RenderDoc MCP Server"], _open_window)
    rd_tools.set_context(pyrenderdoc)
    # auto-start so agents can use the server right after launch; the Tools
    # menu window can still be opened later for status (start is a no-op then)
    _start_server()


def unregister():
    _stop_server("extension reloaded")
    top = _state["top"]
    _state["top"] = None
    _state["log_box"] = None
    _state["status_label"] = None
    _state["toggle_btn"] = None
    mqt = _state["mqt"]
    _state["mqt"] = None
    if top is not None and mqt is not None:
        try:
            mqt.CloseToplevelWidget(top)
        except Exception:
            pass
