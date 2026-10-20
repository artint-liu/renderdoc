"""Tool implementations for the RenderDoc MCP server.

Every replay-dependent access goes through pyrenderdoc.Replay().BlockInvoke()
which marshals the work onto the replay thread and is safe to call from the
HTTP server's worker threads (the GIL is released while waiting).
"""
import base64
import json
import os

import renderdoc as rd

_ctx = None  # qrenderdoc.CaptureContext, injected by __init__


def set_context(ctx):
    global _ctx
    _ctx = ctx


class ToolError(Exception):
    """Raised by tools to report a clean, agent-readable error."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _text_result(obj):
    if isinstance(obj, str):
        text = obj
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=1, default=str)
    # cap gigantic results, agents can narrow with parameters
    if len(text) > 400000:
        text = text[:400000] + "\n... (truncated, narrow your query)"
    return [{"type": "text", "text": text}]


def _require_capture():
    if _ctx is None:
        raise ToolError("Extension not initialized yet.")
    try:
        loaded = bool(_ctx.IsCaptureLoaded())
    except Exception:
        loaded = False
    if not loaded:
        raise ToolError("No capture is loaded in this RenderDoc instance. "
                        "Open a capture first, then retry.")


def _rid_from(value):
    """Accept int / '0x..' / '123' / 'ResourceId(123)' -> renderdoc.ResourceId."""
    if value is None:
        return rd.ResourceId()
    if isinstance(value, rd.ResourceId):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.lower() == "null" or s == "":
            return rd.ResourceId()
        if s.startswith("ResourceId"):
            s = s[s.index("(") + 1:s.rindex(")")]
        value = int(s, 16) if s.lower().startswith("0x") else int(s)
    rid = rd.ResourceId()
    rid.id = int(value)
    return rid


def _replay_invoke(job):
    """Run job(ctrl) on the replay thread. Exceptions raised inside a
    BlockInvoke callback are swallowed by the hosted-python marshalling, so
    capture and re-raise them here."""
    err = []

    def wrapper(ctrl):
        try:
            job(ctrl)
        except BaseException as e:  # noqa: re-raised below
            err.append(e)

    _ctx.Replay().BlockInvoke(wrapper)
    if err:
        raise err[0]


def _names(ctrl):
    """Map 'ResourceId(N)' -> resource name."""
    out = {}
    try:
        for r in ctrl.GetResources():
            nm = r.customName if r.customName else r.name
            out[str(r.resourceId)] = nm
    except Exception:
        pass
    return out


def _fmt(value, depth=0):
    """Best-effort conversion of renderdoc SWIG structs into JSON-able data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return "<%d bytes>" % len(value)
    name = type(value).__name__
    if isinstance(value, (list, tuple)) or name.startswith("rdcarray"):
        if depth > 6:
            return "<list len=%d>" % len(value)
        return [_fmt(v, depth + 1) for v in value]
    if depth > 4:
        return name
    out = {}
    for attr in dir(value):
        if attr.startswith("_") or attr in ("this", "thisown"):
            continue
        try:
            v = getattr(value, attr)
        except Exception:
            continue
        if callable(v):
            continue
        try:
            out[attr] = _fmt(v, depth + 1)
        except Exception:
            out[attr] = "<unprintable>"
    if not out:
        return str(value)
    return out


_STAGE_NAMES = {
    "vertex": rd.ShaderStage.Vertex, "vs": rd.ShaderStage.Vertex,
    "hull": rd.ShaderStage.Hull, "tesscontrol": rd.ShaderStage.Hull,
    "domain": rd.ShaderStage.Domain, "tesseval": rd.ShaderStage.Domain,
    "geometry": rd.ShaderStage.Geometry, "gs": rd.ShaderStage.Geometry,
    "pixel": rd.ShaderStage.Pixel, "fragment": rd.ShaderStage.Pixel,
    "ps": rd.ShaderStage.Pixel, "fs": rd.ShaderStage.Pixel,
    "compute": rd.ShaderStage.Compute, "cs": rd.ShaderStage.Compute,
}


def _parse_stage(value):
    if value is None or str(value).strip() == "":
        return rd.ShaderStage.Pixel
    key = str(value).strip().lower()
    if key in _STAGE_NAMES:
        return _STAGE_NAMES[key]
    raise ToolError("Unknown shader stage %r. Valid: %s" %
                    (value, ", ".join(sorted(_STAGE_NAMES.keys()))))


