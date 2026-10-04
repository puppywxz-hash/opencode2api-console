# -*- coding: utf-8 -*-
"""OA2A codex 兼容中间层（Responses 协议适配器）。

为什么需要它
------------
opencode2api 网关的 `/v1/responses` 会把 tools 数组原样转给 zen 上游，而 zen 上游对
工具 schema 的校验远比 OpenAI 官方严格。codex（wire_api="responses"）默认会带上一批
**非function 类型**的工具，实测会被上游逐个拒绝：

    tools[6]  type=custom       name=apply_patch -> `custom` tools are not supported on this endpoint
    tools[11] type=tool_search                   -> 'required' ... Missing 'limit'.
    tools[12] type=web_search                    -> `tools[].search_content_types` is only
                                                  supported for web_search_preview tools.

而同一套工具走 `/v1/chat/completions`（WorkBuddy 用的协议）完全正常，所以表现为
"WorkBuddy 好用、codex 一律报错"。

本模块做的事
------------
在网关前面做一个**只改写不改语义**的适配层，专治 codex 一家：

1. **丢弃**不被支持的非 function 工具（custom / tool_search / web_search /
   local_shell / computer_use_* / code_interpreter / mcp ...），并如实记录日志；
2. 对保留下来的 function 工具**补齐 `required` 覆盖全部 properties**
   （zen 要求 required 是数组且含每个 key，codex 习惯只标必填参数）；
3. 其余请求/响应**字节级透传**，SSE / chunked / gzip 不重组；
4. 其它协议（chat/completions 等）与其它客户端（WorkBuddy）**完全不受影响**。

设计原则：宁可不放行，也不臆造工具。被丢弃的工具会在控制台日志里列出来，
让用户清楚 codex 的 apply_patch / web_search 在这条链路上不可用。

实现要点（踩过的坑，勿改）
--------------------------
* **不要用 http.server.BaseHTTPRequestHandler**：它会自动写状态行/Server/Date头，
  与"原样透传"冲突，且会和我们手写的响应头打架。裸 socket 收发才干净。
* **必须真正支持 keep-alive，不能"每请求一关"**：网关(Go)对 HTTP/1.1 默认
  keep-alive，客户端（codex）据此复用连接；而旧实现处理完一个请求就
  `close()`，客户端下一条请求写到已关闭的 socket 上，就会报
  `error sending request` / `stream disconnected before completion`。
  现在 `conn` 的生命周期由 `_handle` 持有（循环调用 `_serve_once`），响应头
  原样转发，由客户端自己决定是否复用。
* **响应头只在 `\r\n\r\n` 之前的 header 区动手，绝不碰 body**：任何"整段追加"
  的写法都会破坏 chunked 分帧（早期踩过的坑）。
* 请求侧也要删掉 `Connection` / `Keep-Alive` / `Transfer-Encoding`，避免与
  我们重算的 `Content-Length` 冲突。
* 后端 socket 超时要 >= 上游最坏耗时（网关自身 retry 可达 300s），否则慢请求被掐断。
* 客户端 socket 要 settimeout(None) 或足够大，不能让 wfile 写入超时。
"""
import json
import os
import re
import socket
import sys
import threading
import time

__all__ = ["start_shim", "stop_shim", "shim_is_running", "fix_tools_payload",
           "analyze", "shim_stats", "reset_stats", "native_responses_models",
           "DEFAULT_FRONT", "DEFAULT_BACK"]

ALLOWED_TYPE = "function"

DEFAULT_FRONT = "127.0.0.1:18000"
DEFAULT_BACK = "127.0.0.1:8080"

# 客户端没给输出上限时的默认值。codex 的 Responses 请求不带 max_output_tokens，
# 不设的话上游免费模型会按自己的小上限把回答掐断（实测会断在半句）。
# 4096 足够日常对话/工具调用，又不会大到触发上游"超出模型上限"的 400。
_DEFAULT_MAX_TOKENS = 4096

# ---------------------------------------------------------------- 可选文件追踪
# 打包成 exe 后是窗口程序，stdout 看不见，排障时把 _handle 的关键步骤写盘。
# 默认关闭：在 exe 同目录放一个空的 shim_debug.on 即开启，日志为 shim_trace.log。
_TRACE_ON = False
_TRACE_PATH = ""


def _trace_init():
    global _TRACE_ON, _TRACE_PATH
    try:
        base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        base = ""
    _TRACE_PATH = os.path.join(base, "shim_trace.log")
    _TRACE_ON = os.path.exists(os.path.join(base, "shim_debug.on"))


def _dbg(msg):
    if not _TRACE_ON:
        return
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


_trace_init()

# 读超时：请求头/体用短超时，响应体用长超时
_HDR_TIMEOUT = 120
_BODY_TIMEOUT = 900

# 这些请求头不转发（由我们自己重算或与逐字节转发冲突）
#注意：host 必须去掉！客户端的 Host 是 shim 自己的地址，
# 若原样转发再叠加我们补的 Host，Go 严格解析器会因**重复 Host 头**直接
# 在语法层返回 `400 Bad Request`（text/plain），根本进不到应用层。
_SKIP_REQ_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "te", "trailer",
    "upgrade", "proxy-authorization", "proxy-authenticate", "content-length",
    "host",
}


# ----------------------------------------------------------------- 工具清洗
def _fix_one(t):
    """修正单个工具的 schema。返回 True 表示该工具可用。"""
    if not isinstance(t, dict):
        return False
    ttype = t.get("type", ALLOWED_TYPE)
    if ttype != ALLOWED_TYPE:
        return False
    # 提取嵌套的 function 定义（{"type":"function","function":{...}} 形态）
    inner = t.get("function") if isinstance(t.get("function"), dict) else None
    work = inner if inner is not None else t
    name = work.get("name") if isinstance(work, dict) else None
    if not name:
        # 无名工具：zen 上游要求 function 工具必须有 name，丢弃更稳妥
        # （社区标准做法也是 drops nameless tools before forwarding）
        return False
    if inner is not None and inner.get("type", ALLOWED_TYPE) != ALLOWED_TYPE:
        return False
    params = work.get("parameters")
    if params is None:
        return True
    if not isinstance(params, dict):
        return False
    if not isinstance(params.get("properties"), dict):
        params["properties"] = {}
        params.setdefault("type", "object")
        return True
    props = params["properties"]
    req = params.get("required")
    if not isinstance(req, list):
        req = []
    missing = [k for k in props if k not in req]
    if missing:
        params["required"] = req + missing
    return True


