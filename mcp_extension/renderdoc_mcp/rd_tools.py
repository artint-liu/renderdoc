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
import ctypes
import json
import os
import sys
import time

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

def _temp_capture_dir():
    """Where RenderDoc writes captures made outside launch_app (hotkey
    captures of manually-started programs): %TEMP%\\RenderDoc."""
    return os.path.join(os.environ.get("TEMP", os.getcwd()), "RenderDoc")


_NO_CONN_HINT = (
    "No active capture connection. Call launch_app first. If the frames "
    "were captured manually (hotkey captures of a program started outside "
    "this extension), the .rdc files are written to %s - enumerate them "
    "with list_capture_files and load one with open_capture_file(path) "
    "(or start_renderdoc with args=\"<path.rdc>\")."
    % _temp_capture_dir())


def _frame_number_from_filename(path):
    """Parse the frame number out of '<app>_<ts>_frame<N>.rdc' (or
    '<...>_capture.rdc' -> None). Returns None when not parseable."""
    base = os.path.basename(str(path))
    if base.lower().endswith(".rdc"):
        base = base[:-4]
    idx = base.rfind("_frame")
    if idx >= 0 and base[idx + 6:].isdigit():
        return int(base[idx + 6:])
    return None


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
                        "Open a capture first, then retry - or load one "
                        "from disk with open_capture_file(path) (see "
                        "list_capture_files).")


def _rid_from(value):
    """Accept int / '0x..' / '123' / 'ResourceId(123)' -> numeric id (or None).

    NOTE: qrenderdoc's python API cannot construct a ResourceId directly
    (docs/python_api/in_depth/resourceids.rst). Use _resolve_rid(ctrl, value)
    inside a replay job to turn this number into a real ResourceId by
    matching against the capture's texture/buffer lists.
    """
    if value is None:
        return None
    if isinstance(value, rd.ResourceId):
        return value
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.lower() == "null" or s == "":
        return None
    if s.startswith("ResourceId"):
        s = s[s.index("(") + 1:s.rindex(")")]
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def _resolve_rid(ctrl, value):
    """Resolve a user-supplied resource id into a real rd.ResourceId by
    matching its numeric value against the capture's textures and buffers.
    Raises ToolError when no resource matches."""
    if value is None:
        return rd.ResourceId()
    if isinstance(value, rd.ResourceId):
        return value
    num = _rid_from(value)
    if num is None:
        return rd.ResourceId()
    try:
        for t in ctrl.GetTextures():
            if int(t.resourceId) == num:
                return t.resourceId
    except Exception:
        pass
    try:
        for b in ctrl.GetBuffers():
            if int(b.resourceId) == num:
                return b.resourceId
    except Exception:
        pass
    raise ToolError("No texture/buffer with id %s exists in this capture "
                    "(ids are per-capture; see get_texture_list / "
                    "get_buffer_list for valid ids)." % num)


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


_COMP_NAMES = {
    "Float": "F", "UNorm": "UNORM", "SNorm": "SNORM", "UInt": "UINT",
    "SInt": "SINT", "UScaled": "USCALED", "SScaled": "SSCALED",
    "Depth": "DEPTH", "UNormSRGB": "UNORMSRGB", "Typeless": "TYPELESS",
}
_COUNT_NAMES = {1: "R", 2: "RG", 3: "RGB", 4: "RGBA"}


def _format_raw(fmt):
    """Structured data of a ResourceFormat (fileType/compType/compCount/
    compByteWidth); individual fields degrade to None + <attr>_err."""
    out = {}
    for attr in ("fileType", "compType", "compCount", "compByteWidth"):
        try:
            v = getattr(fmt, attr)
            out[attr] = int(v) if attr in ("compCount", "compByteWidth") \
                else str(v)
        except Exception as e:
            out[attr] = None
            out[attr + "_err"] = str(e)
    return out


def _format_readable(fmt):
    """Human-readable format string: Name() when available, else a manual
    'F32_RGBA' / 'UNORM8_RGBA' style string built from the struct fields."""
    try:
        n = fmt.Name()
        if n:
            return str(n)
    except Exception:
        pass
    raw = _format_raw(fmt)
    ct = _COMP_NAMES.get(raw.get("compType") or "", raw.get("compType"))
    width = raw.get("compByteWidth")
    if ct == "F" and width:
        ct = "F32" if width == 4 else ("F16" if width == 2
                                       else "F%d" % (width * 8))
    elif ct and width and ct in ("UNORM", "SNORM", "UINT", "SINT",
                                 "UNORMSRGB"):
        ct = "%s%d" % (ct, width * 8)
    suffix = _COUNT_NAMES.get(raw.get("compCount"),
                              "x%s" % raw.get("compCount"))
    return "%s_%s" % (ct, suffix)


def _f6(v):
    try:
        return round(float(v), 6)
    except Exception:
        return None


def _f6v(values, count=4):
    try:
        return [_f6(v) for v in list(values)[:count]]
    except Exception:
        return [None] * count


def _mv_px(mv):
    """ModificationValue -> PixelValue. Newer RenderDoc API wraps the color
    in a ``col`` member; the old API had floatValue directly on the struct."""
    col = getattr(mv, "col", None)
    return col if col is not None else mv


def _int_or_none(v):
    try:
        return int(v)
    except Exception:
        return None


def _cfloat(raw4):
    return ctypes.c_float.from_buffer_copy(bytes(raw4)).value


def _half_to_float(raw2):
    h = int.from_bytes(bytes(raw2), "little")
    sign = (h >> 15) & 1
    exp = (h >> 10) & 0x1F
    mant = h & 0x3FF
    if exp == 0:
        val = mant * (2.0 ** -24)
    elif exp == 31:
        val = float("inf") if mant == 0 else float("nan")
    else:
        val = (1.0 + mant / 1024.0) * (2.0 ** (exp - 15))
    return -val if sign else val


def _pixel_decoder(fmt):
    """Return (decode_fn, bytes_per_pixel, raw) or raise ToolError listing
    the format struct. First version supports UNORM8/UNORM16, SNORM8,
    FLOAT16, FLOAT32."""
    raw = _format_raw(fmt)
    ct = raw.get("compType")
    n = raw.get("compCount")
    bw = raw.get("compByteWidth")
    if not isinstance(n, int) or not isinstance(bw, int) or n <= 0 or bw <= 0:
        raise ToolError("Unsupported/unknown resource format: %s "
                        "(format struct: %s)" % (_format_readable(fmt),
                                                 json.dumps(raw)))
    bpp = bw * n
    if ct == "Float" and bw == 4:
        def dec(b):
            return [_cfloat(b[i * 4:(i + 1) * 4]) for i in range(n)]
    elif ct == "Float" and bw == 2:
        def dec(b):
            return [_half_to_float(b[i * 2:(i + 1) * 2]) for i in range(n)]
    elif ct == "UNorm" and bw == 1:
        def dec(b):
            return [b[i] / 255.0 for i in range(n)]
    elif ct == "UNorm" and bw == 2:
        def dec(b):
            return [int.from_bytes(b[i * 2:(i + 1) * 2], "little") / 65535.0
                    for i in range(n)]
    elif ct == "SNorm" and bw == 1:
        def dec(b):
            return [max(int.from_bytes(b[i:i + 1], "little", signed=True),
                        -128) / 127.0 for i in range(n)]
    else:
        raise ToolError("Unsupported resource format %s (compType=%s, "
                        "compCount=%s, compByteWidth=%s). Supported: "
                        "UNORM8, UNORM16, SNORM8, FLOAT16, FLOAT32."
                        % (_format_readable(fmt), ct, n, bw))
    return dec, bpp, raw


def _channel_stats(values):
    n = len(values)
    if n == 0:
        return {"mean": None, "std": None, "min": None, "max": None,
                "num_values": 0}
    mean = sum(values) / float(n)
    var = sum((v - mean) * (v - mean) for v in values) / float(n)
    return {"mean": _f6(mean), "std": _f6(var ** 0.5),
            "min": _f6(min(values)), "max": _f6(max(values)),
            "num_values": n}


def _pixel_value_compact(v):
    """PixelValue -> {floatValue, depth, stencil} only (raw mode of
    get_pixel_history drops intValue/uintValue; UNORM values are recoverable
    from the float ones)."""
    out = {}
    try:
        px = _mv_px(v)
        out["floatValue"] = [_f6(x) for x in list(px.floatValue)[:4]]
    except Exception as e:
        out["floatValue_err"] = str(e)
    try:
        out["depth"] = _f6(v.depth)
    except Exception:
        out["depth"] = None
    try:
        out["stencil"] = int(v.stencil)
    except Exception:
        out["stencil"] = None
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