def _find_action(actions, eid):
    for a in actions:
        if a.eventId == eid:
            return a
        found = _find_action(a.children, eid)
        if found is not None:
            return found
    return None


def _action_summary(a):
    out = {"event_id": a.eventId, "flags": str(a.flags)}
    name = a.customName
    out["name"] = name if name else "EID %d" % a.eventId
    try:
        if a.flags & rd.ActionFlags.Drawcall:
            out["numIndices"] = a.numIndices
            out["numInstances"] = a.numInstances
    except Exception:
        pass
    try:
        if a.flags & rd.ActionFlags.Dispatch:
            out["dispatchDimension"] = list(a.dispatchDimension)
    except Exception:
        pass
    return out


def _texture_info_map(ctrl):  # noqa: kept for reference
    return {}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def get_capture_info(args):
    _require_capture()
    out = {}
    try:
        out["capture_filename"] = str(_ctx.GetCaptureFilename())
    except Exception:
        pass
    try:
        out["current_event"] = int(_ctx.CurEvent())
    except Exception:
        pass

    def work(ctrl):
        api = ctrl.GetAPIProperties()
        out["graphics_api"] = str(api.pipelineType)
        frame = ctrl.GetFrameInfo()
        out["frame_number"] = frame.frameNumber
        texs = ctrl.GetTextures()
        bufs = ctrl.GetBuffers()
        out["num_textures"] = len(texs)
        out["num_buffers"] = len(bufs)
        msgs = ctrl.GetDebugMessages()
        out["num_api_debug_messages"] = len(msgs)
        if msgs:
            out["api_debug_messages"] = [_fmt(m) for m in msgs[:50]]

    _replay_invoke(work)
    return _text_result(out)


def list_drawcalls(args):
    _require_capture()
    include_markers = bool(args.get("include_markers", False))
    max_results = int(args.get("max_results", 2000))
    results = []
    truncated = [False]

    def work(ctrl):
        def walk(actions):
            for a in actions:
                is_marker = bool(a.flags & (rd.ActionFlags.PushMarker |
                                            rd.ActionFlags.PopMarker |
                                            rd.ActionFlags.MultiAction))
                if include_markers or not is_marker:
                    if len(results) >= max_results:
                        truncated[0] = True
                        return
                    results.append(_action_summary(a))
                walk(a.children)

        walk(ctrl.GetRootActions())

    _replay_invoke(work)
    payload = {"count": len(results), "drawcalls": results}
    if truncated[0]:
        payload["note"] = ("Output truncated at max_results=%d. Use marker "
                           "regions or filter in code." % max_results)
    return _text_result(payload)


def get_drawcall_params(args):
    _require_capture()
    eid = int(args["event_id"])
    out = {"event_id": eid}

    def work(ctrl):
        ctrl.SetFrameEvent(eid, True)
        names = _names(ctrl)
        out["resource_names"] = names
        action = _find_action(ctrl.GetRootActions(), eid)
        if action is not None:
            out["action"] = _action_summary(action)
        pipe = ctrl.GetPipelineState()
        outs = []
        try:
            for i, o in enumerate(pipe.GetOutputTargets()):
                if o.resource != rd.ResourceId():
                    outs.append({"index": i, "resource": str(o.resource),
                                 "name": names.get(str(o.resource), "?")})
        except Exception:
            pass
        out["output_targets"] = outs
        try:
            dt = pipe.GetDepthTarget()
            if dt.resource != rd.ResourceId():
                out["depth_target"] = {"resource": str(dt.resource),
                                       "name": names.get(str(dt.resource), "?")}
        except Exception:
            pass
        try:
            out["pipeline_graphics"] = str(pipe.GetGraphicsPipelineObject())
        except Exception:
            pass
        try:
            out["pipeline_compute"] = str(pipe.GetComputePipelineObject())
        except Exception:
            pass
        try:
            out["topology"] = str(pipe.GetPrimitiveTopology())
        except Exception:
            pass
        try:
            vp = pipe.GetViewport(0)
            out["viewport0"] = _fmt(vp)
        except Exception:
            pass
        shaders = {}
        for stage in rd.ShaderStage:
            try:
                sid = pipe.GetShader(stage)
            except Exception:
                continue
            if sid == rd.ResourceId():
                continue
            entry = {"shader": str(sid),
                     "name": names.get(str(sid), "?")}
            try:
                entry["entry_point"] = str(pipe.GetShaderEntryPoint(stage))
            except Exception:
                pass
            try:
                refl = pipe.GetShaderReflection(stage)
                if refl is not None:
                    entry["reflection"] = _refl_summary(refl)
            except Exception:
                pass
            shaders[str(stage)] = entry
        out["shaders"] = shaders
        descs = []
        try:
            for d in pipe.GetAllUsedDescriptors(True):
                res = str(d.descriptor.resource)
                descs.append({"stage": str(d.access.stage),
                              "type": str(d.access.type),
                              "index": d.access.index,
                              "readWrite": bool(d.access.readWrite),
                              "resource": res,
                              "name": names.get(res, "?")})
        except Exception:
            pass
        out["descriptors"] = descs
        msgs = ctrl.GetDebugMessages()
        if msgs:
            out["api_debug_messages"] = [_fmt(m) for m in msgs[:50]]

    _replay_invoke(work)
    return _text_result(out)


