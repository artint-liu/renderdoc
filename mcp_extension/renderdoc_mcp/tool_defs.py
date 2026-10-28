"""Pure-data tool registry for the RenderDoc MCP server.

This module must stay importable WITHOUT the ``renderdoc`` python module:
the standalone broker (plain system python) uses it to serve tools/list,
while the in-RenderDoc extension pairs it with rd_tools.TOOL_IMPL.

Every capture/analysis tool is *instance scoped*: the broker routes it to
one connected RenderDoc instance. The optional ``instance`` argument
(injected automatically below) selects which one; omit it when exactly one
instance is connected.
"""
import json

_INSTANCE_PROP = {
    "type": "string",
    "description": "Target RenderDoc instance: the id shown by "
                   "list_instances (e.g. 'rd1'), or a process id. Omit when "
                   "exactly one instance is connected; required to "
                   "disambiguate when several are.",
}

# tools that execute on a RenderDoc instance (routed by the broker)
_INSTANCE_TOOLS = [
    "get_capture_info", "get_current_state", "list_drawcalls",
    "get_drawcall_params", "get_screenshot", "get_pixel_history",
    "get_shader_disassembly", "get_texture_list", "get_buffer_list",
    "get_buffer_data", "get_event_usage", "get_debug_messages",
    "launch_app", "trigger_capture", "list_captures", "open_capture",
]


def _def(description, schema):
    return {"description": description, "schema": schema}


_BASE = {
    "get_capture_info": _def(
        "Get general info about the capture currently open in the target "
        "RenderDoc instance: file path, graphics API, frame number, resource "
        "counts and any API debug/validation messages.",
        {"type": "object", "properties": {}}),

    "get_current_state": _def(
        "Get the target instance's current UI state: whether a capture is "
        "loaded, which event is selected and whether a launched app is "
        "still connected.",
        {"type": "object", "properties": {}}),

    "list_drawcalls": _def(
        "List the drawcall/dispatch/copy action tree of the open capture as "
        "a flat list. Use an entry's event_id with get_drawcall_params for "
        "full details of a specific draw.",
        {"type": "object", "properties": {
            "include_markers": {"type": "boolean",
                                "description": "Include marker/debug regions.",
                                "default": False},
            "max_results": {"type": "integer", "default": 2000}}}),

    "get_drawcall_params": _def(
        "Get the full pipeline state at a given drawcall/dispatch: output "
        "targets, depth target, pipeline objects, bound shaders with "
        "reflection (inputs/outputs/constant blocks), all used descriptor "
        "bindings, viewport and API debug messages. Note: this moves the "
        "RenderDoc UI to that event.",
        {"type": "object", "properties": {
            "event_id": {"type": "integer",
                         "description": "EID from list_drawcalls"}},
         "required": ["event_id"]}),

    "get_screenshot": _def(
        "Save a texture to PNG and return it as an image so the agent can "
        "visually inspect the render output. Defaults to the first color "
        "output target at event_id (or the currently selected event if "
        "omitted); pass texture_id to screenshot any texture.",
        {"type": "object", "properties": {
            "event_id": {"type": "integer"},
            "texture_id": {"type": "string",
                           "description": "ResourceId like '123' or "
                                          "'ResourceId(123)' from "
                                          "get_texture_list"},
            "output_index": {"type": "integer", "default": 0}}}),

    "get_pixel_history": _def(
        "Get the full modification history of one pixel of a texture across "
        "the frame: which events wrote it, with which color/depth values "
        "and shader outputs. Essential for diagnosing 'why is this pixel "
        "wrong'.",
        {"type": "object", "properties": {
            "texture_id": {"type": "string"},
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "mip": {"type": "integer", "default": 0},
            "slice": {"type": "integer", "default": 0},
            "event_id": {"type": "integer"}},
         "required": ["texture_id", "x", "y"]}),

    "get_shader_disassembly": _def(
        "Get the disassembly of the shader bound at a stage for an event.",
        {"type": "object", "properties": {
            "event_id": {"type": "integer"},
            "stage": {"type": "string", "default": "pixel",
                      "description": "vertex/hull/domain/geometry/pixel/"
                                     "compute"},
            "target": {"type": "string",
                       "description": "Optional disassembly target; see "
                                      "returned 'targets' list"}},
         "required": ["event_id"]}),

    "get_texture_list": _def(
        "List textures/resources in the capture with id, name, dimensions "
        "and format.",
        {"type": "object", "properties": {
            "max_results": {"type": "integer", "default": 500},
            "name_filter": {"type": "string"}}}),

    "get_buffer_list": _def(
        "List buffers in the capture with id, name and length.",
        {"type": "object", "properties": {
            "max_results": {"type": "integer", "default": 500},
            "name_filter": {"type": "string"}}}),

    "get_buffer_data": _def(
        "Read raw bytes of a buffer as a hexdump (plus base64 if <= 4096 "
        "bytes).",
        {"type": "object", "properties": {
            "buffer_id": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "length": {"type": "integer", "default": 1024}},
         "required": ["buffer_id"]}),

    "get_event_usage": _def(
        "Get every event where a given resource is used (read/written), for "
        "tracing resource lifetime through the frame.",
        {"type": "object", "properties": {
            "resource_id": {"type": "string"}},
         "required": ["resource_id"]}),

    "get_debug_messages": _def(
        "Get all API debug/validation messages emitted by the capture's "
        "debug layer.",
        {"type": "object", "properties": {}}),

    "launch_app": _def(
        "Launch a program under RenderDoc's capture control in the target "
        "instance - equivalent to filling the Launch Application window and "
        "clicking Launch. Pass exe (plus optional working_dir/cmdline) to "
        "debug a specific program; without them the dialog's current "
        "settings are used (loading qrenderdoc's most-recent settings if "
        "empty, same as clicking its top recent entry). Use "
        "trigger_capture afterwards to capture frames, then list_captures "
        "/ open_capture to load one for analysis.",
        {"type": "object", "properties": {
            "exe": {"type": "string",
                    "description": "Path of the executable to launch and "
                                   "capture (set into the dialog like "
                                   "typing it in the UI)"},
            "working_dir": {"type": "string",
                            "description": "Working directory; empty/omit "
                                           "= the executable's directory"},
            "cmdline": {"type": "string",
                        "description": "Command line arguments for the "
                                       "program"},
            "settings_file": {"type": "string",
                              "description": "Optional .cap settings file "
                                             "to load before launching "
                                             "(ignored when exe is given)"}}}),

    "trigger_capture": _def(
        "Ask the program launched by launch_app to capture a frame after "
        "delay_seconds (0 = immediately).",
        {"type": "object", "properties": {
            "delay_seconds": {"type": "number", "default": 0},
            "num_frames": {"type": "integer", "default": 1,
                           "description": "How many frames to capture "
                                          "starting at the delayed one."}}}),

    "list_captures": _def(
        "List the captures made so far on the connection launched by "
        "launch_app, with their capture_id for open_capture.",
        {"type": "object", "properties": {}}),

    "open_capture": _def(
        "Open a captured frame (by capture_id from list_captures) into the "
        "target instance's UI so the analysis tools can inspect it. Poll "
        "get_current_state until capture_loaded.",
        {"type": "object", "properties": {
            "capture_id": {"type": "integer"}},
         "required": ["capture_id"]}),
}