_ACTION_TYPE_MASK = (
    rd.ActionFlags.Clear | rd.ActionFlags.Drawcall |
    rd.ActionFlags.Dispatch | rd.ActionFlags.MeshDispatch |
    rd.ActionFlags.CmdList | rd.ActionFlags.Present |
    rd.ActionFlags.MultiAction | rd.ActionFlags.Copy |
    rd.ActionFlags.Resolve | rd.ActionFlags.GenMips |
    rd.ActionFlags.DispatchRay | rd.ActionFlags.BuildAccStruct)


def list_drawcalls(args):
    _require_capture()
    include_markers = bool(args.get("include_markers", False))
    actions_only = bool(args.get("actions_only", False))
    flags_include = args.get("flags_include") or []
    if isinstance(flags_include, str):
        flags_include = [flags_include]
    flags_include = [str(f).strip().lower() for f in flags_include
                     if str(f).strip()]
    # max_results 0 = unlimited; the historical default 2000 is kept
    max_results = int(args.get("max_results", 2000))
    results = []
    truncated = [False]
    total_matched = [0]

    def work(ctrl):
        def walk(actions):
            for a in actions:
                is_marker = bool(a.flags & (rd.ActionFlags.PushMarker |
                                            rd.ActionFlags.PopMarker |
                                            rd.ActionFlags.MultiAction))
                if include_markers or not is_marker:
                    ok = True
                    if actions_only and not (a.flags & _ACTION_TYPE_MASK):
                        ok = False
                    if ok and flags_include:
                        fstr = str(a.flags).lower()
                        ok = any(f in fstr for f in flags_include)
                    if ok:
                        total_matched[0] += 1
                        if max_results <= 0 or \
                                len(results) < max_results:
                            results.append(_action_summary(a))
                        else:
                            truncated[0] = True
                walk(a.children)

        walk(ctrl.GetRootActions())

    _replay_invoke(work)
    payload = {"count": len(results), "drawcalls": results}
    if _instance_id:
        payload["instance"] = _instance_id
    if total_matched[0] != len(results):
        payload["total_matched"] = total_matched[0]
    if truncated[0]:
        payload["truncated"] = True
        payload["note"] = ("Output truncated at max_results=%d after "
                           "filtering (total_matched=%d). Narrow with "
                           "actions_only/flags_include or filter in code."
                           % (max_results, total_matched[0]))
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
            try:
                used = pipe.GetAllUsedDescriptors(True)
            except TypeError:
                used = pipe.GetAllUsedDescriptors()
            for d in used:
                res = str(d.descriptor.resource)
                # 'bindset' does not exist in this RenderDoc API generation
                # (bindings are stage + index); kept as null for callers
                # expecting the key. 'bind' = the access index.
                try:
                    bindset = int(d.access.arrayElement)
                except Exception:
                    bindset = None
                descs.append({"stage": str(d.access.stage),
                              "bindset": None,
                              "bind": d.access.index,
                              "res_type": str(d.descriptor.type),
                              "type": str(d.access.type),
                              "index": d.access.index,
                              "arrayElement": bindset,
                              "readWrite": bool(d.access.readWrite),
                              "resource": res,
                              "name": names.get(res, "?")})
        except Exception as e:
            out["descriptors_err"] = str(e)
        out["descriptors"] = descs
        try:
            vbs = []
            for i, vb in enumerate(pipe.GetVBuffers()):
                if vb.resourceId != rd.ResourceId():
                    vbs.append({"slot": i, "resource": str(vb.resourceId),
                                "byteOffset": int(vb.byteOffset),
                                "byteStride": int(vb.byteStride),
                                "byteSize": int(vb.byteSize),
                                "name": names.get(str(vb.resourceId), "?")})
            out["vertex_buffers"] = vbs
        except Exception as e:
            out["vertex_buffers_err"] = str(e)
        try:
            attrs = []
            for a in pipe.GetVertexInputs():
                if not a.used:
                    continue
                attrs.append({"name": str(a.name),
                              "vertexBuffer": int(a.vertexBuffer),
                              "byteOffset": int(a.byteOffset),
                              "format": _format_readable(a.format),
                              "format_raw": _format_raw(a.format),
                              "instanceRate": int(a.instanceRate),
                              "perInstance": bool(a.perInstance)})
            out["vertex_inputs"] = attrs
        except Exception as e:
            out["vertex_inputs_err"] = str(e)
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


def _resolve_screenshot_target(ctrl, tex_id, eid, output_index, chosen):
    """Shared by the SaveTexture path and the crop/scale path: pick the
    texture to screenshot. Returns the resolved ResourceId."""
    names = _names(ctrl)
    rid = None
    if tex_id is not None:
        rid = _resolve_rid(ctrl, tex_id)
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
            "UI, or pass an explicit 'texture_id' (see get_texture_list).")
    chosen["resource"] = str(rid)
    chosen["name"] = names.get(str(rid), "?")
    return rid