def _refl_summary(refl):
    out = {"entry_point": str(refl.entryPoint)}
    out["inputs"] = [{"name": v.name, "regIndex": v.regIndex,
                      "systemValue": str(v.systemValue),
                      "type": str(v.type)}
                     for v in refl.inputSignature]
    out["outputs"] = [{"name": v.name, "regIndex": v.regIndex,
                       "systemValue": str(v.systemValue),
                       "type": str(v.type)}
                      for v in refl.outputSignature]
    out["constantBlocks"] = [{"name": cb.name, "byteSize": cb.byteSize}
                             for cb in refl.constantBlocks]
    out["samplers"] = [v.name for v in refl.samplers]
    out["readOnlyResources"] = [v.name for v in refl.readOnlyResources]
    out["readWriteResources"] = [v.name for v in refl.readWriteResources]
    return out


def get_shader_disassembly(args):
    _require_capture()
    eid = int(args["event_id"])
    stage = _parse_stage(args.get("stage"))
    disasm = {}

    def work(ctrl):
        ctrl.SetFrameEvent(eid, True)
        pipe = ctrl.GetPipelineState()
        sid = pipe.GetShader(stage)
        if sid == rd.ResourceId():
            raise ToolError("No shader bound at stage %s for event %d."
                            % (stage, eid))
        targets = ctrl.GetDisassemblyTargets(True)
        disasm["targets"] = [str(t) for t in targets]
        target = args.get("target")
        if target:
            want = str(target)
            match = [t for t in targets if str(t) == want]
            if not match:
                raise ToolError("Disassembly target %r not available. "
                                "Available: %s" % (want, disasm["targets"]))
            target = match[0]
        else:
            target = targets[0] if targets else ""
        pipeline = pipe.GetGraphicsPipelineObject()
        if pipeline == rd.ResourceId():
            pipeline = pipe.GetComputePipelineObject()
        refl = pipe.GetShaderReflection(stage)
        disasm["stage"] = str(stage)
        disasm["target"] = str(target)
        disasm["disassembly"] = str(ctrl.DisassembleShader(pipeline, refl, target))

    _replay_invoke(work)
    return _text_result(disasm)