def fix_tools_payload(payload):
    """就地修正 Responses 请求体里的 tools，返回 (payload, report)。"""
    report = {"total": 0, "kept": 0, "dropped": [], "required_fixed": [],
              "repaired": []}
    if not isinstance(payload, dict):
        return payload, report
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return payload, report

    report["total"] = len(tools)
    kept = []
    for idx, t in enumerate(tools):
        if not isinstance(t, dict):
            report["dropped"].append({"index": idx, "type": "?",
                                      "name": "(malformed)"})
            continue
        ttype = t.get("type", ALLOWED_TYPE)
        name = t.get("name") or (t.get("function") or {}).get("name") or ""
        before = json.dumps(t, sort_keys=True, ensure_ascii=False)
        if not _fix_one(t):
            report["dropped"].append({"index": idx, "type": ttype, "name": name})
            continue
        after = json.dumps(t, sort_keys=True, ensure_ascii=False)
        if after != before:
            report["repaired"].append({"index": idx, "name": name})
            pr = t.get("parameters")
            if isinstance(pr, dict):
                rp = pr.get("required")
                props = pr.get("properties") or {}
                if isinstance(rp, list) and set(rp) >= set(props.keys()):
                    report["required_fixed"].append(
                        {"index": idx, "name": name, "required": rp})
        kept.append(t)

    payload["tools"] = kept
    report["kept"] = len(kept)
    return payload, report


def analyze(payload):
    """只体检不改写，供控制台「诊断」使用。"""
    rep = {"total": 0, "kept": 0, "dropped": [], "issues": []}
    if not isinstance(payload, dict):
        return rep
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return rep
    rep["total"] = len(tools)
    for i, t in enumerate(tools):
        if not isinstance(t, dict):
            rep["issues"].append("tools[%d] 不是对象" % i)
            continue
        ttype = t.get("type", ALLOWED_TYPE)
        if ttype != ALLOWED_TYPE:
            rep["dropped"].append(
                {"index": i, "type": ttype,
                 "name": t.get("name") or (t.get("function") or {}).get("name") or ""})
            continue
        params = t.get("parameters") or (t.get("function") or {}).get("parameters")
        if isinstance(params, dict) and isinstance(params.get("properties"), dict):
            props = list(params["properties"].keys())
            req = params.get("required")
            if not isinstance(req, list):
                rep["issues"].append("tools[%d] %s: required 不是数组"
                                     % (i, t.get("name", "")))
            else:
                miss = [p for p in props if p not in req]
                if miss:
                    rep["issues"].append("tools[%d] %s: required 缺 %s"
                                         % (i, t.get("name", ""), ",".join(miss)))
    rep["kept"] = rep["total"] - len(rep["dropped"])
    return rep


# ----------------------------------------------------------------- 裸 socket
def _recv_until(conn, marker, limit=8 * 1024 * 1024):
    """读到marker 出现为止，返回 (累积字节, marker 之后剩余的字节)。"""
    buf = b""
    while marker not in buf:
        if len(buf) > limit:
            raise ValueError("request head too large")
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    if marker not in buf:
        raise ValueError("incomplete request head")
    head, rest = buf.split(marker, 1)
    return head + marker, rest


def _read_body(conn, headers, rest):
    """读请求体，返回 (body, 剩余未消费的字节)。

    踩坑记录（勿改）
    ----------------
    * **请求头之后的字节已经躺在 `rest` 里，必须优先从 `rest` 取**。旧实现在
      chunked 分支里直接 `conn.recv(1)` 找 chunk 大小行，结果对已经到达的数据
      死等；而且 chunked 分支**从头到尾没有给 `body` 赋值**，body 永远是空串，
      最终以上游收到 `Content-Length: 0` 的空请求收场（对 /v1/responses 表现为
      上游 400、客户端 0 字节）。
    * 因此统一用一个带缓冲的读取器：先吃 `rest`，不够了才去 socket 上收。
    """
    te = (headers.get("transfer-encoding") or "").lower()
    clen = int(headers.get("content-length") or 0)
    buf = rest

    def take_line():
        nonlocal buf
        while b"\r\n" not in buf:
            c = conn.recv(1)
            if not c:
                break
            buf += c
        line, _, buf = buf.partition(b"\r\n")
        return line

    def take_exact(n):
        nonlocal buf
        while len(buf) < n:
            c = conn.recv(65536)
            if not c:
                break
            buf += c
        data, buf = buf[:n], buf[n:]
        return data

    if "chunked" in te:
        body = b""
        while True:
            line = take_line()
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                break
            if size == 0:
                while True:
                    if not take_line():
                        break
                break
            body += take_exact(size + 2)[:size]  # 丢掉尾部 CRLF
        return body, buf
    if clen > 0:
        return take_exact(clen), buf
    return buf, b""


def _force_close_header(head):
    """已弃用：不再强制 `Connection: close`（原因见 _serve_once 里的说明）。

    保留仅为向后兼容；现在靠 `_handle` 主动支持 keep-alive 来根治 stale 连接问题。
    本函数只在 header 区替换/补一个 Connection 头，body（含 chunked 帧）一字不动。
    """
    if head.endswith(b"\r\n\r\n"):
        core = head[:-4]
    else:
        core = head
    lines = core.split(b"\r\n")
    out = []
    replaced = False
    for ln in lines:
        if ln[:12].lower() == b"connection:":
            out.append(b"Connection: close")
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(b"Connection: close")
    return b"\r\n".join(out) + b"\r\n\r\n"


# --------------------------------------------------- 协议路由与 Responses↔Chat 翻译
# 默认所有模型都走 chat 翻译（绕开网关坏的 /v1/responses 转换层）；
# 仅当模型在 native_responses 集合里时才原样透传 Responses。
_PROTO = {"native_responses": set(), "force_tool_choice": set(),
          "auto_continue": True, "auto_continue_max": 2}