# tools that execute inside the broker itself (not routed to an instance)
BROKER_TOOL_NAMES = ("list_instances", "start_renderdoc")

_BASE["list_instances"] = _def(
    "List every RenderDoc instance connected to this MCP broker with its "
    "instance id, process id, executable and capture state. Call this first "
    "when several RenderDoc windows may be running, then pass the chosen id "
    "as the 'instance' argument of the other tools.",
    {"type": "object", "properties": {}})

_BASE["start_renderdoc"] = _def(
    "Start a new qrenderdoc.exe process; once its renderdoc_mcp extension "
    "loads it registers with this broker as a new instance that can be "
    "driven through the other tools. exe defaults to the "
    "RENDERDOC_MCP_RD_EXE environment variable, then to the path used by "
    "the previous successful call.",
    {"type": "object", "properties": {
        "exe": {"type": "string",
                "description": "Path to qrenderdoc.exe"},
        "args": {"type": "string",
                 "description": "Optional extra command line for "
                                "qrenderdoc.exe"},
        "working_dir": {"type": "string"},
        "wait_seconds": {"type": "integer", "default": 45,
                         "description": "How long to wait for the new "
                                        "instance to register with this "
                                        "broker (0 = don't wait)"}}})


def _inject_instance(schema):
    out = {"type": schema.get("type", "object")}
    props = dict(schema.get("properties", {}))
    props["instance"] = dict(_INSTANCE_PROP)
    out["properties"] = props
    if "required" in schema:
        out["required"] = list(schema["required"])
    return out


def build_defs():
    """name -> {"description", "schema"} for ALL tools, with the optional
    'instance' argument added to every instance-scoped tool."""
    defs = {}
    for name in _INSTANCE_TOOLS:
        d = _BASE[name]
        defs[name] = {"description": d["description"],
                      "schema": _inject_instance(d["schema"])}
    for name in BROKER_TOOL_NAMES:
        d = _BASE[name]
        defs[name] = {"description": d["description"],
                      "schema": d["schema"]}
    return defs


TOOL_DEFS = build_defs()


def text_result(obj):
    """JSON-encode a tool result as MCP text content (shared by broker and
    instance-side dispatchers)."""
    if isinstance(obj, str):
        text = obj
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=1, default=str)
    if len(text) > 400000:
        text = text[:400000] + "\n... (truncated, narrow your query)"
    return [{"type": "text", "text": text}]