def get_screenshot(args):
    _require_capture()
    eid = args.get("event_id")
    tex_id = args.get("texture_id")
    output_index = int(args.get("output_index", 0))
    chosen = {"source": None}

    path = os.path.join(os.environ.get("TEMP", os.getcwd()),
                        "renderdoc_mcp_shot.png")

    def work(ctrl):
        names = _names(ctrl)
        rid = None
        if tex_id is not None:
            rid = _rid_from(tex_id)
            chosen["source"] = "texture %s (%s)" % (
                str(rid), names.get(str(rid), "?"))
        else:
            if eid is not None:
                ctrl.SetFrameEvent(int(eid), True)
            else:
                try:
                    ctrl.SetFrameEvent(int(_ctx.CurEvent()), True)
                except Exception:
                    pass
            pipe = ctrl.GetPipelineState()
            outs = []
            try:
                outs = [o for o in pipe.GetOutputTargets()
                        if o.resource != rd.ResourceId()]
            except Exception:
                pass
            if len(outs) > output_index:
                rid = outs[output_index].resource
                chosen["source"] = "output %d at current event" % output_index
            else:
                try:
                    dt = pipe.GetDepthTarget()
                    if dt.resource != rd.ResourceId():
                        rid = dt.resource
                        chosen["source"] = "depth target at current event"
                except Exception:
                    pass
            if rid is None:
                # fall back to the swapchain backbuffer(s)
                try:
                    for t in ctrl.GetTextures():
                        if (int(t.creationFlags) &
                                int(rd.TextureCategory.SwapBuffer)):
                            rid = t.resourceId
                            chosen["source"] = "swapchain backbuffer"
                            break
                except Exception:
                    pass
            if rid is not None:
                chosen["source"] += " (%s)" % names.get(str(rid), "?")
        if rid is None:
            raise ToolError(
                "No bound output target found. Open the event you want in the "
                "UI, or pass an explicit 'texture_id' "
                "(see get_texture_list).")
        chosen["resource"] = str(rid)
        chosen["name"] = names.get(str(rid), "?")
        for t in ctrl.GetTextures():
            if t.resourceId == rid:
                chosen["width"] = t.width
                chosen["height"] = t.height
                try:
                    chosen["format"] = t.format.Name()
                except Exception:
                    chosen["format"] = "id %d" % int(t.format)
                break
        save = rd.TextureSave()
        save.resourceId = rid
        save.destType = rd.FileType.PNG
        save.mip = 0
        save.slice.sliceIndex = 0
        save.alpha = rd.AlphaMapping.Preserve
        result = ctrl.SaveTexture(save, path)
        chosen["save_result"] = str(result.code)
        if result.code != rd.ResultCode.Succeeded:
            raise ToolError("SaveTexture failed: %s (%s)" %
                            (str(result.code), str(result)))

    _replay_invoke(work)

    chosen["save_result"] = str(chosen.get("save_result", ""))
    if not os.path.exists(path):
        # retry with forward slashes (some layers mangle backslash paths)
        alt = path.replace("\\", "/")
        if os.path.exists(alt):
            path = alt
        else:
            raise ToolError(
                "SaveTexture reported success but produced no file at %r "
                "(cwd=%r, temp exists=%s, result=%s)"
                % (path, os.getcwd(),
                   os.path.isdir(os.path.dirname(path)), chosen))
    with open(path, "rb") as f:
        data = f.read()
    try:
        os.remove(path)
    except OSError:
        pass
    if not data:
        raise ToolError("SaveTexture produced an empty file.")
    text = ("Screenshot of %s: %s (%sx%s %s)" %
            (chosen.get("source", "?"), chosen.get("name", "?"),
             chosen.get("width", "?"), chosen.get("height", "?"),
             chosen.get("format", "?")))
    return [{"type": "text", "text": text},
            {"type": "image", "data": base64.b64encode(data).decode("ascii"),
             "mimeType": "image/png"}]


def get_pixel_history(args):
    _require_capture()
    rid = _rid_from(args["texture_id"])
    x = int(args["x"])
    y = int(args["y"])
    mip = int(args.get("mip", 0))
    slice_ = int(args.get("slice", 0))
    eid = args.get("event_id")
    hist = {"texture": str(rid), "x": x, "y": y, "mip": mip, "slice": slice_}
    mods = []

    def work(ctrl):
        if eid is not None:
            ctrl.SetFrameEvent(int(eid), True)
        hist["name"] = _names(ctrl).get(str(rid), "?")
        sub = rd.Subresource()
        sub.mip = mip
        sub.slice = slice_
        results = ctrl.PixelHistory(rid, x, y, sub, rd.CompType.Typeless)
        for r in results:
            rejects = [flag for flag, val in (
                ("sampleMasked", r.sampleMasked),
                ("backfaceCulled", r.backfaceCulled),
                ("depthClipped", r.depthClipped),
                ("depthBoundsFailed", r.depthBoundsFailed),
                ("viewClipped", r.viewClipped),
                ("scissorClipped", r.scissorClipped),
                ("shaderDiscarded", r.shaderDiscarded),
                ("depthTestFailed", r.depthTestFailed),
                ("stencilTestFailed", r.stencilTestFailed),
                ("predicationSkipped", r.predicationSkipped)) if val]
            entry = {"event_id": r.eventId,
                     "frag_index": r.fragIndex,
                     "primitive": r.primitiveID,
                     "passed": bool(r.Passed()),
                     "unboundPS": bool(r.unboundPS),
                     "directShaderWrite": bool(r.directShaderWrite),
                     "rejected_by": rejects,
                     "preMod": _fmt(r.preMod, 3),
                     "shaderOut": _fmt(r.shaderOut, 3),
                     "postMod": _fmt(r.postMod, 3)}
            mods.append(entry)

    _replay_invoke(work)
    hist["num_modifications"] = len(mods)
    hist["modifications"] = mods
    return _text_result(hist)