def _proto_load():
    global _PROTO
    try:
        base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(base, "codex_protocol.json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            _PROTO["native_responses"] = set(d.get("native_responses") or [])
            _PROTO["force_tool_choice"] = set(d.get("force_tool_choice") or [])
            _PROTO["auto_continue"] = bool(d.get("auto_continue", True))
            _PROTO["auto_continue_max"] = int(d.get("auto_continue_max", 2))
    except Exception:
        pass


_proto_load()


# 会话连续性：response_id -> chat messages 历史
_STORE = {}
_STORE_MAX = 800


def _store_put(rid, msgs):
    _STORE[rid] = msgs
    if len(_STORE) > _STORE_MAX:
        try:
            _STORE.pop(next(iter(_STORE)))
        except Exception:
            pass


def _gen_rid():
    import uuid
    return "resp_" + uuid.uuid4().hex


def _sse_event(obj):
    ev = obj.get("type", "message")
    return ("event: %s\ndata: %s\n\n"
            % (ev, json.dumps(obj, ensure_ascii=False))).encode("utf-8")


def _resp_obj(model, prev_id, new_id, status, output):
    return {
        "id": new_id,
        "status": status,
        "model": model,
        "object": "response",
        "previous_response_id": prev_id,
        "output": output,
        "created_at": int(time.time()),
    }


def _conv_content_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        t = part.get("type")
        if t in ("input_text", "output_text", "text"):
            parts.append(part.get("text", ""))
        elif t == "refusal":
            parts.append(part.get("refusal", ""))
    return "".join(parts)


def _cap_text(s, cap=8000, head=6000, tail=2000):
    """超长文本截断（保留头尾）。工具输出动辄几十 KB，是把请求撑爆的大头。"""
    if not isinstance(s, str) or len(s) <= cap:
        return s
    return ("%s\n...[中间省略 %d 字符，工具输出过长已截断]...\n%s"
            % (s[:head], len(s) - head - tail, s[-tail:]))


def _window_messages(msgs, max_msgs=150, max_chars=500000):
    """历史超限时的**结构化压缩**：不舍弃叙事，只舍弃细节。

    踩坑记录（必看）
    ----------------
    codex 无状态模式（store:false）每轮把**全部历史**重发一遍。长任务跑到
    几百条消息后，单次请求 0.5~1.6MB（实测 mimo 会话 631 条/1.6MB），
    免费模型上下文根本装不下，模型"看不全"就陷入无限工具调用、不输出文字
    （实测一个会话连打 100+ 次"已运行命令"、47 分钟不收敛）。

    压缩策略（按信息价值分级）：
    * 必保：system + **第一条 user 消息（任务定义）**——丢了它模型就不知道
      自己在干什么，这是大忌；
    * 尾窗：最近 max_msgs 条原样保留（切点落在 user/system 边界，防止把
      assistant(tool_calls) 与其 tool 结果拆开——拆了上游直接 400）；
    * 中段：只保留用户的话与助手的**文字结论**，工具调用与输出细节全部舍弃
      （它们是体积大头，时效一过就没价值）。不做 LLM 摘要——额外请求烧
      免费额度还有失败模式，结构化压缩已够用。
    """
    def size(m):
        try:
            return len(json.dumps(m, ensure_ascii=False))
        except Exception:
            return len(str(m))

    total = sum(size(m) for m in msgs)
    if len(msgs) <= max_msgs and total <= max_chars:
        return msgs

    # ---- 尾窗起点（切在 user/system 边界）----
    start = max(1, len(msgs) - max_msgs)
    while start < len(msgs) and msgs[start].get("role") not in ("user", "system"):
        start += 1  # 向后找 user/system 边界，宁多丢不少留
    if start >= len(msgs):
        start = max(1, len(msgs) - max_msgs)

    # ---- 头部：system + 任务定义（第一条 user）----
    head = []
    if msgs and msgs[0].get("role") == "system":
        head.append(msgs[0])
    task_idx = None
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            task_idx = i
            break
    if task_idx is not None and task_idx < start:
        tu = dict(msgs[task_idx])
        tu["content"] = _cap_text(_conv_content_text(tu.get("content")), 4000)
        head.append(tu)

    # ---- 中段压缩：user 的话 + 助手文字结论，工具细节舍弃 ----
    mid_start = (task_idx + 1) if (task_idx is not None and task_idx < start) else 1
    mid = []
    for m in msgs[mid_start:start]:
        role = m.get("role")
        if role == "user":
            um = dict(m)
            um["content"] = _cap_text(_conv_content_text(um.get("content")), 4000)
            mid.append(um)
        elif role == "assistant":
            txt = m.get("content")
            if isinstance(txt, str) and txt.strip():
                # 只保留文字结论；tool_calls 键必须去掉（没有配对的 tool 消息会被上游拒）
                mid.append({"role": "assistant",
                            "content": _cap_text(txt, 1500, 1200, 300)})
        # tool / reasoning / 其他 -> 舍弃
    tail = msgs[start:]

    trimmed = list(head)
    trimmed.append({"role": "system",
                    "content": "（注意：更早的对话历史已压缩——保留了任务目标、"
                               "用户关键指令与各阶段文字结论，中间的工具调用"
                               "与输出细节已省略；请基于现有信息继续任务）"})
    trimmed.extend(mid)
    trimmed.extend(tail)
    # 兜底：压缩后仍超限就再丢中段前半
    while sum(size(m) for m in trimmed) > max_chars and mid:
        mid = mid[len(mid) // 2:]
        trimmed = list(head) + [trimmed[1]] + mid + tail
    _dbg("window: %d msgs/%dB -> %d msgs/%dB (mid=%d, cut at %d)"
         % (len(msgs), total, len(trimmed),
            sum(size(m) for m in trimmed), len(mid), start))
    return trimmed


def _tool_output_to_text(out):
    """把 function_call_output 的 output 展平成纯文本。

    踩坑记录（必看）
    ----------------
    新版 codex 的工具结果 `output` 不一定是字符串，可能是**部件数组**，如
        [{"type":"input_text","text":"..."}]
    直接塞进 chat 消息的 content，上游会报
        [UNKNOWN_PART_TYPE] unknown content part type 'input_text'
    然后整个请求失败——用户看到的现象是"显示思考，然后毫无回应"。
    所以这里必须展平成纯字符串，绝不能让部件对象透传给上游。
    """
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts = []
        for p in out:
            if isinstance(p, dict):
                if isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif isinstance(p.get("content"), (str, list, dict)):
                    parts.append(_conv_content_text(p.get("content")))
                else:
                    parts.append(json.dumps(p, ensure_ascii=False))
            else:
                parts.append(str(p))
        joined = "".join(parts)
        return joined if joined else json.dumps(out, ensure_ascii=False)
    if isinstance(out, dict):
        for k in ("text", "output", "content"):
            v = out.get(k)
            if isinstance(v, str):
                return v
            if isinstance(v, (list, dict)):
                return _tool_output_to_text(v)
        return json.dumps(out, ensure_ascii=False)
    return str(out)


def _conv_items_to_messages(items, skip_developer):
    """Responses input items -> chat messages（增量，不含 system）。"""
    msgs = []
    pending = None
    for it in items:
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        if t == "message" or (t is None and it.get("role")):
            role = it.get("role")
            if role == "developer":
                if skip_developer:
                    continue
                role = "system"
            text = _conv_content_text(it.get("content"))
            msgs.append({"role": role, "content": text})
            pending = msgs[-1] if role == "assistant" else None
        elif t == "function_call":
            name = it.get("name", "")
            args = it.get("arguments", "") or ""
            cid = it.get("call_id") or ("call_%d" % len(msgs))
            tc = {"id": cid, "type": "function",
                  "function": {"name": name, "arguments": args}}
            if pending is not None:
                pending.setdefault("tool_calls", []).append(tc)
            else:
                msgs.append({"role": "assistant", "content": None,
                             "tool_calls": [tc]})
                pending = msgs[-1]
        elif t == "function_call_output":
            cid = it.get("call_id") or ""
            # output 可能是部件数组（新版 codex），必须展平成纯文本，
            # 否则上游报 [UNKNOWN_PART_TYPE] 直接拒绝整个请求
            out = _cap_text(_tool_output_to_text(it.get("output")))
            msgs.append({"role": "tool", "tool_call_id": cid, "content": out})
            pending = None
        else:
            continue
    return msgs


def _looks_like_full_history(items):
    """判断 input 是不是"全量历史"。

    codex 带 store:false（实测请求体里有这个字段）时走无状态模式：
    每轮都把**完整对话历史**塞进 input，首条通常是 developer/system 消息。
    此时绝不能再把 _STORE 里的历史拼在前面——否则历史翻倍，
    请求体一轮比一轮大（实测 118KB -> 545KB），上游直接被撑爆。
    """
    if not items:
        return False
    first = items[0] if isinstance(items[0], dict) else {}
    return first.get("role") in ("developer", "system")


def _build_messages(payload, prev_id):
    instructions = payload.get("instructions")
    inp = payload.get("input")
    items = (inp if isinstance(inp, list)
             else ([inp] if isinstance(inp, str) else []))
    # 增量模式：带了 previous_response_id 且 input 不是全量历史 -> 用存储的历史拼接
    if (prev_id and prev_id in _STORE
            and not _looks_like_full_history(items)):
        base = list(_STORE[prev_id])
        new_msgs = _conv_items_to_messages(items, skip_developer=False)
        return _window_messages(base + new_msgs)
    # 全量/无状态模式：input 本身就是完整历史，直接重建
    msgs = []
    if instructions:
        msgs.append({"role": "system", "content": instructions})
        skip_dev = True
    else:
        skip_dev = False
    msgs.extend(_conv_items_to_messages(items, skip_developer=skip_dev))
    return _window_messages(msgs)


def _wrap_tool(t):
    """Responses 扁平 function 工具 -> Chat 的 {function:{...}} 形态。"""
    if not isinstance(t, dict):
        return None
    inner = t.get("function") if isinstance(t.get("function"), dict) else t
    name = inner.get("name")
    if not name:
        return None
    params = inner.get("parameters")
    if not isinstance(params, dict):
        params = {"type": "object", "properties": {}}
    return {"type": "function", "function": {
        "name": name,
        "description": inner.get("description", ""),
        "parameters": params,
    }}


def _build_chat_payload(payload, messages):
    model = payload.get("model", "")
    chat = {"model": model, "messages": messages, "stream": True}
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        wrapped = [_wrap_tool(t) for t in tools]
        wrapped = [w for w in wrapped if w]
        if wrapped:
            chat["tools"] = wrapped
            chat["tool_choice"] = payload.get("tool_choice", "auto")
    if "temperature" in payload:
        chat["temperature"] = payload["temperature"]
    if "top_p" in payload:
        chat["top_p"] = payload["top_p"]
    # max_output_tokens 必须给默认值：codex 的 Responses 请求实测**不带**这个字段，
    # 若原样透传成"无 max_tokens"，上游免费模型会用它自己的小默认值，
    # 结果回答被掐断在半句（用户实测："先看看当前项目状态，然后去" 就断了）。
    mo = payload.get("max_output_tokens") or payload.get("max_completion_tokens")
    chat["max_tokens"] = int(mo) if mo else _DEFAULT_MAX_TOKENS
    if "parallel_tool_calls" in payload:
        chat["parallel_tool_calls"] = payload["parallel_tool_calls"]
    if isinstance(payload.get("reasoning"), dict):
        chat["reasoning"] = payload["reasoning"]
    if model in _PROTO.get("force_tool_choice", set()):
        chat["tool_choice"] = "required"
    return chat


def _post_chat(back, chat_payload, auth):
    body = json.dumps(chat_payload, ensure_ascii=False).encode("utf-8")
    bh, bp = back
    s = socket.create_connection((bh, bp), timeout=30)
    fwd = [
        b"POST /v1/chat/completions HTTP/1.1",
        ("Host: %s:%d" % (bh, bp)).encode(),
        b"Content-Type: application/json",
        ("Content-Length: %d" % len(body)).encode(),
        b"Accept: text/event-stream",
        b"Connection: close",
    ]
    if auth:
        fwd.append(("Authorization: %s" % auth).encode())
    s.sendall(b"\r\n".join(fwd) + b"\r\n\r\n" + body)
    return s


def _body_segments(up, init, chunked):
    """从上游 socket 读响应体，yield **已解帧**的字节段。

    踩坑记录（必看，勿改回去）
    --------------------------
    网关的 SSE 响应带 `Transfer-Encoding: chunked`（实测确认，体以
    `1db\\r\\ndata: {...` 开头），一个 SSE data 行经常被从中间切开。
    早期实现直接按 \\n 切行解析，跨帧的行**全部丢失**（实测 92 块丢 21 块），
    表现为：
    * 回答断在半句（文本块被丢）
    * finish_reason='tool_calls' 却一个工具调用都没收到（tool_calls=0），
      codex 只看到文本就结束回合，用户必须手动说"继续"
    离线测试没暴露是因为 fake_chat 不发 chunked 帧。必须先解帧再按行解析。
    """
    if not chunked:
        if init:
            yield init
        while True:
            try:
                c = up.recv(65536)
            except OSError:
                return
            if not c:
                return
            yield c
        return
    raw = init
    while True:
        # 读帧大小行（hex\r\n）
        while b"\r\n" not in raw:
            try:
                c = up.recv(65536)
            except OSError:
                return
            if not c:
                return
            raw += c
        size_line, raw = raw.split(b"\r\n", 1)
        try:
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            # 理论上不该发生：不是合法分帧就原样吐出并退化为直读
            yield size_line + b"\r\n" + raw
            while True:
                try:
                    c = up.recv(65536)
                except OSError:
                    return
                if not c:
                    return
                yield c
            return
        if size == 0:
            return
        while len(raw) < size:
            try:
                c = up.recv(65536)
            except OSError:
                if raw:
                    yield raw
                return
            if not c:
                if raw:
                    yield raw
                return
            raw += c
        yield raw[:size]
        raw = raw[size:]
        if raw.startswith(b"\r\n"):
            raw = raw[2:]


def _norm_usage(u):
    """把 usage 规范成 Responses 格式，缺的字段一律补 0。

    踩坑记录（必看）：codex 解析 response.completed 时 usage 是**必填**，
    且要求 input_tokens / input_tokens_details.cached_tokens /
    output_tokens / output_tokens_details.reasoning_tokens / total_tokens
    一个都不能少——缺了就报
        failed to parse ResponseCompleted: missing field `input_tokens`
    而网关的统计块常常只有 cost 没有 usage，所以**必须无条件**给全。
    """
    if not isinstance(u, dict):
        u = {}
    in_tok = u.get("input_tokens", u.get("prompt_tokens")) or 0
    out_tok = u.get("output_tokens", u.get("completion_tokens")) or 0
    try:
        in_tok, out_tok = int(in_tok), int(out_tok)
    except (TypeError, ValueError):
        in_tok = out_tok = 0
    in_det = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
    out_det = (u.get("output_tokens_details")
               or u.get("completion_tokens_details") or {})
    if not isinstance(in_det, dict):
        in_det = {}
    if not isinstance(out_det, dict):
        out_det = {}
    total = u.get("total_tokens")
    try:
        total = int(total) if total is not None else in_tok + out_tok
    except (TypeError, ValueError):
        total = in_tok + out_tok
    return {
        "input_tokens": in_tok,
        "input_tokens_details": {
            "cached_tokens": int(in_det.get("cached_tokens") or 0)},
        "output_tokens": out_tok,
        "output_tokens_details": {
            "reasoning_tokens": int(out_det.get("reasoning_tokens") or 0)},
        "total_tokens": total,
    }


def _dump_failed_chat(chat_payload, reason):
    """上游拒绝请求时把请求体存盘，方便离线分析（放 exe 同目录）。"""
    try:
        base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(base, "failed_chat.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"reason": str(reason), "chat": chat_payload}, f,
                      ensure_ascii=False, indent=2)
        _dbg("出错的请求体已保存 -> %s" % p)
    except Exception:
        pass


def _read_chat_sse(up, timeout, init=b"", chunked=True):
    up.settimeout(timeout)
    nchunk = nfail = 0

    def parse(line):
        nonlocal nchunk, nfail
        line = line.rstrip(b"\r")
        if not line.startswith(b"data:"):
            return None
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return None
        nchunk += 1
        if _TRACE_ON and (b"tool_call" in line or nchunk % 80 == 0
                          or len(data) < 200):
            _dbg("chat chunk#%d: %s" % (nchunk, data[:300]))
        try:
            return json.loads(data.decode("utf-8"))
        except Exception as e:
            nfail += 1
            if _TRACE_ON:
                _dbg("chat chunk#%d PARSE FAIL: %s | raw=%s"
                     % (nchunk, e, data[:300]))
            return None

    buf = b""
    for seg in _body_segments(up, init, chunked):
        buf += seg
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            d = parse(line)
            if d is not None:
                yield d
    if buf:
        for line in buf.split(b"\n"):
            d = parse(line)
            if d is not None:
                yield d
    if _TRACE_ON:
        _dbg("chat stream end: chunks=%d parse_fail=%d" % (nchunk, nfail))


def _status_code(head):
    try:
        return int(head.split(b"\r\n")[0].decode("latin-1").split(" ")[1])
    except Exception:
        return 0


def _extract_err(body):
    try:
        d = json.loads(body.decode("utf-8", "replace"))
        e = d.get("error") or {}
        return e.get("message") or str(d)[:200]
    except Exception:
        return body.decode("utf-8", "replace")[:200]


def _write_head(conn, ctype="text/event-stream"):
    conn.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: " + ctype.encode() + b"\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: close\r\n\r\n")


def _emit_error(conn, model, prev_id, new_id, msg):
    resp = _resp_obj(model, prev_id, new_id, "completed", [{
        "type": "message", "id": "msg_" + new_id, "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text",
                     "text": "[opencode2api 错误] %s" % msg}]}])
    resp["error"] = None
    resp["incomplete_details"] = None
    resp["usage"] = _norm_usage(None)
    _write_head(conn)
    for ev in ({"type": "response.created", "response": resp},
               {"type": "response.in_progress", "response": resp},
               {"type": "response.completed", "response": resp}):
        conn.sendall(_sse_event(ev))


