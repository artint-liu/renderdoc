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
    "get_buffer_data", "get_buffer_regions", "buffer_search",
    "get_vertex_data", "get_event_usage", "get_debug_messages",
    "launch_app", "trigger_capture", "list_captures", "open_capture",
    "open_capture_file", "close_capture", "capture_and_open",
    "get_texture_stats", "compare_regions", "get_constant_buffer",
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
            "actions_only": {"type": "boolean", "default": False,
                             "description": "Skip non-action events "
                                            "(markers/state rows); only "
                                            "draws/dispatches/clears/copies/"
                                            "presents remain"},
            "flags_include": {"type": "array",
                              "items": {"type": "string"},
                              "description": "Keep only actions whose "
                                             "flags contain one of these "
                                             "substrings, e.g. "
                                             "['Drawcall', 'Clear', "
                                             "'Present']"},
            "max_results": {"type": "integer", "default": 2000,
                            "description": "Cap after filtering; 0 = "
                                           "unlimited. When truncated the "
                                           "output has truncated=true and "
                                           "total_matched=N"}}}),

    "get_drawcall_params": _def(
        "Get the full pipeline state at a given drawcall/dispatch: output "
        "targets, depth target, pipeline objects, bound shaders with "
        "reflection (inputs/outputs/constant blocks), all used descriptor "
        "bindings, vertex inputs with human-readable formats and viewport. "
        "Note: this moves the RenderDoc UI to that event.",
        {"type": "object", "properties": {
            "event_id": {"type": "integer",
                         "description": "EID from list_drawcalls"}},
         "required": ["event_id"]}),

    "get_screenshot": _def(
        "Save a texture to PNG and return it as an image so the agent can "
        "visually inspect the render output. Defaults to the first color "
        "output target at event_id (or the currently selected event if "
        "omitted); pass texture_id to screenshot any texture. Large "
        "responses are avoided with region(x, y, w, h) to capture only a "
        "sub-rectangle, max_width to downscale before encoding, and/or "
        "save_to(path) to write the PNG to disk and return only the path "
        "(agent reads the file itself). region/max_width need a directly "
        "decodable format (UNORM8/16, SNORM8, F16/F32); plain save_to works "
        "for every format.",
        {"type": "object", "properties": {
            "event_id": {"type": "integer"},
            "texture_id": {"type": "string",
                           "description": "ResourceId like '123' or "
                                          "'ResourceId(123)' from "
                                          "get_texture_list"},
            "output_index": {"type": "integer", "default": 0},
            "region": {"type": "array", "items": {"type": "integer"},
                       "minItems": 4, "maxItems": 4,
                       "description": "Crop before encoding: [x, y, w, h] "
                                      "in texture pixels"},
            "max_width": {"type": "integer",
                          "description": "Downscale the (cropped) image to "
                                         "this width before PNG encoding"},
            "save_to": {"type": "string",
                        "description": "Write the PNG to this absolute path "
                                       "and return the path instead of "
                                       "inlining base64 (parent dirs are "
                                       "created; .png appended if missing)"}}}),

    "get_pixel_history": _def(
        "Get the full modification history of one pixel of a texture across "
        "the frame: which events wrote it, with which color/depth values "
        "and shader outputs. Essential for diagnosing 'why is this pixel "
        "wrong'. Modification field names are snake_case: event_id, "
        "frag_index, primitive, passed, rejected_by, unboundPS, "
        "directShaderWrite, preMod, shaderOut, postMod (summary mode: "
        "shader_out, pre_mod, post_mod, depth_in, depth_out, stencil_in, "
        "stencil_out).",
        {"type": "object", "properties": {
            "texture_id": {"type": "string"},
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "mip": {"type": "integer", "default": 0},
            "slice": {"type": "integer", "default": 0},
            "event_id": {"type": "integer"},
            "mode": {"type": "string", "default": "raw",
                     "description": "'raw' (full values, default) or "
                                    "'summary' (one compact record per "
                                    "modification: event_id, frag_index, "
                                    "primitive, passed, rejected_by, "
                                    "unboundPS, shader_out/pre_mod/"
                                    "post_mod as [r,g,b,a] floats, "
                                    "depth_in/out, stencil_in/out)"}},
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

    "get_buffer_regions": _def(
        "Classify a buffer's bytes into merged same-class regions: '0x00' "
        "runs, '0xCD' runs (MSVC debug-heap fill - the signature of a GPU "
        "heap/instance that was never written by the program), uniform "
        "'0xNN' runs and 'mixed'. Answers 'how much of this buffer was "
        "actually written, and where' without shipping a hexdump to the "
        "agent. Also reports bytes_by_class and first_written_offset.",
        {"type": "object", "properties": {
            "buffer_id": {"type": "string"},
            "event_id": {"type": "integer",
                         "description": "Event to replay before reading "
                                        "(omit = currently selected)"},
            "max_regions": {"type": "integer", "default": 64,
                            "description": "Cap on returned regions; when "
                                           "exceeded the tail is merged and "
                                           "truncated=true is set"}},
         "required": ["buffer_id"]}),

    "buffer_search": _def(
        "Search a buffer's bytes for a pattern and get absolute byte "
        "offsets: type 'u32' (little-endian integer), 'f32' (exact IEEE "
        "float bits, e.g. pattern=1076.0 for 0x44856000) or 'hex' (hex "
        "string). stride_align restricts hits to offsets divisible by it "
        "(e.g. an 88-byte vertex stride) and max_hits caps the list while "
        "hits_total keeps counting. This answers 'where in the 1MB vertex "
        "buffer is the pixel quad' without eyeballing hexdumps.",
        {"type": "object", "properties": {
            "buffer_id": {"type": "string"},
            "pattern": {"description": "Integer (u32), number (f32) or hex "
                                       "string like '44856000' or '0xCD'"},
            "type": {"type": "string", "default": "u32",
                     "description": "u32 | f32 | hex"},
            "stride_align": {"type": "integer", "default": 1,
                             "description": "Only report offsets divisible "
                                            "by this (vertex stride etc.)"},
            "max_hits": {"type": "integer", "default": 32,
                         "description": "Max offsets returned (0 = only "
                                        "count)"},
            "event_id": {"type": "integer"}},
         "required": ["buffer_id", "pattern"]}),

    "get_vertex_data": _def(
        "Decode vertices out of a vertex/instance buffer with a stride-"
        "aware attribute layout - one row per vertex from start_vertex, "
        "replacing several get_buffer_data calls plus manual hex offset "
        "arithmetic (e.g. an 88-byte-stride quad at offsets 4/88/92/96). "
        "attrs = [{name, byte_offset, type, count?}] with type in f32, f16, "
        "u32, s32, u16, s16, u8, s8, unorm8x4; count expands an attribute "
        "to consecutive elements (e.g. a 3-float position).",
        {"type": "object", "properties": {
            "buffer_id": {"type": "string"},
            "start_vertex": {"type": "integer", "default": 0},
            "count": {"type": "integer", "default": 1,
                      "description": "How many vertices to decode (cap "
                                     "4096)"},
            "stride": {"type": "integer",
                       "description": "Vertex stride in bytes"},
            "attrs": {"type": "array",
                      "items": {"type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "byte_offset": {"type": "integer"},
                                    "type": {"type": "string"},
                                    "count": {"type": "integer"}}},
                      "description": "Attribute layout inside one stride"},
            "event_id": {"type": "integer"}},
         "required": ["buffer_id", "stride", "attrs"]}),

    "get_event_usage": _def(
        "Get every event where a given resource is used (read/written), for "
        "tracing resource lifetime through the frame. IMPORTANT limitation: "
        "host-side persistent/readonly-heap writes (vkMapMemory + memcpy "
        "without an explicit unmap event) do NOT appear here as events - "
        "data uploaded that way is only visible in the buffer/texture "
        "content itself (see get_buffer_regions / buffer_search).",
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
        "/ open_capture to load one for analysis. The result includes "
        "child_capture_hint: whether 'Automatically capture child "
        "processes' (CaptureOptions.hookIntoChildren) is enabled - when "
        "true, child processes spawned by the program (e.g. a launcher "
        "spawning the real renderer) are captured too; when 'unknown', "
        "verify the toggle in the Launch Application UI or qrenderdoc's "
        "settings file. stdout limitation: the embedded Launch does NOT "
        "support stdout/stderr redirection (passing handles would break "
        "the capture injection) - if you need the program's log output, "
        "have the program itself write a log file; the program's stdout is "
        "consumed by the RenderDoc host process.",
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
        "launch_app, with their capture_id for open_capture. Each entry "
        "has an 'opened' flag (the capture currently open in the UI); "
        "captures_total gives the count. Note: capture_id is only unique "
        "per connection and restarts at 0 for every launched program - "
        "match timestamps when older same-id captures may exist. This only "
        "covers launch_app connections; captures made manually (hotkey) "
        "are on disk instead - see list_capture_files.",
        {"type": "object", "properties": {}}),

    "open_capture": _def(
        "Open a captured frame (by capture_id from list_captures) into the "
        "target instance's UI so the analysis tools can inspect it. Poll "
        "get_current_state until capture_loaded. Unknown ids fail with the "
        "list of currently available [capture_id -> timestamp] entries. "
        "Only captures from a launch_app connection; for manually captured "
        "frames use open_capture_file(path) instead.",
        {"type": "object", "properties": {
            "capture_id": {"type": "integer"}},
         "required": ["capture_id"]}),

    "open_capture_file": _def(
        "Load a .rdc capture file from disk into the target instance's UI, "
        "replacing whatever capture is open - the way to analyze frames "
        "that were captured MANUALLY (hotkey captures of programs started "
        "outside this extension): list_capture_files finds the files, this "
        "tool loads one into an already-running instance instead of "
        "spawning a new one per frame (unlike start_renderdoc(args=path)), "
        "so you can step frame23414 -> frame23423 in one instance. Loading "
        "is asynchronous; with wait_seconds>0 (default) the call waits "
        "until capture_loaded or reports the failure. frame_number is "
        "parsed from the ..._frame<N>.rdc filename when present.",
        {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Absolute path to a .rdc file (from "
                                    "list_capture_files)"},
            "wait_seconds": {"type": "number", "default": 120,
                             "description": "Wait for the load to finish "
                                            "(0 = queue it and return "
                                            "immediately, then poll "
                                            "get_current_state)"}},
         "required": ["path"]}),

    "close_capture": _def(
        "Remove a capture (by capture_id from list_captures) from the live "
        "capture connection so it stops cluttering the list and holds no "
        "temp file. Implemented with RenderDoc's per-capture removal API "
        "(CaptureConnection.DeleteCapture, no save prompt); opening "
        "another capture afterwards is unaffected.",
        {"type": "object", "properties": {
            "capture_id": {"type": "integer"}},
         "required": ["capture_id"]}),

    "capture_and_open": _def(
        "One-shot capture: TimedCapture -> wait for the new capture to "
        "appear -> OpenCapture it -> wait until loaded -> return "
        "{capture_id, frame_number, timestamp, api, num_actions, "
        "first_actions}. first_actions holds the first actions_summary "
        "actions of the frame ({event_id, flags, name, numIndices, "
        "numInstances}). Replaces the manual trigger_capture / "
        "list_captures / open_capture / get_current_state polling loop.",
        {"type": "object", "properties": {
            "delay_seconds": {"type": "number", "default": 0},
            "num_frames": {"type": "integer", "default": 1},
            "poll_interval_seconds": {"type": "number", "default": 0.5},
            "timeout_seconds": {"type": "number", "default": 30,
                                "description": "Overall budget for each "
                                               "phase (wait for capture, "
                                               "wait for load)"},
            "actions_summary": {"type": "integer", "default": 10,
                                "description": "How many first actions to "
                                               "include in the summary"}}}),

    "get_texture_stats": _def(
        "Numerical statistics of a rectangular region of a texture at an "
        "event: per-channel {mean, std, min, max} computed from the raw "
        "texture bytes (no PNG round-trip, no channel-order ambiguity). "
        "Supports UNORM8/UNORM16/SNORM8/FLOAT16/FLOAT32 formats; the "
        "response names the decoded format.",
        {"type": "object", "properties": {
            "texture_id": {"type": "string"},
            "event_id": {"type": "integer",
                         "description": "Event to replay before reading "
                                        "(omit = currently selected)"},
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "w": {"type": "integer"}, "h": {"type": "integer"},
            "mip": {"type": "integer", "default": 0},
            "slice": {"type": "integer", "default": 0}},
         "required": ["texture_id", "x", "y", "w", "h"]}),

    "compare_regions": _def(
        "Compare the same rectangular region of two textures (optionally "
        "at different events) numerically: per-side per-channel stats plus "
        "max_abs_diff / mean_abs_diff / pixels_over_threshold. Same texture "
        "and event = perfect baseline check (all diffs 0). Region sizes or "
        "formats that differ yield per-side stats only, with a reason.",
        {"type": "object", "properties": {
            "texture_a": {"type": "string"},
            "event_a": {"type": "integer"},
            "texture_b": {"type": "string"},
            "event_b": {"type": "integer"},
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "w": {"type": "integer"}, "h": {"type": "integer"},
            "threshold": {"type": "number", "default": 0.00784,
                          "description": "Per-value diff above which a "
                                         "pixel counts as different "
                                         "(default 2/255)"},
            "mip": {"type": "integer", "default": 0},
            "slice": {"type": "integer", "default": 0}},
         "required": ["texture_a", "texture_b", "x", "y", "w", "h"]}),

    "get_constant_buffer": _def(
        "Export a constant buffer (cbuffer) bound at an event: reflection "
        "variable layout ({name, byte_offset, type}) plus raw contents as "
        "floats, u32s and a hexdump. The primary tool for comparing "
        "transform/matrices between two RenderDoc instances.",
        {"type": "object", "properties": {
            "event_id": {"type": "integer"},
            "stage": {"type": "string", "default": "pixel",
                      "description": "vertex/hull/domain/geometry/pixel/"
                                     "compute"},
            "slot": {"type": "integer",
                     "description": "Index into the shader reflection's "
                                    "constantBlocks (see "
                                    "get_drawcall_params -> "
                                    "reflection.constantBlocks)"},
            "max_floats": {"type": "integer", "default": 64,
                           "description": "How many floats/u32s to return "
                                          "from the buffer start"}},
         "required": ["event_id", "slot"]}),
}