def get_texture_list(args):
    _require_capture()
    max_results = int(args.get("max_results", 500))
    name_filter = str(args.get("name_filter", "")).lower()
    texs = []

    def work(ctrl):
        names = _names(ctrl)
        for t in ctrl.GetTextures():
            nm = names.get(str(t.resourceId), "")
            if name_filter and name_filter not in nm.lower():
                continue
            texs.append({"resource_id": str(t.resourceId), "name": nm,
                         "width": t.width, "height": t.height,
                         "depth": t.depth, "format": str(t.format),
                         "mips": t.mips, "arraysize": t.arraysize})
            if len(texs) >= max_results:
                break

    _replay_invoke(work)
    return _text_result({"count": len(texs), "textures": texs})


def get_buffer_list(args):
    _require_capture()
    max_results = int(args.get("max_results", 500))
    name_filter = str(args.get("name_filter", "")).lower()
    bufs = []

    def work(ctrl):
        names = _names(ctrl)
        for b in ctrl.GetBuffers():
            nm = names.get(str(b.resourceId), "")
            if name_filter and name_filter not in nm.lower():
                continue
            bufs.append({"resource_id": str(b.resourceId), "name": nm,
                         "length": b.length})
            if len(bufs) >= max_results:
                break

    _replay_invoke(work)
    return _text_result({"count": len(bufs), "buffers": bufs})


def _hexdump(data, max_rows=48):
    lines = []
    total_rows = (len(data) + 15) // 16
    for row in range(min(total_rows, max_rows)):
        chunk = data[row * 16:(row + 1) * 16]
        hexs = " ".join("%02x" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append("%08x  %-47s  |%s|" % (row * 16, hexs, asc))
    if total_rows > max_rows:
        lines.append("... (%d more bytes not shown)" %
                     (len(data) - max_rows * 16))
    return "\n".join(lines)


def get_buffer_data(args):
    _require_capture()
    rid = _rid_from(args["buffer_id"])
    offset = int(args.get("offset", 0))
    length = min(int(args.get("length", 1024)), 65536)
    out = {"buffer": str(rid), "offset": offset, "requested_length": length}

    def work(ctrl):
        out["name"] = _names(ctrl).get(str(rid), "?")
        data = ctrl.GetBufferData(rid, offset, length)
        out["returned_length"] = len(data)
        out["hexdump"] = _hexdump(bytes(data))
        out["raw_base64"] = base64.b64encode(bytes(data)).decode("ascii") \
            if len(data) <= 4096 else ""

    _replay_invoke(work)
    return _text_result(out)


def get_event_usage(args):
    _require_capture()
    rid = _rid_from(args["resource_id"])
    usage = []

    def work(ctrl):
        usage.append({"name": _names(ctrl).get(str(rid), "?"),
                      "resource": str(rid),
                      "usages": [{"event_id": u.eventId, "usage": str(u.usage)}
                                 for u in ctrl.GetUsage(rid)]})

    _replay_invoke(work)
    return _text_result(usage[0])


def get_debug_messages(args):
    _require_capture()
    msgs = []

    def work(ctrl):
        for m in ctrl.GetDebugMessages():
            msgs.append({"event_id": m.eventId, "severity": str(m.severity),
                         "source": str(m.source),
                         "message": str(m.description)})

    _replay_invoke(work)
    return _text_result({"count": len(msgs), "messages": msgs})


def get_current_state(args):
    out = {}
    if _ctx is not None:
        try:
            out["capture_loaded"] = bool(_ctx.IsCaptureLoaded())
        except Exception:
            out["capture_loaded"] = False
        try:
            out["current_event"] = int(_ctx.CurEvent())
        except Exception:
            pass
        try:
            out["capture_filename"] = str(_ctx.GetCaptureFilename())
        except Exception:
            pass
    if not out.get("capture_loaded"):
        raise ToolError("No capture is loaded in this RenderDoc instance.")
    return _text_result(out)


# ---------------------------------------------------------------------------
# capture control: launch a program and trigger captures. qrenderdoc must own
# the launch so its in-app capture hooks are installed (plain injection is
# NOT enough to get a live connection + timed capture).
# ---------------------------------------------------------------------------

_conn = None  # qrenderdoc.CaptureConnection from the last launch_app


def _settings_file(path=None):
    if path:
        return path
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "qrenderdoc", "most_recent.cap")