_AUTO_CONT_RE = __import__("re").compile(
    r"(先|首先|然后|接着|接下来|我会|让我|我将|准备|即将|下一步|再查|再读|"
    r"现在开始|then\\b|first\\b|next\\b|let me|i'll|i will|going to)",
    re.IGNORECASE)
_AUTO_CONT_NUDGE = (
    "（系统提醒）你上一条消息只描述了打算做什么，回合就结束了，任务并未完成。"
    "请立即调用工具实际执行你刚才描述的操作；"
    "如果整个任务确实已完成，请直接给出最终结果，不要再描述计划。")


def _translate_and_respond(conn, back, payload, model, headers, auth):
    """把 codex 的 Responses 请求翻译成 chat/completions，再把响应翻回 Responses SSE。

    含**自动续跑**：免费模型常"只描述不执行"就干净收尾（finish_reason=stop
    且零工具调用，文本里全是"我先/然后/接下来"），codex 只能干等或手动说继续。
    此时 shim 自动注入一条继续指令再调一次，把结果接在同一条流里——
    最多续 _PROTO["auto_continue_max"] 次（默认 2），codex_protocol.json 可配。
    """
    prev_id = payload.get("previous_response_id")
    messages = _build_messages(payload, prev_id)
    base_chat = _build_chat_payload(payload, messages)
    new_id = _gen_rid()
    _dbg("translate: model=%s prev_id=%r messages=%d chat_body=%dB store=%r"
         % (model, prev_id, len(messages),
            len(json.dumps(base_chat, ensure_ascii=False)),
            payload.get("store")))

    seq = [0]
    stats = {"usage": None}

    def ev(obj):
        seq[0] += 1
        obj["sequence_number"] = seq[0]
        return _sse_event(obj)

    def send(obj):
        try:
            conn.sendall(ev(obj))
        except OSError:
            _dbg("client write FAILED at event %s (seq=%d)"
                 % (obj.get("type"), seq[0]))
            raise

    oi_box = [0]

    def take_oi():
        oi_box[0] += 1
        return oi_box[0] - 1

    _write_head(conn)
    resp0 = _resp_obj(model, prev_id, new_id, "in_progress", [])
    send({"type": "response.created", "response": resp0})
    send({"type": "response.in_progress", "response": resp0})

    output = []
    fatal = None
    cont_used = 0
    max_cont = _PROTO.get("auto_continue_max", 2)
    if not _PROTO.get("auto_continue", True):
        max_cont = 0
    chat = base_chat
    up = None
    round_no = 0
    last_finish = None
    last_tools = 0
    total_text = 0
    try:
        while True:
            round_no += 1
            try:
                up = _post_chat(back, chat, auth)
            except OSError as e:
                fatal = "无法连到网关：%s" % e
                up = None
                break
            try:
                up_head, up_rest = _recv_until(up, b"\r\n\r\n")
            except (ValueError, OSError) as e:
                fatal = "网关无响应：%s" % e
                break
            _dbg("chat upstream status=%d (round %d)"
                 % (_status_code(up_head), round_no))
            # 响应体是 chunked 分帧时必须先解帧再按行解析（详见 _body_segments）
            chunked = "chunked" in up_head.decode("latin-1").lower()
            if _status_code(up_head) != 200:
                err = _extract_err(up_rest)
                _dump_failed_chat(chat, "HTTP %s: %s"
                                  % (_status_code(up_head), err))
                try:
                    while True:
                        c = up.recv(65536)
                        if not c:
                            break
                        err = _extract_err(c)
                except Exception:
                    pass
                fatal = err
                break

            # ---- 流式收集本轮 ----
            text_buf = ""
            msg_open = False
            msg_id = "msg_%s_r%d" % (new_id, round_no)
            tool_items = []
            finish_reason = None

            for d in _read_chat_sse(up, _BODY_TIMEOUT, init=up_rest,
                                    chunked=chunked):
                if not isinstance(d, dict):
                    continue
                choices = d.get("choices") or []
                if not choices:
                    if d.get("usage"):
                        stats["usage"] = d["usage"]
                    if d.get("error"):
                        # 上游报错（如 [UNKNOWN_PART_TYPE]）不能再静默吞掉
                        stats["upstream_error"] = _extract_err(
                            json.dumps(d).encode("utf-8"))
                        _dump_failed_chat(chat, stats["upstream_error"])
                        break
                    continue
                delta = choices[0].get("delta") or {}
                if choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]
                t = delta.get("content")
                if t:
                    if not msg_open:
                        msg_open = True
                        m_oi = take_oi()
                        send({"type": "response.output_item.added",
                              "output_index": m_oi, "item": {
                                  "type": "message", "id": msg_id,
                                  "role": "assistant",
                                  "status": "in_progress",
                                  "content": [{"type": "output_text",
                                               "text": ""}]}})
                        send({"type": "response.content_part.added",
                              "item_id": msg_id, "output_index": m_oi,
                              "content_index": 0,
                              "part": {"type": "output_text", "text": ""}})
                    text_buf += t
                    send({"type": "response.output_text.delta",
                          "item_id": msg_id, "output_index": m_oi,
                          "content_index": 0, "delta": t})
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    while len(tool_items) <= idx:
                        tool_items.append({
                            # id=本响应内 item id（fc_...），call_id=上游调用 id
                            # （call_...）；codex 靠 call_id 回传工具结果。
                            "id": "fc_%s_%d_%d" % (new_id, round_no,
                                                   len(tool_items)),
                            "call_id": "", "name": "", "args": "",
                            "oi": take_oi(), "added": False})
                    cur = tool_items[idx]
                    if tc.get("id") and not cur["call_id"]:
                        cur["call_id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        # 名称可能分多片到达，必须累加（只取第一片=截断的工具名）
                        cur["name"] += fn["name"]
                    a = fn.get("arguments")
                    if a:
                        if not cur["added"]:
                            # added 延迟到第一个参数分片时发，此时名称已收完
                            cur["added"] = True
                            send({"type": "response.output_item.added",
                                  "output_index": cur["oi"], "item": {
                                      "type": "function_call",
                                      "id": cur["id"],
                                      "call_id": cur["call_id"],
                                      "status": "in_progress",
                                      "name": cur["name"],
                                      "arguments": ""}})
                        cur["args"] += a
                        send({"type": "response.function_call_arguments.delta",
                              "item_id": cur["id"],
                              "output_index": cur["oi"], "delta": a})
            # ---- 收尾本轮 item ----
            if msg_open:
                send({"type": "response.output_text.done",
                      "item_id": msg_id, "output_index": m_oi,
                      "content_index": 0, "text": text_buf})
                send({"type": "response.content_part.done",
                      "item_id": msg_id, "output_index": m_oi,
                      "content_index": 0,
                      "part": {"type": "output_text", "text": text_buf}})
                send({"type": "response.output_item.done",
                      "item_id": msg_id, "output_index": m_oi, "item": {
                          "type": "message", "id": msg_id, "role": "assistant",
                          "status": "completed",
                          "content": [{"type": "output_text",
                                       "text": text_buf}]}})
            named = [c for c in tool_items if c["name"]]
            for cur in named:
                if not cur["added"]:
                    # 参数为空的工具调用：added 拖到现在才发
                    cur["added"] = True
                    send({"type": "response.output_item.added",
                          "output_index": cur["oi"], "item": {
                              "type": "function_call", "id": cur["id"],
                              "call_id": cur["call_id"],
                              "status": "in_progress", "name": cur["name"],
                              "arguments": ""}})
                send({"type": "response.function_call_arguments.done",
                      "item_id": cur["id"], "output_index": cur["oi"],
                      "arguments": cur["args"]})
                send({"type": "response.output_item.done",
                      "item_id": cur["id"], "output_index": cur["oi"],
                      "item": {"type": "function_call", "id": cur["id"],
                               "call_id": cur["call_id"],
                               "status": "completed", "name": cur["name"],
                               "arguments": cur["args"]}})
            # ---- 本轮产物归档 ----
            if msg_open:
                output.append({"type": "message", "id": msg_id,
                               "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text",
                                            "text": text_buf}]})
            for cur in named:
                output.append({"type": "function_call", "id": cur["id"],
                               "call_id": cur["call_id"],
                               "status": "completed", "name": cur["name"],
                               "arguments": cur["args"]})
            last_finish, last_tools = finish_reason, len(named)
            total_text += len(text_buf)
            amsg = {"role": "assistant",
                    "content": text_buf if text_buf else None}
            if named:
                amsg["tool_calls"] = [
                    {"id": c["call_id"] or c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"]}}
                    for c in named]
            messages.append(amsg)
            # ---- 自动续跑判定：只说不做 -> 注入继续指令，同一条流接着流 ----
            if (finish_reason == "stop" and not named
                    and cont_used < max_cont
                    and _AUTO_CONT_RE.search(text_buf or "")):
                cont_used += 1
                _dbg("auto-continue #%d/%d: 模型只说不做(%r)，注入继续指令"
                     % (cont_used, max_cont, (text_buf or "")[:60]))
                messages.append({"role": "user", "content": _AUTO_CONT_NUDGE})
                chat = dict(base_chat)
                chat["messages"] = _window_messages(list(messages))
                try:
                    up.close()
                except OSError:
                    pass
                continue
            break
    except OSError as e:
        _dbg("translate stream error: %s" % e)
        try:
            resp = _resp_obj(model, prev_id, new_id, "incomplete", output)
            resp["error"] = None
            resp["incomplete_details"] = {"reason": "stream_error"}
            resp["usage"] = _norm_usage(stats.get("usage"))
            conn.sendall(_sse_event({"type": "response.completed",
                                     "response": resp}))
        except Exception:
            pass
    else:
        # 上游报错/致命问题透传给 codex（否则界面只会"思考"然后毫无回应）
        up_err = stats.get("upstream_error")
        if fatal and not up_err:
            up_err = fatal
        if up_err:
            err_item = {"type": "message", "id": "err_" + new_id,
                        "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text",
                                     "text": "[opencode2api 上游错误] %s"
                                             % up_err}]}
            e_oi = take_oi()
            send({"type": "response.output_item.added",
                  "output_index": e_oi,
                  "item": dict(err_item, status="in_progress")})
            send({"type": "response.output_item.done",
                  "item_id": err_item["id"], "output_index": e_oi,
                  "item": err_item})
            output.append(err_item)
            _dbg("上游错误已透传: %s" % up_err[:150])
        resp = _resp_obj(model, prev_id, new_id, "completed", output)
        # 对齐原生 completed：error/incomplete_details/usage（usage 必填）
        resp["error"] = None
        resp["incomplete_details"] = None
        resp["usage"] = _norm_usage(stats.get("usage"))
        send({"type": "response.completed", "response": resp})
        _dbg("turn done: rounds=%d finish=%r text_total=%d tools=%d cont=%d"
             % (round_no, last_finish, total_text, last_tools, cont_used))
    finally:
        if up is not None:
            try:
                up.close()
            except OSError:
                pass
    # 存历史（含各轮 assistant 与续跑指令，供 previous_response_id 串联）
    _store_put(new_id, messages)
    return False