# tools that execute inside the broker itself (not routed to an instance)
BROKER_TOOL_NAMES = ("list_instances", "start_renderdoc",
                     "shutdown_instance", "list_capture_files")

_BASE["list_instances"] = _def(
    "List every RenderDoc instance connected to this MCP broker with its "
    "instance id, process id, executable and capture state (pid, "
    "started_at, last_used, last_seen, state active/stale, "
    "has_capture_connection and the open capture's filename/frame_number "
    "included). Also reports 'rd_exe': the qrenderdoc.exe path that "
    "start_renderdoc will launch (fixed at broker startup). Call this "
    "first when several RenderDoc windows may be running, then pass the "
    "chosen id as the 'instance' argument of the other tools. RenderDoc "
    "instances that start while the broker is down retry registration "
    "every 2s and appear here automatically once it is back.",
    {"type": "object", "properties": {}})

_BASE["shutdown_instance"] = _def(
    "Gracefully close one RenderDoc instance (main-window close scheduled "
    "on its UI thread) and remove it from the broker's instance table. If "
    "the process does not exit within the grace period (e.g. a save prompt "
    "is blocking), it is force-killed and the response notes method='kill'. "
    "Other instances and the broker itself are unaffected; when a hosted "
    "broker's instance is shut down, another instance takes over the "
    "endpoint.",
    {"type": "object", "properties": {
        "instance": {"type": "string",
                     "description": "Target RenderDoc instance: the id "
                                    "shown by list_instances (e.g. 'rd1'), "
                                    "or a process id. Required to "
                                    "disambiguate when several are "
                                    "connected."}},
     "required": ["instance"]})