def _clear_conn(*a):
    global _conn
    _conn = None


def launch_app(args):
    """Exactly equivalent to clicking 'Launch' in the Launch Application
    window: launches whatever the dialog currently shows. If the dialog is
    still empty, first load qrenderdoc's last-used settings (most_recent.cap)
    — the same as clicking the top entry in the dialog's recent settings
    list. No fields are modified beyond that."""
    global _conn
    if _ctx is None:
        raise ToolError("Extension not initialized yet.")
    settings_file = args.get("settings_file")
    dialog = _ctx.GetCaptureDialog()
    s = dialog.Settings()
    if settings_file or not str(s.executable):
        settings_file = settings_file or _settings_file()
        if not os.path.isfile(settings_file):
            raise ToolError("Settings file not found: %s" % settings_file)
        dialog.LoadSettings(settings_file)
        s = dialog.Settings()
    info = {"settings_file": settings_file,
            "exe": str(s.executable),
            "working_dir": str(s.workingDir),
            "cmdline": str(s.commandLine)}
    # note: an empty working_dir is what the UI sends too; RenderDoc itself
    # defaults it to the executable's directory (win32_process.cpp)
    conn = dialog.Launch()
    if conn is None:
        raise ToolError("Launch failed: RenderDoc reported no connection "
                        "was made (check the Launch Application window).")
    try:
        conn.RegisterClosedCallback(_clear_conn)
    except Exception:
        pass
    _conn = conn
    try:
        info["connected"] = bool(conn.IsConnected())
    except Exception:
        info["connected"] = None
    return _text_result(info)


def trigger_capture(args):
    """Ask the connected program to capture a frame after a delay."""
    if _conn is None:
        raise ToolError("No active capture connection. Call launch_app first.")
    delay = float(args.get("delay_seconds", 0) or 0)
    num = int(args.get("num_frames", 1) or 1)
    _conn.TimedCapture(delay, num)
    return _text_result({"queued": True, "delay_seconds": delay,
                         "num_frames": num,
                         "note": "poll list_captures until the capture appears"})


def list_captures(args):
    """List captures made on the active capture connection."""
    if _conn is None:
        raise ToolError("No active capture connection. Call launch_app first.")
    out = {"connected": bool(_conn.IsConnected())}
    try:
        out["apis"] = [str(a) for a in _conn.GetAPIs()]
    except Exception:
        pass
    caps = []
    for c in _conn.GetCaptures():
        caps.append({"capture_id": int(c.captureID),
                     "frame_number": int(c.frameNumber),
                     "api": str(c.api),
                     "timestamp": str(c.timestamp)})
    out["captures"] = caps
    return _text_result(out)


def open_capture(args):
    """Open a captured frame from the connection into the RenderDoc UI."""
    if _conn is None:
        raise ToolError("No active capture connection. Call launch_app first.")
    cid = int(args["capture_id"])
    _conn.OpenCapture(cid)
    return _text_result({"opened": cid,
                         "note": "capture is loading into the UI; poll "
                                 "get_current_state until capture_loaded"})


# ---------------------------------------------------------------------------
# registry (name -> definition) — consumed by mcp_server
# ---------------------------------------------------------------------------

def _def(description, schema, call):
    return {"description": description, "schema": schema, "call": call}


