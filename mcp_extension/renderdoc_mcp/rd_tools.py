"""Tool implementations for the RenderDoc MCP server.

Every tool here runs *inside one RenderDoc instance*; the broker routes
calls to it (see broker.py / spoke.py). All replay-dependent access goes
through pyrenderdoc.Replay().BlockInvoke() which marshals the work onto the
replay thread and is safe to call from broker/socket worker threads (the
GIL is released while waiting).

TOOL_IMPL at the bottom maps tool name -> implementation, matching the
descriptions/schemas in tool_defs.py (shared with the standalone broker).
"""
import base64
import json
import os
import sys

import renderdoc as rd

_ctx = None          # qrenderdoc.CaptureContext, injected by set_context()
_instance_id = None  # broker-assigned id ("rd1", ...), via set_instance_id()


def set_context(ctx):
    global _ctx
    _ctx = ctx


def set_instance_id(iid):
    global _instance_id
    _instance_id = iid


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
    if _instance_id:
        out["instance"] = _instance_id
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
    if _instance_id:
        payload["instance"] = _instance_id
    if truncated[0]:
        payload["note"] = ("Output truncated at max_results=%d. Use marker "
                           "regions or filter in code." % max_results)
    return _text_result(payload)


def get_drawcall_params(args):
    _require_capture()
    eid = int(args["event_id"])
    out = {"event_id": eid}
    if _instance_id:
        out["instance"] = _instance_id

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
    if _instance_id:
        disasm["instance"] = _instance_id

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
    if _instance_id:
        chosen["instance"] = _instance_id

    path = os.path.join(os.environ.get("TEMP", os.getcwd()),
                        "renderdoc_mcp_shot_%d.png" % os.getpid())

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
    if _instance_id:
        hist["instance"] = _instance_id
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
    out = current_status()
    if not out.get("capture_loaded"):
        out["note"] = ("No capture is loaded. Use launch_app + "
                       "trigger_capture + list_captures + open_capture, or "
                       "open a capture in the UI.")
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
    """Launch a program under capture control. With exe/working_dir/cmdline
    the values are typed into the Launch Application dialog (Settings copy +
    SetSettings, exactly what the UI does on edit); without them the
    dialog's current settings are used, loading most_recent.cap if empty -
    equivalent to clicking Launch in both cases."""
    global _conn
    if _ctx is None:
        raise ToolError("Extension not initialized yet.")
    exe = args.get("exe")
    working_dir = args.get("working_dir")
    cmdline = args.get("cmdline")
    settings_file = args.get("settings_file")

    if exe and not os.path.isfile(exe):
        # pre-check here: the dialog would show a *modal* error box instead
        raise ToolError("Executable not found: %s" % exe)
    if working_dir and not os.path.isdir(working_dir):
        raise ToolError("Working directory not found: %s" % working_dir)

    dialog = _ctx.GetCaptureDialog()
    if exe or working_dir is not None or cmdline is not None:
        s = dialog.Settings()  # a copy of the dialog's current settings
        if exe:
            s.executable = exe
        if working_dir is not None:
            s.workingDir = working_dir
        if cmdline is not None:
            s.commandLine = cmdline
        dialog.SetSettings(s)  # pushes back + updates the dialog UI fields
        s = dialog.Settings()
    else:
        s = dialog.Settings()
        if settings_file or not str(s.executable):
            settings_file = settings_file or _settings_file()
            if not os.path.isfile(settings_file):
                raise ToolError("Settings file not found: %s" % settings_file)
            dialog.LoadSettings(settings_file)
            s = dialog.Settings()
    info = {"instance": _instance_id,
            "settings_file": settings_file,
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
    return _text_result({"instance": _instance_id, "queued": True,
                         "delay_seconds": delay, "num_frames": num,
                         "note": "poll list_captures until the capture appears"})


def list_captures(args):
    """List captures made on the active capture connection."""
    if _conn is None:
        raise ToolError("No active capture connection. Call launch_app first.")
    out = {"instance": _instance_id,
           "connected": bool(_conn.IsConnected())}
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
    return _text_result({"instance": _instance_id, "opened": cid,
                         "note": "capture is loading into the UI; poll "
                                 "get_current_state until capture_loaded"})


# ---------------------------------------------------------------------------
# registry: tool name -> implementation (descriptions/schemas live in
# tool_defs.py; the broker routes 'instance'-scoped tools here via spoke.py)
# ---------------------------------------------------------------------------

TOOL_IMPL = {
    "get_capture_info": get_capture_info,
    "get_current_state": get_current_state,
    "list_drawcalls": list_drawcalls,
    "get_drawcall_params": get_drawcall_params,
    "get_screenshot": get_screenshot,
    "get_pixel_history": get_pixel_history,
    "get_shader_disassembly": get_shader_disassembly,
    "get_texture_list": get_texture_list,
    "get_buffer_list": get_buffer_list,
    "get_buffer_data": get_buffer_data,
    "get_event_usage": get_event_usage,
    "get_debug_messages": get_debug_messages,
    "launch_app": launch_app,
    "trigger_capture": trigger_capture,
    "list_captures": list_captures,
    "open_capture": open_capture,
}
