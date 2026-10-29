"""RenderDoc MCP extension - one MCP broker, many RenderDoc instances.

Every RenderDoc instance running this extension connects to the local MCP
broker and registers as an instance ('rd1', 'rd2', ...). If no broker is
running yet, the FIRST instance hosts it (or use the standalone
rdmcp_broker.py BEFORE starting RenderDoc). When a broker's host disappears,
another instance automatically takes over.

AI agents talk to the broker over HTTP (http://127.0.0.1:<port>/mcp), call
list_instances to see the connected RenderDocs, and target a specific one
with the 'instance' argument of every capture/analysis tool.
"""
import os
import sys
import traceback

import qrenderdoc as qrd

try:
    from . import mcp_server, rd_tools, spoke as spoke_mod
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
    import spoke as spoke_mod

_VERSION = "0.3.0"
_PORT = int(os.environ.get("RENDERDOC_MCP_PORT", "30007"))
_REG_PORT = int(os.environ.get("RENDERDOC_MCP_REG_PORT", str(_PORT + 1)))
_REG_HOST = "127.0.0.1"

_state = {
    "spoke": None,        # spoke_mod.SpokeClient
    "broker": None,       # broker.Broker when this process hosts it
    "instance_id": None,
    "top": None,          # docked window widget
    "status_label": None,
    "toggle_btn": None,
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


def _refresh_ui():
    mqt = _state["mqt"]
    if mqt is None:
        return

    def update():
        m = _state["mqt"]
        if m is None:
            return
        label = _state["status_label"]
        btn = _state["toggle_btn"]
        spoke = _state["spoke"]
        broker = _state["broker"]
        if spoke is None:
            status = ("Status: STOPPED. Click 'Start Server' to connect to "
                      "or host the MCP broker at http://127.0.0.1:%d/mcp"
                      % _PORT)
            btn_text = "Start Server"
            running = False
        elif broker is not None:
            running = True
            btn_text = "Stop Server"
            status = ("Status: BROKER HOST %s\nThis RenderDoc is instance %s "
                      "and serves every connected instance."
                      % (broker.url, _state["instance_id"]))
        else:
            running = True
            btn_text = "Disconnect"
            status = ("Status: connected to broker as instance %s "
                      "(endpoint http://127.0.0.1:%d/mcp)"
                      % (_state["instance_id"], _PORT))
        if label is not None:
            m.SetWidgetText(label, status)
        if btn is not None:
            m.SetWidgetText(btn, btn_text)

    mqt.InvokeOntoUIThread(update)


# ---------------------------------------------------------------------------
# spoke dispatch/status glue
# ---------------------------------------------------------------------------

def _dispatch(tool, args):
    """Run an instance-scoped tool of THIS RenderDoc. Never raises."""
    fn = rd_tools.TOOL_IMPL.get(tool)
    if fn is None:
        return [{"type": "text", "text": "Unknown tool: %s" % tool}], True
    try:
        content = fn(args)
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        return content, False
    except Exception as e:
        return ([{"type": "text",
                  "text": "%s: %s\n%s" % (type(e).__name__, e,
                                          traceback.format_exc())}], True)


def _self_exe():
    try:
        return sys.executable or ""
    except Exception:
        return ""


def _instance_meta():
    return {"pid": os.getpid(), "exe": _self_exe(), "version": _VERSION,
            "hostname": os.environ.get("COMPUTERNAME", "")}


def _on_instance(iid, welcome):
    _state["instance_id"] = iid
    rd_tools.set_instance_id(iid)
    _ui_log("[mcp] ready as instance %s" % iid)
    _refresh_ui()


def _on_hosted(broker):
    _state["broker"] = broker
    iid = broker.local_instance_id()
    _state["instance_id"] = iid
    rd_tools.set_instance_id(iid)
    _refresh_ui()


def _on_stopped():
    _state["broker"] = None
    _state["instance_id"] = None
    rd_tools.set_instance_id(None)
    _refresh_ui()


# ---------------------------------------------------------------------------
# role lifecycle
# ---------------------------------------------------------------------------

def _start_role():
    if _state["spoke"] is not None:
        _ui_log("[mcp] already running")
        return
    client = spoke_mod.SpokeClient(
        _REG_HOST, _REG_PORT, _PORT, dispatch=_dispatch,
        status_fn=rd_tools.current_status, meta=_instance_meta(),
        log_cb=_ui_log, on_instance=_on_instance, on_hosted=_on_hosted,
        on_stopped=_on_stopped)
    _state["spoke"] = client
    client.start()
    _refresh_ui()


def _stop_role():
    client = _state["spoke"]
    if client is not None:
        _state["spoke"] = None
        client.stop()
        _ui_log("[mcp] stopped" +
                (" (was instance %s)" % _state["instance_id"]
                 if _state["instance_id"] else ""))


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------

def _toggle_clicked(ctx=None, widget=None, text=None):
    if _state["spoke"] is not None:
        _stop_role()
    else:
        _start_role()


def _clear_clicked(ctx=None, widget=None, text=None):
    del _log_lines[:]
    _ui_log("[mcp] log cleared")


def _window_closed(ctx=None, widget=None, text=None):
    # closing the status window must NOT stop the broker/role: other RenderDoc
    # instances and agents may depend on it. Use the Stop button for that.
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

    _ui_log("[mcp] MCP endpoint http://127.0.0.1:%d/mcp | instance "
            "registration port %d" % (_PORT, _REG_PORT))
    _refresh_ui()
    _start_role()


# ---------------------------------------------------------------------------
# extension entry points
# ---------------------------------------------------------------------------

def register(version, pyrenderdoc):
    print("[renderdoc_mcp] %s registered in RenderDoc %s "
          "(endpoint http://127.0.0.1:%d/mcp)"
          % (_VERSION, version, _PORT))
    pyrenderdoc.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools, ["RenderDoc MCP Server"], _open_window)
    rd_tools.set_context(pyrenderdoc)
    # auto-start so agents can use the server right after launch; the Tools
    # menu window can still be opened later for status (start is a no-op then)
    _start_role()


def unregister():
    _stop_role()
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