TOOLS = {
    "get_capture_info": _def(
        "Get general info about the capture currently open in this RenderDoc "
        "UI instance: file path, graphics API, frame number, resource counts "
        "and any API debug/validation messages.",
        {"type": "object", "properties": {}},
        get_capture_info),

    "get_current_state": _def(
        "Get the current UI state: whether a capture is loaded and which "
        "event is currently selected.",
        {"type": "object", "properties": {}},
        get_current_state),

    "list_drawcalls": _def(
        "List the drawcall/dispatch/copy action tree of the open capture as a "
        "flat list. Use an entry's event_id with get_drawcall_params for full "
        "details.",
        {"type": "object", "properties": {
            "include_markers": {"type": "boolean",
                                "description": "Include marker/debug regions.",
                                "default": False},
            "max_results": {"type": "integer", "default": 2000}}},
        list_drawcalls),

    "get_drawcall_params": _def(
        "Get the full pipeline state at a given drawcall/dispatch: output "
        "targets, depth target, pipeline objects, bound shaders with "
        "reflection (inputs/outputs/constant blocks), all used descriptor "
        "bindings, viewport and API debug messages. Note: this moves the "
        "RenderDoc UI to that event.",
        {"type": "object", "properties": {
            "event_id": {"type": "integer",
                         "description": "EID from list_drawcalls"}},
         "required": ["event_id"]},
        get_drawcall_params),

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
            "output_index": {"type": "integer", "default": 0}}},
        get_screenshot),

    "get_pixel_history": _def(
        "Get the full modification history of one pixel of a texture across "
        "the frame: which events wrote it, with which color/depth values and "
        "shader outputs. Essential for diagnosing 'why is this pixel wrong'.",
        {"type": "object", "properties": {
            "texture_id": {"type": "string"},
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "mip": {"type": "integer", "default": 0},
            "slice": {"type": "integer", "default": 0},
            "event_id": {"type": "integer"}},
         "required": ["texture_id", "x", "y"]},
        get_pixel_history),

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
         "required": ["event_id"]},
        get_shader_disassembly),

    "get_texture_list": _def(
        "List textures/resources in the capture with id, name, dimensions "
        "and format.",
        {"type": "object", "properties": {
            "max_results": {"type": "integer", "default": 500},
            "name_filter": {"type": "string"}}},
        get_texture_list),

    "get_buffer_list": _def(
        "List buffers in the capture with id, name and length.",
        {"type": "object", "properties": {
            "max_results": {"type": "integer", "default": 500},
            "name_filter": {"type": "string"}}},
        get_buffer_list),

    "get_buffer_data": _def(
        "Read raw bytes of a buffer as a hexdump (plus base64 if <= 4096 "
        "bytes).",
        {"type": "object", "properties": {
            "buffer_id": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "length": {"type": "integer", "default": 1024}},
         "required": ["buffer_id"]},
        get_buffer_data),

    "get_event_usage": _def(
        "Get every event where a given resource is used (read/written), for "
        "tracing resource lifetime through the frame.",
        {"type": "object", "properties": {
            "resource_id": {"type": "string"}},
         "required": ["resource_id"]},
        get_event_usage),

    "get_debug_messages": _def(
        "Get all API debug/validation messages emitted by the capture's "
        "debug layer.",
        {"type": "object", "properties": {}},
        get_debug_messages),

    "launch_app": _def(
        "Exactly equivalent to clicking 'Launch' in the Launch Application "
        "window: launches the executable the dialog currently shows, with "
        "its working directory and command line. If the dialog is empty, "
        "qrenderdoc's last-used settings (most_recent.cap) are loaded first "
        "— same as clicking the top entry of the recent settings list. Use "
        "trigger_capture afterwards to capture frames.",
        {"type": "object", "properties": {
            "settings_file": {"type": "string",
                              "description": "Optional .cap settings file to "
                                             "load before launching"}}},
        launch_app),

    "trigger_capture": _def(
        "Ask the program launched by launch_app to capture a frame after "
        "delay_seconds (0 = immediately).",
        {"type": "object", "properties": {
            "delay_seconds": {"type": "number", "default": 0},
            "num_frames": {"type": "integer", "default": 1,
                           "description": "How many frames to capture "
                                          "starting at the delayed one."}}},
        trigger_capture),

    "list_captures": _def(
        "List the captures made so far on the connection launched by "
        "launch_app, with their capture_id for open_capture.",
        {"type": "object", "properties": {}},
        list_captures),

    "open_capture": _def(
        "Open a captured frame (by capture_id from list_captures) into the "
        "RenderDoc UI so the analysis tools can inspect it.",
        {"type": "object", "properties": {
            "capture_id": {"type": "integer"}},
         "required": ["capture_id"]},
        open_capture),
}