def _clamp01(v):
    if v is None or v != v:  # None or NaN
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def _rgba_crop_scale(ctrl, rid, tex, cx, cy, cw, ch, ow, oh):
    """Crop (cx, cy, cw, ch) out of texture ``rid`` and nearest-neighbor
    downscale to (ow, oh), decoded to RGBA8 bytes. Only simple unpacked
    formats are supported (see _pixel_decoder)."""
    try:
        dec, bpp, raw = _pixel_decoder(tex.format)
    except ToolError:
        raise ToolError(
            "region/max_width need a directly decodable texture format "
            "(UNORM8/UNORM16/SNORM8/FLOAT16/FLOAT32); this texture is %s. "
            "Use save_to(path) without region/max_width instead (SaveTexture "
            "handles every format)." % _format_readable(tex.format))
    n = raw["compCount"]
    bw = raw["compByteWidth"]
    ct = raw.get("compType")
    sub = rd.Subresource()
    sub.mip = 0
    sub.slice.sliceIndex = 0
    data = bytes(ctrl.GetTextureData(rid, sub))
    tw, th = int(tex.width), int(tex.height)
    if len(data) < tw * th * bpp:
        raise ToolError("GetTextureData returned %d bytes, less than the "
                        "full mip 0 (%d expected for %dx%d)."
                        % (len(data), tw * th * bpp, tw, th))
    # nearest-neighbor sample maps (identity when not downscaling)
    xmap = [cx + (i * cw + ow // 2) // ow for i in range(ow)]
    ymap = [cy + (j * ch + oh // 2) // oh for j in range(oh)]
    out = bytearray(ow * oh * 4)
    fast = bw == 1 and ct in ("UNorm", "UNormSRGB")
    row_stride = tw * bpp
    for j in range(oh):
        sy = ymap[j]
        srow = sy * row_stride
        drow = j * ow * 4
        for i in range(ow):
            so = srow + xmap[i] * bpp
            do = drow + i * 4
            if fast and n == 4:
                out[do:do + 4] = data[so:so + 4]
            elif fast:
                r = data[so]
                g = data[so + 1] if n > 1 else r
                b = data[so + 2] if n > 2 else r
                out[do] = r
                out[do + 1] = g
                out[do + 2] = b
                out[do + 3] = 255
            else:
                vals = dec(data[so:so + bpp])
                v0 = int(_clamp01(vals[0]) * 255.0 + 0.5)
                v1 = int(_clamp01(vals[1]) * 255.0 + 0.5) if n > 1 else v0
                v2 = int(_clamp01(vals[2]) * 255.0 + 0.5) if n > 2 else v0
                v3 = int(_clamp01(vals[3]) * 255.0 + 0.5) if n > 3 else 255
                out[do] = v0
                out[do + 1] = v1
                out[do + 2] = v2
                out[do + 3] = v3
    return bytes(out), ow, oh


try:
    import binascii as _binascii

    def _crc32(data):
        return _binascii.crc32(data) & 0xFFFFFFFF
except ImportError:  # pragma: no cover - binascii ships with base64's dep
    _CRC_TABLE = None

    def _crc32(data):
        global _CRC_TABLE
        if _CRC_TABLE is None:
            tbl = []
            for nn in range(256):
                c = nn
                for _ in range(8):
                    c = (c >> 1) ^ (0xEDB88320 if c & 1 else 0)
                tbl.append(c)
            _CRC_TABLE = tbl
        crc = 0xFFFFFFFF
        for b in data:
            crc = _CRC_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
        return crc ^ 0xFFFFFFFF


try:
    import zlib as _zlib
except ImportError:
    _zlib = None


def _adler32(data):
    """Pure-python Adler-32 (only needed when the zlib module is absent)."""
    a, b = 1, 0
    for byte in data:
        a = (a + byte) % 65521
        b = (b + a) % 65521
    return ((b << 16) | a) & 0xFFFFFFFF


def _deflate(data):
    """zlib stream. Real compression when the zlib module exists, otherwise
    valid stored (uncompressed) deflate blocks."""
    if _zlib is not None:
        return _zlib.compress(data, 6)
    out = bytearray(b"\x78\x01")  # CMF/FLG: 32K window, fastest
    i = 0
    n = len(data)
    while True:
        blk = data[i:i + 65535]
        i += len(blk)
        last = 1 if i >= n else 0
        ln = len(blk)
        out.append(0x01 if last else 0x00)  # BFINAL + BTYPE=00 (stored)
        out += (ln & 0xFFFF).to_bytes(2, "little")
        out += ((~ln) & 0xFFFF).to_bytes(2, "little")
        out += blk
        if last:
            break
    out += _adler32(data).to_bytes(4, "big")
    return bytes(out)


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_chunk(tag, payload):
    ln = len(payload)
    return (ln.to_bytes(4, "big") + tag + payload +
            _crc32(tag + payload).to_bytes(4, "big"))


def _png_encode(rgba, w, h):
    """Minimal RGBA8 PNG encoder (no external deps; zlib when available)."""
    stride = w * 4
    rows = []
    for j in range(h):
        rows.append(b"\x00" + rgba[j * stride:(j + 1) * stride])
    ihdr = (w.to_bytes(4, "big") + h.to_bytes(4, "big") +
            bytes(bytearray([8, 6, 0, 0, 0])))  # 8-bit RGBA
    return (_PNG_SIG + _png_chunk(b"IHDR", ihdr) +
            _png_chunk(b"IDAT", _deflate(b"".join(rows))) +
            _png_chunk(b"IEND", b""))


def _parse_region_arg(region):
    """Accept [x, y, w, h] or {x, y, w, h} -> tuple or None."""
    if region is None or region == "" or region == []:
        return None
    try:
        if isinstance(region, dict):
            r = (int(region["x"]), int(region["y"]),
                 int(region["w"]), int(region["h"]))
        else:
            vals = [int(v) for v in list(region)]
            if len(vals) != 4:
                raise ValueError("need exactly 4 values")
            r = tuple(vals)
    except ToolError:
        raise
    except Exception:
        raise ToolError("region must be [x, y, w, h] or {x, y, w, h}")
    x, y, w, h = r
    if w <= 0 or h <= 0:
        raise ToolError("region w/h must be positive (got %dx%d)" % (w, h))
    if x < 0 or y < 0:
        raise ToolError("region x/y must be >= 0 (got %d, %d)" % (x, y))
    return r


def get_screenshot(args):
    _require_capture()
    eid = args.get("event_id")
    tex_id = args.get("texture_id")
    output_index = int(args.get("output_index", 0))
    region = _parse_region_arg(args.get("region"))
    max_width = args.get("max_width")
    try:
        max_width = int(max_width) if max_width not in (None, "", 0) else None
    except (TypeError, ValueError):
        max_width = None
    if max_width is not None and max_width <= 0:
        max_width = None
    save_to = args.get("save_to")
    if save_to is not None and str(save_to).strip():
        save_to = os.path.abspath(
            os.path.expandvars(os.path.expanduser(str(save_to).strip())))
        if not save_to.lower().endswith(".png"):
            save_to += ".png"
        parent = os.path.dirname(save_to)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent)
            except OSError as e:
                raise ToolError("cannot create save_to directory %s: %s"
                                % (parent, e))
    else:
        save_to = None
    # crop/scale need raw texture bytes + our own PNG encoder; the plain
    # SaveTexture path handles every format and writes the full image
    custom = region is not None or max_width is not None
    chosen = {"source": None}
    path = save_to or os.path.join(os.environ.get("TEMP", os.getcwd()),
                                   "renderdoc_mcp_shot_%d.png" % os.getpid())

    if custom:
        png_holder = {}

        def work(ctrl):
            rid = _resolve_screenshot_target(ctrl, tex_id, eid,
                                             output_index, chosen)
            tex = None
            for t in ctrl.GetTextures():
                if t.resourceId == rid:
                    tex = t
                    break
            if tex is None:
                raise ToolError("No texture %s found in this capture." % rid)
            tw, th = int(tex.width), int(tex.height)
            if region is not None:
                cx, cy, cw, ch = region
                if cx + cw > tw or cy + ch > th:
                    raise ToolError(
                        "region (%d, %d, %dx%d) is outside texture %s "
                        "(%dx%d, %s)."
                        % (cx, cy, cw, ch, rid, tw, th,
                           chosen.get("name", "?")))
            else:
                cx, cy, cw, ch = 0, 0, tw, th
            ow, oh = cw, ch
            if max_width is not None and cw > max_width:
                ow = max_width
                oh = max(1, (ch * max_width + cw // 2) // cw)
            png_holder["png"], ow, oh = _rgba_crop_scale(
                ctrl, rid, tex, cx, cy, cw, ch, ow, oh)
            chosen["width"] = ow
            chosen["height"] = oh
            chosen["region"] = [cx, cy, cw, ch]
            chosen["max_width"] = max_width

        _replay_invoke(work)
        png = png_holder["png"]
        try:
            with open(path, "wb") as f:
                f.write(png)
        except OSError as e:
            raise ToolError("cannot write %s: %s" % (path, e))
        chosen["save_result"] = "Succeeded (self-encoded PNG)"
        if not save_to and len(png) > 8 * 1024 * 1024:
            raise ToolError("encoded PNG is %.1f MB - too large to inline; "
                            "use save_to(path) or a smaller max_width."
                            % (len(png) / 1048576.0))
    else:

        def work(ctrl):
            rid = _resolve_screenshot_target(ctrl, tex_id, eid,
                                             output_index, chosen)
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

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ToolError("cannot read back %s: %s" % (path, e))
    if not save_to:
        try:
            os.remove(path)
        except OSError:
            pass
    if not data:
        raise ToolError("screenshot file is empty: %s" % path)
    text = ("Screenshot of %s: %s (%sx%s %s) -> %s (%d bytes)" %
            (chosen.get("source", "?"), chosen.get("name", "?"),
             chosen.get("width", "?"), chosen.get("height", "?"),
             chosen.get("format", "?"), path, len(data)))
    if save_to:
        return [{"type": "text", "text": text + "; not inlined (save_to)"}]
    return [{"type": "text", "text": text},
            {"type": "image", "data": base64.b64encode(data).decode("ascii"),
             "mimeType": "image/png"}]


def _walk_flat(actions, out):
    for a in actions:
        out.append(a)
        _walk_flat(a.children, out)


def get_pixel_history(args):
    _require_capture()
    rid_arg = args["texture_id"]
    x = int(args["x"])
    y = int(args["y"])
    mip = int(args.get("mip", 0))
    slice_ = int(args.get("slice", 0))
    eid = args.get("event_id")
    mode = str(args.get("mode", "raw") or "raw").lower()
    if mode not in ("raw", "summary"):
        raise ToolError("Unknown mode %r (valid: 'raw', 'summary')" % mode)
    hist = {"texture": str(rid_arg), "x": x, "y": y, "mip": mip,
            "slice": slice_, "mode": mode}
    if _instance_id:
        hist["instance"] = _instance_id
    mods = []

    def work(ctrl):
        rid = _resolve_rid(ctrl, rid_arg)
        hist["texture"] = str(rid)
        if eid is not None:
            ctrl.SetFrameEvent(int(eid), True)
        hist["name"] = _names(ctrl).get(str(rid), "?")
        # bounds check: an out-of-range point would silently return an empty
        # history, which reads as 'nothing ever wrote this pixel'
        try:
            for t in ctrl.GetTextures():
                if t.resourceId == rid:
                    tw = max(1, int(t.width) >> mip)
                    th = max(1, int(t.height) >> mip)
                    if x < 0 or y < 0 or x >= tw or y >= th:
                        raise ToolError(
                            "Pixel (x=%d, y=%d) is outside texture %s "
                            "(%dx%d at mip %d -> %dx%d; %s)."
                            % (x, y, rid, t.width, t.height, mip, tw, th,
                               hist["name"]))
                    break
        except ToolError:
            raise
        except Exception:
            pass
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
            if mode == "summary":
                mods.append({"event_id": r.eventId,
                             "frag_index": r.fragIndex,
                             "primitive": r.primitiveID,
                             "passed": bool(r.Passed()),
                             "rejected_by": rejects,
                             "unboundPS": bool(r.unboundPS),
                             "shader_out": _f6v(_mv_px(r.shaderOut).floatValue),
                             "pre_mod": _f6v(_mv_px(r.preMod).floatValue),
                             "post_mod": _f6v(_mv_px(r.postMod).floatValue),
                             "depth_in": _f6(r.preMod.depth),
                             "depth_out": _f6(r.postMod.depth),
                             "stencil_in": _int_or_none(r.preMod.stencil),
                             "stencil_out": _int_or_none(r.postMod.stencil)})
            else:
                entry = {"event_id": r.eventId,
                         "frag_index": r.fragIndex,
                         "primitive": r.primitiveID,
                         "passed": bool(r.Passed()),
                         "unboundPS": bool(r.unboundPS),
                         "directShaderWrite": bool(r.directShaderWrite),
                         "rejected_by": rejects,
                         "preMod": _pixel_value_compact(r.preMod),
                         "shaderOut": _pixel_value_compact(r.shaderOut),
                         "postMod": _pixel_value_compact(r.postMod)}
                mods.append(entry)

    _replay_invoke(work)
    hist["num_modifications"] = len(mods)
    hist["modifications"] = mods
    return _text_result(hist)


def _region_per_channel(data, dec, bpp, stride, x, y, w, h, num_channels):
    """Decode an ROI out of raw texture bytes into per-channel value lists."""
    chans = [[] for _ in range(num_channels)]
    for j in range(y, y + h):
        row = data[j * stride + x * bpp:(j * stride + x * bpp) + w * bpp]
        for i in range(w):
            vals = dec(row[i * bpp:(i + 1) * bpp])
            for c in range(num_channels):
                chans[c].append(vals[c])
    return chans


def _region_stats(rid_arg, eid, x, y, w, h, mip, slice_, tag):
    """Fetch raw texture bytes at an event and decode an ROI. Returns
    (stats_dict, per_channel_lists, dec, bpp, width, height, fmt_raw)."""
    rid_holder = {}
    fmt_holder = {}
    chans_holder = {}

    def work(ctrl):
        rid = _resolve_rid(ctrl, rid_arg)
        if eid is not None:
            ctrl.SetFrameEvent(int(eid), True)
        tex = None
        try:
            for t in ctrl.GetTextures():
                if t.resourceId == rid:
                    tex = t
                    break
        except Exception:
            pass
        if tex is None:
            raise ToolError("No texture %s found in this capture." % rid)
        tw = max(1, int(tex.width) >> mip)
        th = max(1, int(tex.height) >> mip)
        if w <= 0 or h <= 0:
            raise ToolError("%s: region w/h must be positive (got %dx%d)."
                            % (tag, w, h))
        if x < 0 or y < 0 or x + w > tw or y + h > th:
            raise ToolError("%s: region (x=%d, y=%d, w=%d, h=%d) is outside "
                            "texture %s (%dx%d at mip %d -> %dx%d, %s)."
                            % (tag, x, y, w, h, rid, tex.width, tex.height,
                               mip, tw, th, str(tex.format.Name())
                               if _has_name(tex.format) else ""))
        dec, bpp, raw = _pixel_decoder(tex.format)
        sub = rd.Subresource()
        sub.mip = mip
        sub.slice = slice_
        data = ctrl.GetTextureData(rid, sub)
        if len(data) < th * tw * bpp:
            raise ToolError("%s: GetTextureData returned %d bytes, less than "
                            "a full subresource (%d expected for %dx%d)."
                            % (tag, len(data), th * tw * bpp, tw, th))
        rid_holder["rid"] = str(rid)
        rid_holder["name"] = _names(ctrl).get(str(rid), "?")
        rid_holder["tex_w"] = int(tex.width)
        rid_holder["tex_h"] = int(tex.height)
        fmt_holder["raw"] = raw
        fmt_holder["readable"] = _format_readable(tex.format)
        chans_holder["chans"] = _region_per_channel(
            bytes(data), dec, bpp, tw * bpp, x, y, w, h, raw["compCount"])

    _replay_invoke(work)
    info = {"texture": rid_holder["rid"], "name": rid_holder["name"],
            "texture_size": [rid_holder["tex_w"], rid_holder["tex_h"]],
            "x": x, "y": y, "w": w, "h": h, "mip": mip, "slice": slice_,
            "format": fmt_holder["readable"], "format_raw": fmt_holder["raw"],
            "num_pixels": w * h}
    return info, chans_holder["chans"]


def _has_name(fmt):
    try:
        return bool(fmt.Name())
    except Exception:
        return False


def get_texture_stats(args):
    _require_capture()
    x = int(args["x"])
    y = int(args["y"])
    w = int(args["w"])
    h = int(args["h"])
    mip = int(args.get("mip", 0))
    slice_ = int(args.get("slice", 0))
    info, chans = _region_stats(rid_arg=args["texture_id"],
                                eid=args.get("event_id"), x=x, y=y, w=w, h=h,
                                mip=mip, slice_=slice_,
                                tag="get_texture_stats")
    out = dict(info)
    out["per_channel"] = [_channel_stats(c) for c in chans]
    if _instance_id:
        out["instance"] = _instance_id
    return _text_result(out)


def compare_regions(args):
    _require_capture()
    x = int(args["x"])
    y = int(args["y"])
    w = int(args["w"])
    h = int(args["h"])
    mip = int(args.get("mip", 0))
    slice_ = int(args.get("slice", 0))
    try:
        threshold = float(args.get("threshold", 2.0 / 255.0))
    except (TypeError, ValueError):
        threshold = 2.0 / 255.0
    out = {"x": x, "y": y, "w": w, "h": h, "mip": mip, "slice": slice_,
           "threshold": threshold}
    if _instance_id:
        out["instance"] = _instance_id

    info_a, chans_a = _region_stats(
        rid_arg=args["texture_a"], eid=args.get("event_a"),
        x=x, y=y, w=w, h=h, mip=mip, slice_=slice_, tag="region A")
    info_b, chans_b = _region_stats(
        rid_arg=args["texture_b"], eid=args.get("event_b"),
        x=x, y=y, w=w, h=h, mip=mip, slice_=slice_, tag="region B")

    out["a"] = dict((k, v) for k, v in info_a.items()
                    if k not in ("x", "y", "w", "h", "mip", "slice"))
    out["b"] = dict((k, v) for k, v in info_b.items()
                    if k not in ("x", "y", "w", "h", "mip", "slice"))
    out["per_channel_a"] = [_channel_stats(c) for c in chans_a]
    out["per_channel_b"] = [_channel_stats(c) for c in chans_b]

    ra = info_a["format_raw"]
    rb = info_b["format_raw"]
    same_dims = (info_a["texture_size"] == info_b["texture_size"])
    same_fmt = (ra.get("compType") == rb.get("compType") and
                ra.get("compCount") == rb.get("compCount") and
                ra.get("compByteWidth") == rb.get("compByteWidth"))
    out["dims_mismatch"] = not same_dims
    out["formats_mismatch"] = not same_fmt
    if not same_dims:
        out["diff_reason"] = ("texture sizes differ (%s vs %s); only "
                              "per-side statistics are reported"
                              % (info_a["texture_size"],
                                 info_b["texture_size"]))
    elif not same_fmt:
        out["diff_reason"] = ("formats differ (%s vs %s); decoded value "
                              "comparison is not meaningful"
                              % (info_a["format"], info_b["format"]))
    else:
        n_px = w * h
        n_vals = 0
        total = 0.0
        max_abs = 0.0
        over = 0
        for ca, cb in zip(chans_a, chans_b):
            for va, vb in zip(ca, cb):
                d = abs(va - vb)
                n_vals += 1
                total += d
                if d > max_abs:
                    max_abs = d
                if d > threshold:
                    over += 1
        out["max_abs_diff"] = _f6(max_abs)
        out["mean_abs_diff"] = _f6(total / n_vals) if n_vals else None
        out["pixels_over_threshold"] = over
        out["pixels_over_threshold_ratio"] = \
            _f6(over / float(n_px)) if n_px else None
    return _text_result(out)
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
    rid_arg = args["buffer_id"]
    offset = int(args.get("offset", 0))
    length = min(int(args.get("length", 1024)), 65536)
    eid = args.get("event_id")
    out = {"buffer": str(rid_arg), "offset": offset, "requested_length": length}

    def work(ctrl):
        if eid is not None:
            ctrl.SetFrameEvent(int(eid), True)
        rid = _resolve_rid(ctrl, rid_arg)
        out["buffer"] = str(rid)
        out["name"] = _names(ctrl).get(str(rid), "?")
        data = ctrl.GetBufferData(rid, offset, length)
        out["returned_length"] = len(data)
        out["hexdump"] = _hexdump(bytes(data))
        out["raw_base64"] = base64.b64encode(bytes(data)).decode("ascii") \
            if len(data) <= 4096 else ""

    _replay_invoke(work)
    return _text_result(out)


# ---------------------------------------------------------------------------
# buffer understanding: region classification, pattern search, vertex decode.
# All three fetch raw bytes through GetBufferData in chunks (replay thread)
# and do the heavy lifting inside the extension so agents never receive
# multi-megabyte hexdumps.
# ---------------------------------------------------------------------------

_BUF_CHUNK = 4 << 20  # 4MB GetBufferData reads


def _buffer_length(ctrl, rid):
    try:
        for b in ctrl.GetBuffers():
            if b.resourceId == rid:
                return int(b.length)
    except Exception:
        pass
    return None


def _iter_buffer_data(ctrl, rid, total, chunk=_BUF_CHUNK):
    """Yield the buffer's bytes in chunks. Stops early when RenderDoc
    returns less than requested (buffer shrunk / unreadable tail)."""
    off = 0
    while off < total:
        n = min(chunk, total - off)
        data = bytes(ctrl.GetBufferData(rid, off, n))
        if not data:
            return
        yield data
        off += len(data)
        if len(data) < n:
            return


# classify every byte: Z = 0x00, C = 0xCD (MSVC debug-heap fill), X = other
_CLASS_TABLE = bytes.maketrans(
    bytes(bytearray(range(256))),
    bytes(bytearray(ord("Z") if v == 0x00 else
                    (ord("C") if v == 0xCD else ord("X"))
                    for v in range(256))))

try:
    import re as _re
    _RUN_SPLIT = _re.compile(rb"Z+|C+|X+")

    def _runs_of(cls):
        for m in _RUN_SPLIT.finditer(cls):
            s, e = m.start(), m.end()
            yield s, e, cls[s:s + 1]
except ImportError:  # pragma: no cover - pure-python fallback
    def _runs_of(cls):
        n = len(cls)
        i = 0
        while i < n:
            c = cls[i]
            j = i + 1
            while j < n and cls[j] == c:
                j += 1
            yield i, j, cls[i:i + 1]
            i = j


def get_buffer_regions(args):
    """REQ-04: classify a buffer into merged same-class byte regions."""
    _require_capture()
    rid_arg = args["buffer_id"]
    eid = args.get("event_id")
    max_regions = int(args.get("max_regions", 64) or 64)
    if max_regions < 1:
        max_regions = 1
    out = {"buffer": str(rid_arg), "max_regions": max_regions}
    if _instance_id:
        out["instance"] = _instance_id
    regions = []
    counts = {}
    first_written = [None]
    readable = [0]

    def add_region(start, end, cls_char, uniform):
        if cls_char == b"Z":
            cls = "0x00"
        elif cls_char == b"C":
            cls = "0xCD"
        elif uniform is not None:
            cls = "0x%02X" % uniform[0]
        else:
            cls = "mixed"
        counts[cls] = counts.get(cls, 0) + (end - start)
        if cls != "0x00" and first_written[0] is None:
            first_written[0] = start
        regions.append({"byte_offset": start, "byte_length": end - start,
                        "class": cls})

    def work(ctrl):
        if eid is not None:
            ctrl.SetFrameEvent(int(eid), True)
        rid = _resolve_rid(ctrl, rid_arg)
        out["buffer"] = str(rid)
        out["name"] = _names(ctrl).get(str(rid), "?")
        total = _buffer_length(ctrl, rid)
        if total is None:
            raise ToolError("No buffer %s in this capture (see "
                            "get_buffer_list)." % rid)
        out["length"] = total
        if total == 0:
            return
        prev = None  # run spanning a chunk boundary: [start, end, cls, uniform]
        off = 0
        for data in _iter_buffer_data(ctrl, rid, total):
            cls = data.translate(_CLASS_TABLE)
            for s, e, c in _runs_of(cls):
                uniform = None
                if c == b"X":
                    seg = data[s:e]
                    fb = seg[:1]
                    if seg.count(fb) == (e - s):
                        uniform = fb
                astart, aend = off + s, off + e
                # a chunk boundary may split one logical run; continue the
                # previous run whenever the class matches. An X run stays
                # uniform only if both halves agree on the same byte value;
                # any disagreement (or either half mixed) merges into a
                # single 'mixed' region covering both halves.
                if prev is not None and prev[2] == c:
                    if c == b"X" and not (prev[3] is not None and
                                          uniform is not None and
                                          prev[3] == uniform):
                        prev[3] = None
                    prev[1] = aend
                else:
                    if prev is not None:
                        add_region(prev[0], prev[1], prev[2], prev[3])
                    prev = [astart, aend, c, uniform]
            off += len(data)
        readable[0] = off
        if prev is not None:
            add_region(prev[0], prev[1], prev[2], prev[3])

    _replay_invoke(work)
    out["num_regions"] = len(regions)
    truncated = False
    if len(regions) > max_regions:
        truncated = True
        tail = regions[max_regions - 1:]
        merged_len = sum(r["byte_length"] for r in tail)
        regions = regions[:max_regions - 1] + [
            {"byte_offset": tail[0]["byte_offset"],
             "byte_length": merged_len, "class": "mixed"}]
    out["regions"] = regions
    out["bytes_by_class"] = counts
    out["first_written_offset"] = first_written[0]
    if truncated:
        out["truncated"] = True
        out["note"] = ("region list truncated at max_regions=%d; raise it or "
                       "narrow with get_buffer_data" % max_regions)
    if readable[0] and readable[0] < out.get("length", 0):
        out["readable_bytes"] = readable[0]
        out["note"] = ("only %d of %d bytes were readable from the replay" %
                       (readable[0], out.get("length", 0)))
    return _text_result(out)


def buffer_search(args):
    """REQ-05: search a buffer for a u32/f32/hex pattern."""
    _require_capture()
    rid_arg = args["buffer_id"]
    eid = args.get("event_id")
    ptype = str(args.get("type", "u32") or "u32").lower()
    pattern = args.get("pattern")
    if pattern is None or (isinstance(pattern, str) and not pattern.strip()):
        raise ToolError("pattern is required (number for u32/f32, hex "
                        "string for hex).")
    if ptype == "u32":
        try:
            v = int(pattern)
        except (TypeError, ValueError):
            raise ToolError("u32 pattern must be an integer (got %r)"
                            % (pattern,))
        pat = (v & 0xFFFFFFFF).to_bytes(4, "little")
    elif ptype == "f32":
        try:
            v = float(pattern)
        except (TypeError, ValueError):
            raise ToolError("f32 pattern must be a number (got %r)"
                            % (pattern,))
        pat = bytes(bytearray(memoryview(ctypes.c_float(v)).tobytes()))
    elif ptype == "hex":
        hs = str(pattern).strip().lower()
        if hs.startswith("0x"):
            hs = hs[2:]
        hs = "".join(hs.split())
        try:
            pat = bytes.fromhex(hs)
        except ValueError as e:
            raise ToolError("bad hex pattern %r: %s" % (pattern, e))
    else:
        raise ToolError("Unknown pattern type %r (valid: u32, f32, hex)"
                        % ptype)
    if not pat:
        raise ToolError("pattern is empty")
    stride_align = int(args.get("stride_align", 1) or 1)
    if stride_align < 1:
        stride_align = 1
    max_hits = max(int(args.get("max_hits", 32) or 32), 0)
    out = {"buffer": str(rid_arg), "type": ptype,
           "pattern_hex": pat.hex(), "pattern_length": len(pat),
           "stride_align": stride_align, "max_hits": max_hits}
    if _instance_id:
        out["instance"] = _instance_id
    hits = []
    hits_total = [0]
    scanned = [0]

    def work(ctrl):
        if eid is not None:
            ctrl.SetFrameEvent(int(eid), True)
        rid = _resolve_rid(ctrl, rid_arg)
        out["buffer"] = str(rid)
        out["name"] = _names(ctrl).get(str(rid), "?")
        total = _buffer_length(ctrl, rid)
        if total is None:
            raise ToolError("No buffer %s in this capture (see "
                            "get_buffer_list)." % rid)
        out["length"] = total
        keep = len(pat) - 1
        carry = b""
        base = 0  # absolute offset of carry's first byte
        for data in _iter_buffer_data(ctrl, rid, total):
            buf = carry + data
            cstart = base
            pos = 0
            while True:
                i = buf.find(pat, pos)
                if i < 0:
                    break
                pos = i + 1
                abs_off = cstart + i
                if abs_off % stride_align:
                    continue
                hits_total[0] += 1
                if max_hits <= 0 or len(hits) < max_hits:
                    hits.append(abs_off)
            scanned[0] = cstart + len(buf)
            base = max(0, scanned[0] - keep)
            carry = buf[-keep:] if keep else b""

    _replay_invoke(work)
    out["hits"] = hits
    out["hits_total"] = hits_total[0]
    out["scanned_bytes"] = min(out.get("length", 0), scanned[0])
    if hits_total[0] == 0:
        out["note"] = ("no hits; the value may not be in this buffer, or is "
                       "stored in another encoding (try type=f32 for floats)")
    elif hits_total[0] > len(hits):
        out["note"] = ("%d hits total, first %d shown; narrow with "
                       "stride_align/max_hits" % (hits_total[0], len(hits)))
    return _text_result(out)


_ATTR_TYPES = {
    "f32": (4, "f4"), "float": (4, "f4"),
    "f16": (2, "f2"), "half": (2, "f2"),
    "u32": (4, "u4"), "uint": (4, "u4"),
    "s32": (4, "s4"), "int": (4, "s4"),
    "u16": (2, "u2"), "s16": (2, "s2"),
    "u8": (1, "u1"), "s8": (1, "s1"),
    "unorm8x4": (4, "u8x4"),
}


def _decode_attr_values(atype, count, data):
    """Decode ``count`` elements of ``atype`` from the start of ``data``."""
    size, kind = _ATTR_TYPES[atype]
    need = size * count
    if len(data) < need:
        raise ToolError("attribute needs %d bytes but only %d remain"
                        % (need, len(data)))
    if kind == "f4":
        return [_cfloat(data[i * 4:(i + 1) * 4]) for i in range(count)]
    if kind == "f2":
        return [_half_to_float(data[i * 2:(i + 1) * 2]) for i in range(count)]
    if kind == "u4":
        return [int.from_bytes(data[i * 4:(i + 1) * 4], "little")
                for i in range(count)]
    if kind == "s4":
        return [int.from_bytes(data[i * 4:(i + 1) * 4], "little", signed=True)
                for i in range(count)]
    if kind == "u2":
        return [int.from_bytes(data[i * 2:(i + 1) * 2], "little")
                for i in range(count)]
    if kind == "s2":
        return [int.from_bytes(data[i * 2:(i + 1) * 2], "little", signed=True)
                for i in range(count)]
    if kind == "u1":
        return [int(b) for b in data[:count]]
    if kind == "s1":
        return [int.from_bytes(data[i:i + 1], "little", signed=True)
                for i in range(count)]
    if kind == "u8x4":
        return [round(data[0] / 255.0, 6), round(data[1] / 255.0, 6),
                round(data[2] / 255.0, 6), round(data[3] / 255.0, 6)]
    raise ToolError("unsupported attr kind %r" % kind)  # pragma: no cover


def get_vertex_data(args):
    """REQ-06: stride-aware vertex/instance data decoding."""
    _require_capture()
    rid_arg = args["buffer_id"]
    stride = int(args.get("stride", 0) or 0)
    if stride <= 0:
        raise ToolError("stride must be a positive byte count")
    start_vertex = max(int(args.get("start_vertex", 0) or 0), 0)
    count = max(int(args.get("count", 1) or 1), 1)
    if count > 4096:
        raise ToolError("count is capped at 4096 vertices per call")
    attrs_in = args.get("attrs")
    if not isinstance(attrs_in, list) or not attrs_in:
        raise ToolError("attrs is required: [{name, byte_offset, type, "
                        "count?}, ...]")
    attrs = []
    max_end = 0
    for i, a in enumerate(attrs_in):
        if not isinstance(a, dict):
            raise ToolError("attrs[%d] must be an object with name/"
                            "byte_offset/type" % i)
        name = str(a.get("name") or "attr%d" % len(attrs))
        boff = int(a.get("byte_offset", 0) or 0)
        atype = str(a.get("type", "")).lower()
        if atype not in _ATTR_TYPES:
            raise ToolError("Unknown attr type %r for %r (valid: %s)"
                            % (atype, name,
                               ", ".join(sorted(_ATTR_TYPES))))
        acount = max(int(a.get("count", 1) or 1), 1)
        attrs.append({"name": name, "byte_offset": boff, "type": atype,
                      "count": acount})
        max_end = max(max_end, boff + _ATTR_TYPES[atype][0] * acount)
    eid = args.get("event_id")
    out = {"buffer": str(rid_arg), "stride": stride,
           "start_vertex": start_vertex, "count": count, "attrs": attrs}
    if _instance_id:
        out["instance"] = _instance_id
    rows = []

    def work(ctrl):
        if eid is not None:
            ctrl.SetFrameEvent(int(eid), True)
        rid = _resolve_rid(ctrl, rid_arg)
        out["buffer"] = str(rid)
        out["name"] = _names(ctrl).get(str(rid), "?")
        total = _buffer_length(ctrl, rid)
        if total is None:
            raise ToolError("No buffer %s in this capture (see "
                            "get_buffer_list)." % rid)
        out["length"] = total
        first = start_vertex * stride
        need = (count - 1) * stride + max_end
        if first + need > total:
            raise ToolError(
                "vertex range needs bytes [%d, %d) but buffer %s is only %d "
                "bytes (%d vertices at stride %d)."
                % (first, first + need, rid, total, total // stride, stride))
        data = bytes(ctrl.GetBufferData(rid, first, need))
        if len(data) < need:
            raise ToolError("GetBufferData returned %d of %d bytes"
                            % (len(data), need))
        for v in range(count):
            row_off = v * stride
            row = {"vertex": start_vertex + v}
            for a in attrs:
                row[a["name"]] = _decode_attr_values(
                    a["type"], a["count"], data[row_off + a["byte_offset"]:])
            rows.append(row)

    _replay_invoke(work)
    out["vertices"] = rows
    return _text_result(out)


def get_constant_buffer(args):
    _require_capture()
    eid = int(args["event_id"])
    stage = _parse_stage(args.get("stage"))
    slot = int(args["slot"])
    max_floats = max(int(args.get("max_floats", 64) or 64), 1)
    out = {"event_id": eid, "stage": str(stage), "slot": slot}
    if _instance_id:
        out["instance"] = _instance_id

    def work(ctrl):
        ctrl.SetFrameEvent(eid, True)
        pipe = ctrl.GetPipelineState()
        sid = pipe.GetShader(stage)
        if sid == rd.ResourceId():
            raise ToolError("No shader bound at stage %s for event %d."
                            % (stage, eid))
        refl = pipe.GetShaderReflection(stage)
        if refl is None or slot < 0 or slot >= len(refl.constantBlocks):
            names = [(i, str(cb.name), int(cb.byteSize))
                     for i, cb in enumerate(refl.constantBlocks)] \
                if refl is not None else []
            raise ToolError(
                "No constant block at slot %d for stage %s (event %d). "
                "Available: %s" % (slot, stage, eid, names))
        cb = refl.constantBlocks[slot]
        out["name"] = str(cb.name)
        out["byte_size"] = int(cb.byteSize)
        try:
            out["bind"] = int(cb.fixedBindNumber)
        except Exception:
            out["bind"] = None
        try:
            out["bindset_or_space"] = int(cb.fixedBindSetOrSpace)
        except Exception:
            out["bindset_or_space"] = None
        out["variables"] = []
        for v in cb.variables:
            entry = {"name": str(v.name), "byte_offset": int(v.byteOffset)}
            try:
                entry["type"] = str(v.type.baseType)
            except Exception as e:
                entry["type_err"] = str(e)
            out["variables"].append(entry)
        # fetch the bound buffer + its contents
        try:
            binding = pipe.GetConstantBlock(stage, slot, 0)
            desc = binding.descriptor
        except Exception as e:
            raise ToolError("GetConstantBlock(%s, %d) failed: %s"
                            % (stage, slot, e))
        rid = desc.resource
        if rid == rd.ResourceId():
            raise ToolError(
                "Constant block slot %d (%s) of stage %s has no buffer "
                "bound at event %d." % (slot, out["name"], stage, eid))
        out["buffer"] = str(rid)
        out["buffer_name"] = _names(ctrl).get(str(rid), "?")
        out["buffer_byte_offset"] = int(desc.byteOffset)
        read_len = int(desc.byteSize)
        if read_len <= 0 or read_len > max_floats * 4:
            read_len = max_floats * 4
        out["requested_bytes"] = read_len
        data = ctrl.GetBufferData(rid, int(desc.byteOffset), read_len)
        data = bytes(data)
        out["returned_bytes"] = len(data)
        nf = min(len(data) // 4, max_floats)
        out["floats"] = [_cfloat(data[i * 4:(i + 1) * 4]) for i in range(nf)]
        out["u32s"] = [int.from_bytes(data[i * 4:(i + 1) * 4], "little")
                       for i in range(nf)]
        out["hex"] = _hexdump(data[:256], max_rows=16)

    _replay_invoke(work)
    return _text_result(out)


def get_event_usage(args):
    _require_capture()
    rid_arg = args["resource_id"]
    eid = args.get("event_id")
    usage = []

    def work(ctrl):
        if eid is not None:
            ctrl.SetFrameEvent(int(eid), True)
        rid = _resolve_rid(ctrl, rid_arg)
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


_opened_capture = None  # {capture_id, frame_number, timestamp} of the
                        # live-capture entry opened via open_capture /
                        # capture_and_open (REQ-08/REQ-11)


def current_status():
    """Cheap, exception-safe status dict for list_instances /
    get_current_state. No replay-thread round-trip: only reads UI state, so
    it is safe to call from broker/socket worker threads at any time."""
    out = {}
    if _instance_id:
        out["instance"] = _instance_id
    loaded = False
    if _ctx is not None:
        try:
            loaded = bool(_ctx.IsCaptureLoaded())
        except Exception:
            loaded = False
        try:
            fn = str(_ctx.GetCaptureFilename())
            if fn:
                out["capture_filename"] = fn
                frame = _frame_number_from_filename(fn)
                if frame is not None:
                    out["capture_frame_number"] = frame
        except Exception:
            pass
        try:
            ev = int(_ctx.CurEvent())
            out["selected_event"] = ev
            out["current_event"] = ev
        except Exception:
            out["selected_event"] = None
            out["current_event"] = None
    out["capture_loaded"] = loaded
    # opened_capture: which live-capture entry is open in the UI (null
    # instead of a missing key, per contract); only meaningful when loaded
    oc = None
    if loaded and _opened_capture is not None:
        oc = {"capture_id": _opened_capture.get("capture_id"),
              "frame_number": _opened_capture.get("frame_number"),
              "timestamp": _opened_capture.get("timestamp")}
    out["opened_capture"] = oc
    if _conn is not None:
        try:
            out["app_connected"] = bool(_conn.IsConnected())
        except Exception:
            out["app_connected"] = None
        try:
            out["num_captures"] = len(_conn.GetCaptures())
        except Exception:
            pass
    out["capture_connected"] = out.get("app_connected")
    out["captures_available"] = out.get("num_captures")
    return out


def get_current_state(args):
    out = current_status()
    if not out.get("capture_loaded"):
        out["note"] = ("No capture is loaded. Use launch_app + "
                       "trigger_capture + list_captures + open_capture, or "
                       "open a capture in the UI.")
    return _text_result(out)


def shutdown_instance(args):
    """Spoke side of the broker's shutdown_instance: request a graceful
    close of THIS qrenderdoc (main-window close scheduled on the UI
    thread). Returns immediately; the broker waits for process exit and
    force-kills if the graceful path does not finish in time."""
    out = {"pid": os.getpid(), "graceful_requested": False}
    if _ctx is None:
        out["note"] = "extension context not available; broker will kill"
        return _text_result(out)
    try:
        widget = _ctx.GetMainWindow().Widget()
    except Exception:
        widget = None
    closer = getattr(widget, "close", None) if widget is not None else None
    if callable(closer):
        try:
            _ctx.InvokeOntoUIThread(lambda c=closer: c())
            out["graceful_requested"] = True
            out["note"] = ("main window close requested on the UI thread; "
                           "if a save prompt blocks it the broker force-"
                           "kills after its grace period")
        except Exception as e:
            out["note"] = "could not schedule window close: %s" % e
    else:
        out["note"] = ("no closeable window handle available (PySide2 not "
                       "exposed); broker will kill the process")
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
    # REQ-10: report the "Automatically capture child processes" toggle
    # (CaptureOptions.hookIntoChildren) so agents know whether grandchild
    # processes will be captured. 'unknown' when the API doesn't expose it.
    try:
        info["child_capture_hint"] = bool(s.options.hookIntoChildren)
    except Exception:
        info["child_capture_hint"] = "unknown"
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
        raise ToolError(_NO_CONN_HINT)
    delay = float(args.get("delay_seconds", 0) or 0)
    num = int(args.get("num_frames", 1) or 1)
    _conn.TimedCapture(delay, num)
    return _text_result({"instance": _instance_id, "queued": True,
                         "delay_seconds": delay, "num_frames": num,
                         "note": "poll list_captures until the capture appears"})


def _opened_capture_hint(frame_number):
    """Best-effort: when a capture was opened manually in the UI (not via
    our tools) we can still flag it by matching the frame number embedded
    in qrenderdoc's temp capture filename (..._frame123.rdc)."""
    try:
        fn = str(_ctx.GetCaptureFilename())
        return fn and ("_frame%d" % int(frame_number)) in fn
    except Exception:
        return False


def capture_and_open(args):
    """One-shot: TimedCapture -> poll for the new capture -> OpenCapture ->
    poll until loaded -> return an action summary. Collapses the usual
    trigger_capture / list_captures / open_capture / get_current_state
    polling loop into a single call."""
    if _conn is None:
        # same message as trigger_capture so callers can match on it
        raise ToolError(_NO_CONN_HINT)
    delay = float(args.get("delay_seconds", 0) or 0)
    num = int(args.get("num_frames", 1) or 1)
    poll = max(float(args.get("poll_interval_seconds", 0.5) or 0.5), 0.05)
    timeout = max(float(args.get("timeout_seconds", 30) or 30), 1.0)
    actions_summary = max(int(args.get("actions_summary", 10) or 10), 0)

    existing = set()
    try:
        for c in _conn.GetCaptures():
            existing.add(int(c.captureID))
    except Exception:
        pass
    _conn.TimedCapture(delay, num)

    # phase 1: wait for a new capture (diff against the pre-trigger set;
    # pick the newest timestamp when several appear)
    deadline = time.time() + timeout
    chosen = None
    while True:
        time.sleep(poll)
        try:
            caps = _conn.GetCaptures()
        except Exception as e:
            raise ToolError("Lost the capture connection while waiting for "
                            "the capture: %s" % e)
        for c in caps:
            cid = int(c.captureID)
            if cid in existing:
                continue
            ts = str(c.timestamp)
            if chosen is None or (ts, cid) > (chosen[1], chosen[0]):
                chosen = (cid, ts, int(c.frameNumber), str(c.api))
        if chosen is not None or time.time() >= deadline:
            break
    if chosen is None:
        raise ToolError("No new capture appeared within %.1fs (waited "
                        "%.1fs). Check that the program is still connected "
                        "(get_current_state -> app_connected) and that it "
                        "presents frames." % (timeout, timeout))
    cid, ts, frame_no, api = chosen

    # phase 2: open it and wait until the capture is ready in the UI
    open_deadline = time.time() + timeout
    _conn.OpenCapture(cid)
    while True:
        time.sleep(poll)
        st = current_status()
        if st.get("capture_loaded"):
            break
        if time.time() >= open_deadline:
            raise ToolError("Capture %d (timestamp %s) opened but not ready "
                            "within %.1fs (waited %.1fs). Poll "
                            "get_current_state manually."
                            % (cid, ts, timeout, timeout))
    global _opened_capture
    _opened_capture = {"capture_id": cid, "frame_number": frame_no,
                       "timestamp": ts}

    # phase 3: action summary of the loaded capture
    out_actions = []
    num_actions = [0]

    def work(ctrl):
        flat = []
        _walk_flat(ctrl.GetRootActions(), flat)
        num_actions[0] = len(flat)
        for a in flat[:actions_summary]:
            entry = {"event_id": a.eventId, "flags": str(a.flags)}
            name = a.customName
            entry["name"] = name if name else "EID %d" % a.eventId
            try:
                if a.flags & rd.ActionFlags.Drawcall:
                    entry["numIndices"] = a.numIndices
                    entry["numInstances"] = a.numInstances
            except Exception:
                pass
            out_actions.append(entry)

    _replay_invoke(work)
    out = {"capture_id": cid, "frame_number": frame_no, "timestamp": ts,
           "api": api, "num_actions": num_actions[0],
           "first_actions": out_actions}
    if _instance_id:
        out["instance"] = _instance_id
    out["note"] = ("capture_id is only unique per connection and restarts "
                   "at 0 for every launched program - when older captures "
                   "with the same id may exist, tell them apart by "
                   "timestamp (reported above).")
    return _text_result(out)


def list_captures(args):
    """List captures made on the active capture connection."""
    if _conn is None:
        raise ToolError(_NO_CONN_HINT)
    out = {"instance": _instance_id,
           "connected": bool(_conn.IsConnected())}
    try:
        out["apis"] = [str(a) for a in _conn.GetAPIs()]
    except Exception:
        pass
    caps = []
    loaded = False
    try:
        loaded = bool(_ctx.IsCaptureLoaded())
    except Exception:
        loaded = False
    for c in _conn.GetCaptures():
        entry = {"capture_id": int(c.captureID),
                 "frame_number": int(c.frameNumber),
                 "api": str(c.api),
                 "timestamp": str(c.timestamp)}
        # best-effort 'opened' flag: our own open tracking matches by
        # capture id; a capture opened manually in the UI is matched by
        # frame number while a capture is loaded
        opened = False
        if _opened_capture is not None and \
                _opened_capture.get("capture_id") == int(c.captureID):
            opened = True
        elif loaded and _opened_capture is None and \
                _opened_capture_hint(int(c.frameNumber)):
            opened = True
        entry["opened"] = opened
        caps.append(entry)
    out["captures"] = caps
    out["captures_total"] = len(caps)
    return _text_result(out)


def open_capture(args):
    """Open a captured frame from the connection into the RenderDoc UI."""
    if _conn is None:
        raise ToolError(_NO_CONN_HINT)
    cid = int(args["capture_id"])
    # validate up front so a wrong id gets a helpful listing instead of
    # silently opening nothing (capture ids are only unique per connection
    # and restart at 0 - use timestamps to tell same-id frames apart)
    found = None
    try:
        for c in _conn.GetCaptures():
            if int(c.captureID) == cid:
                found = c
                break
    except Exception:
        pass
    if found is None:
        avail = []
        try:
            for c in _conn.GetCaptures():
                avail.append({"capture_id": int(c.captureID),
                              "timestamp": str(c.timestamp),
                              "frame_number": int(c.frameNumber)})
        except Exception:
            pass
        raise ToolError("No capture with id %d on the current connection. "
                        "Available captures: %s (capture ids restart at 0 "
                        "for each launched program - match timestamps, not "
                        "ids, when older captures may be listed)."
                        % (cid, avail))
    _conn.OpenCapture(cid)
    global _opened_capture
    _opened_capture = {"capture_id": cid,
                       "frame_number": int(found.frameNumber),
                       "timestamp": str(found.timestamp)}
    return _text_result({"instance": _instance_id, "opened": cid,
                         "frame_number": int(found.frameNumber),
                         "timestamp": str(found.timestamp),
                         "note": "capture is loading into the UI; poll "
                                 "get_current_state until capture_loaded"})


def open_capture_file(args):
    """REQ-02: load a .rdc from disk into this instance's UI (replacing any
    open capture) - the analysis path for manually captured frames.
    CaptureContext.LoadCapture must run on the UI thread (it shows the load
    progress dialog), so it is queued via InvokeOntoUIThread and the load is
    then polled from this (broker socket) thread."""
    if _ctx is None:
        raise ToolError("Extension not initialized yet.")
    path = str(args.get("path") or "").strip().strip('"')
    if not path:
        raise ToolError("path is required (candidates: list_capture_files).")
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.isfile(path):
        raise ToolError("Capture file not found: %s. Enumerate candidates "
                        "with list_capture_files." % path)
    try:
        if bool(_ctx.IsCaptureLoading()):
            raise ToolError("A capture is already loading in this instance; "
                            "poll get_current_state until capture_loaded "
                            "before loading another.")
    except ToolError:
        raise
    except Exception:
        pass
    out = {"opened": path, "filename": os.path.basename(path)}
    if _instance_id:
        out["instance"] = _instance_id
    fn = _frame_number_from_filename(path)
    if fn is not None:
        out["frame_number"] = fn
    already = False
    try:
        cur = str(_ctx.GetCaptureFilename() or "")
        already = bool(_ctx.IsCaptureLoaded()) and \
            os.path.normcase(os.path.abspath(cur)) == os.path.normcase(path)
    except Exception:
        pass
    if already:
        out["capture_loaded"] = True
        out["note"] = "that file is already open in this instance"
        return _text_result(out)
    load_exc = []

    def _do_load():
        try:
            # temporary=False => no save/delete prompt when it is closed
            _ctx.LoadCapture(path, rd.ReplayOptions(), path, False, True)
        except BaseException as e:  # marshalled back by BlockInvoke-like glue
            load_exc.append(str(e))

    try:
        _ctx.InvokeOntoUIThread(_do_load)
    except Exception as e:
        raise ToolError("Could not schedule LoadCapture on the UI thread: %s"
                        % e)
    try:
        wait = float(args.get("wait_seconds", 120) or 0)
    except (TypeError, ValueError):
        wait = 120.0
    out["wait_seconds"] = wait
    if wait <= 0:
        out["capture_loaded"] = None
        out["note"] = ("loading queued; poll get_current_state until "
                       "capture_loaded")
        return _text_result(out)
    deadline = time.time() + wait
    grace = time.time() + 8.0  # UI thread may still be closing the old capture
    saw_loading = False
    while True:
        time.sleep(0.25)
        loaded = loading = False
        try:
            loaded = bool(_ctx.IsCaptureLoaded())
            loading = bool(_ctx.IsCaptureLoading())
        except Exception:
            pass
        if loaded:
            out["capture_loaded"] = True
            break
        if load_exc:
            raise ToolError("LoadCapture failed: %s" % load_exc[0])
        if loading:
            saw_loading = True
        elif saw_loading:
            # the load ran but no capture is loaded: opening failed and
            # RenderDoc shows an error dialog
            raise ToolError(
                "Load attempt finished but no capture is loaded (bad or "
                "unsupported .rdc?). The RenderDoc UI may show an error "
                "dialog - close it manually.")
        if time.time() >= deadline:
            out["capture_loaded"] = False
            out["note"] = ("still loading after %.0fs (or the load is stuck "
                           "behind an error dialog in the RenderDoc UI); "
                           "poll get_current_state" % wait)
            return _text_result(out)
        if not saw_loading and time.time() >= grace:
            out["capture_loaded"] = False
            out["note"] = ("load did not start within 8s (the UI thread may "
                           "be busy); poll get_current_state")
            return _text_result(out)
    try:
        out["capture_filename"] = str(_ctx.GetCaptureFilename())
    except Exception:
        pass
    out["note"] = "capture loaded; the analysis tools are ready"
    return _text_result(out)


def close_capture(args):
    """Remove a capture from the live-capture connection (RenderDoc's
    per-capture removal API is CaptureConnection.DeleteCapture; there is no
    per-capture 'Close'). Deletes the temp capture without prompting."""
    if _conn is None:
        raise ToolError(_NO_CONN_HINT)
    cid = int(args["capture_id"])
    found = None
    try:
        for c in _conn.GetCaptures():
            if int(c.captureID) == cid:
                found = c
                break
    except Exception:
        pass
    if found is None:
        raise ToolError("No capture with id %d on the current connection "
                        "(it may already have been deleted). Use "
                        "list_captures for the current list." % cid)
    _conn.DeleteCapture(cid, False)
    global _opened_capture
    if _opened_capture is not None and \
            _opened_capture.get("capture_id") == cid:
        _opened_capture = None
    return _text_result({"instance": _instance_id, "closed": cid,
                         "frame_number": int(found.frameNumber),
                         "timestamp": str(found.timestamp),
                         "note": "capture %d removed from the connection "
                                 "(temp capture deleted without prompting)"
                                 % cid})


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
    "get_buffer_regions": get_buffer_regions,
    "buffer_search": buffer_search,
    "get_vertex_data": get_vertex_data,
    "get_constant_buffer": get_constant_buffer,
    "get_event_usage": get_event_usage,
    "get_debug_messages": get_debug_messages,
    "launch_app": launch_app,
    "trigger_capture": trigger_capture,
    "list_captures": list_captures,
    "open_capture": open_capture,
    "open_capture_file": open_capture_file,
    "close_capture": close_capture,
    "capture_and_open": capture_and_open,
    "get_texture_stats": get_texture_stats,
    "compare_regions": compare_regions,
    "shutdown_instance": shutdown_instance,
}