def _handle(conn, back, verbose, stats, lock):
    """一个客户端连接的生命周期：支持在同一条连接上连续处理多个请求（keep-alive）。

    踩坑记录（勿改）
    ----------------
    旧实现处理完第一个请求就在 finally 里 `shutdown + close`，等于"每请求一关"。
    客户端（codex 等）若按 HTTP/1.1 复用连接，第二次就会写到已关闭的 socket 上，
    报 `error sending request` / `stream disconnected before completion`。
    现在由 _handle 持有连接、_serve_once 处理单请求，直到对端关闭才收尾。
    """
    try:
        while True:
            conn.settimeout(_HDR_TIMEOUT)
            if not _serve_once(conn, back, verbose, stats, lock):
                break
    except Exception:
        import traceback
        _dbg("HANDLER EXCEPTION\n" + traceback.format_exc())
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


def _serve_once(conn, back, verbose, stats, lock):
    """处理一条请求。返回 True 表示连接可继续复用，False 表示应当关闭连接。"""
    up = None
    try:
        head, rest = _recv_until(conn, b"\r\n\r\n")
        _dbg("收到请求头 %d 字节: %s"
             % (len(head), head.split(b"\r\n")[0][:100].decode("latin-1")))
        lines = head.split(b"\r\n")
        if not lines or not lines[0]:
            _dbg("请求行空（对端已关闭）")
            return False
        req_line = lines[0].decode("latin-1")
        parts = req_line.split(" ")
        if len(parts) < 2:
            _dbg("请求行无法解析：%r" % req_line[:80])
            return False
        method, path = parts[0], parts[1]

        headers = {}
        for ln in lines[1:]:
            if not ln:
                continue
            k, _, v = ln.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()

        # 读body（chunked / Content-Length 统一处理，见 _read_body）
        _dbg("req %s %s: 读 body" % (method, path))
        body, _ = _read_body(conn, headers, rest)
        _dbg("body 解析完成 %d 字节" % len(body))

        # 是不是要改写的 responses 请求
        rewrite = path.split("?")[0].rstrip("/").endswith("/responses") and body
        auth = headers.get("authorization", "")
        report = None
        model = ""
        payload = None
        if rewrite:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                model = str(payload.get("model", ""))
                fixed, report = fix_tools_payload(payload)
                if report["dropped"] or report["required_fixed"]:
                    body = json.dumps(fixed, ensure_ascii=False).encode("utf-8")
                _record(stats, lock, model, report, verbose)
                # 路由：默认走 chat 翻译（绕开网关坏的 /v1/responses 转换层）；
                # 仅当模型在 native_responses 集合里才原样透传 Responses。
                if model and model not in _PROTO.get("native_responses", set()):
                    return _translate_and_respond(conn, back, payload, model,
                                                 headers, auth)

        # 连接上游
        _dbg("连接上游 %s:%d" % back)
        try:
            up = socket.create_connection(back, timeout=30)
            _dbg("上游连接成功")
        except OSError as e:
            msg = json.dumps({"error": {"message":
                                        "shim cannot reach gateway: %s" % e,
                                        "type": "shim_error"}}).encode("utf-8")
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n"
                         b"Content-Type: application/json\r\n"
                         b"Content-Length: " + str(len(msg)).encode() + b"\r\n"
                         b"Connection: close\r\n\r\n" + msg)
            return True

        fwd = [("%s %s HTTP/1.1" % (method, path)).encode()]
        for k, v in headers.items():
            if k in _SKIP_REQ_HEADERS:
                continue
            fwd.append(("%s: %s" % (k, v)).encode("latin-1"))
        fwd.append(("Host: %s:%d" % (back[0], back[1])).encode())
        if body:
            fwd.append(("Content-Length: %d" % len(body)).encode())
        else:
            fwd.append(b"Content-Length: 0")
        up.sendall(b"\r\n".join(fwd) + b"\r\n\r\n" + body)
        _dbg("已转发 %d 字节给上游" % (len(b"\r\n".join(fwd)) + 4 + len(body)))

        # 读上游响应头（先分离 header 与已到达的 body 开头），原样倒回客户端。
        try:
            up.settimeout(_BODY_TIMEOUT)
            up_head, up_rest = _recv_until(up, b"\r\n\r\n")
            _dbg("上游响应头 %d 字节: %s"
                 % (len(up_head), up_head.split(b"\r\n")[0][:80].decode("latin-1")))
        except (ValueError, OSError) as e:
            _dbg("上游响应头读取失败 %s: %s" % (type(e).__name__, e))
            return True
        # 响应头原样回写，**不再强制 Connection: close**。
        #
        # 为什么改回去：_handle 现在真正支持 keep-alive（同一个连接上连续处理多
        # 个请求，见 _handle 的踩坑记录），所以"每请求一关"已经不是必需的了；反之
        # 强制 close 会让按 HTTP/1.1 复用连接的客户端（codex 就是）每次都开新连接，
        # 而"旧实现每请求一关 + codex 复用"正是 `error sending request` 的来源。
        # 想强制 close 的场景请让客户端在请求里自己带 Connection: close。
        _dbg("响应头原样转发")
        conn.settimeout(_BODY_TIMEOUT)
        try:
            conn.sendall(up_head)
            if up_rest:
                conn.sendall(up_rest)
            while True:
                chunk = up.recv(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
            _dbg("本条响应转发完毕，等待连接上是否有下一个请求")
            return True
        except OSError as e:
            _dbg("客户端写入失败 %s: %s" % (type(e).__name__, e))
            return False
    except Exception:
        # 注意：窗口程式的 stdout 不可见，异常必须同时写盘，否则排障时一片沉默
        import traceback
        _dbg("SERVE EXCEPTION\n" + traceback.format_exc())
        return False
    finally:
        if up is not None:
            try:
                up.close()
            except OSError:
                pass


def _record(stats, lock, model, report, verbose):
    if stats is None:
        return
    with lock:
        stats["count"] = stats.get("count", 0) + 1
        stats["last_model"] = model
        stats["last_dropped"] = report["dropped"]
        stats["last_required_fixed"] = report["required_fixed"]
        if report["dropped"] or report["required_fixed"]:
            bits = []
            if report["dropped"]:
                bits.append("丢弃非function工具 %d 个(%s)" % (
                    len(report["dropped"]),
                    ",".join("%s:%s" % (d["type"], d["name"] or "-")
                             for d in report["dropped"][:6])))
            if report["required_fixed"]:
                bits.append("补齐required %d 个" % len(report["required_fixed"]))
            line = "[shim] %s -> %s" % (model or "?", "; ".join(bits))
            stats["last_log"] = line
            if verbose:
                _out(line)


def _out(line):
    """把一行日志写到 stdout；**窗口版 exe 里 sys.stdout 是 None，绝不能裸写**。

    踩坑记录（必看，勿改回去）
    --------------------------
    PyInstaller 打成窗口程序(console=False)后 `sys.stdout is None`。
    旧代码在这里直接 `sys.stdout.write(...)`，于是**每个真实请求都会抛
    AttributeError** → `_serve_once` 整体崩溃 → 连接被直接关掉、客户端收到
    0 字节 → codex 报
        `stream disconnected before completion: error sending request`
    这正是"服务明明在跑、codex 却首条必失败"的真凶。
    所以：写 stdout 必须先判空，写失败也要吞掉，绝不能让日志把请求搞崩。
    """
    try:
        out = sys.stdout
        if out is not None:
            out.write(line + "\n")
            out.flush()
            return
    except Exception:
        pass
    _dbg(line)


def shim_stats():
    return dict(_STATS)


def reset_stats():
    with _LOCK:
        _STATS.clear()


_STATE = {"srv": None, "thread": None}
_STATS = {}
_LOCK = threading.Lock()


def start_shim(front=DEFAULT_FRONT, back=DEFAULT_BACK, verbose=True,
               stats=None):
    """启动中间层，返回 (ok, message)。"""
    stop_shim()
    try:
        fh, fp = (front.split(":") if isinstance(front, str) else front)[:2]
        bh, bp = (back.split(":") if isinstance(back, str) else back)[:2]
        fp, bp = int(fp), int(bp)
    except Exception as e:
        return False, "地址格式错误：%s" % e
    if (fh, fp) == (bh, bp):
        return False, "前端与后端地址不能相同"

    stats_box = stats if stats is not None else _STATS
    _STATS.clear()

    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((fh, fp))
        srv.listen(128)
    except OSError as e:
        return False, "无法监听 %s:%d（%s）" % (fh, fp, e)

    back_t = (bh, bp)
    stop_flag = threading.Event()

    def loop():
        srv.settimeout(0.5)
        while not stop_flag.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=_handle,
                                 args=(conn, back_t, verbose, stats_box, _LOCK),
                                 daemon=True)
            t.start()
        try:
            srv.close()
        except OSError:
            pass

    th = threading.Thread(target=loop, name="oa2a-codex-shim", daemon=True)
    th.start()
    _STATE["srv"] = srv
    _STATE["thread"] = th
    _STATE["stop"] = stop_flag
    time.sleep(0.2)
    return True, "codex 兼容层已启动：%s:%d -> %s:%d" % (fh, fp, bh, bp)


def stop_shim():
    srv = _STATE.get("srv")
    if srv is None:
        return False, "中间层未运行"
    _STATE.get("stop", threading.Event()).set()
    try:
        srv.close()
    except OSError:
        pass
    _STATE["srv"] = None
    _STATE["thread"] = None
    time.sleep(0.2)
    return True, "codex 兼容层已停止"


def shim_is_running():
    return _STATE.get("srv") is not None


def native_responses_models():
    """配置为「原生 Responses 透传」的模型集合（默认空 = 全部走 chat 翻译）。"""
    return set(_PROTO.get("native_responses", set()))