_BASE["start_renderdoc"] = _def(
    "Start the qrenderdoc.exe configured at broker startup (--exe argument "
    "or RENDERDOC_MCP_RD_EXE environment variable; the path is shown in "
    "the broker startup log and in list_instances' rd_exe field). Once the "
    "new process's renderdoc_mcp extension loads it registers with this "
    "broker as a new instance that can be driven through the other tools. "
    "The window is shown with SW_SHOWNOACTIVATE so it never steals the "
    "user's focus. To analyze a .rdc file you can pass it as args "
    "(\"<path.rdc>\") - but prefer open_capture_file on an existing "
    "instance to avoid one process per frame.",
    {"type": "object", "properties": {
        "args": {"type": "string",
                 "description": "Optional extra command line for "
                                "qrenderdoc.exe"},
        "working_dir": {"type": "string"},
        "wait_seconds": {"type": "integer", "default": 45,
                         "description": "How long to wait for the new "
                                        "instance to register with this "
                                        "broker (0 = don't wait)"}}})

_BASE["list_capture_files"] = _def(
    "Enumerate .rdc capture files on disk WITHOUT needing any connected "
    "RenderDoc instance. RenderDoc writes captures of manually-started "
    "programs (hotkey captures made outside this extension) into the "
    "'RenderDoc' folder inside %TEMP% as <app>_<launch timestamp>_frame<N>"
    ".rdc (or ..._capture.rdc). This is the entry point for analyzing "
    "manual captures: list files here, then open_capture_file(path) on a "
    "connected instance (or start_renderdoc with args=\"<path>\") to load "
    "one. Returns file, app, launch_timestamp, frame_number, size and "
    "mtime per capture.",
    {"type": "object", "properties": {
        "dir": {"type": "string",
                "description": "Directory to scan (default: the RenderDoc "
                               "folder inside %TEMP%)"},
        "name_filter": {"type": "string",
                        "description": "Case-insensitive substring filter "
                                       "on the file name"},
        "max_results": {"type": "integer", "default": 200,
                        "description": "Cap on returned entries (newest "
                                       "first)"}}})


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
