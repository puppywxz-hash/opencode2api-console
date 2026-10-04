#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb_shim.py — WorkBuddy 专用「抗断流 / 抗上下文爆掉」chat->chat 中继层

为什么需要它（背景）
--------------------
WorkBuddy 把 opencode 模型指向网关 127.0.0.1:8080（opencode2api，上游是
opencode.ai/zen）。但 server.log 反复出现：
    downstream stream ended with an error ... context deadline exceeded
    all upstream attempts failed ... context canceled  -> 502 (duration 30009ms)
即：opencode.ai 上游流在网关层超时/卡死，网关把到客户端的下游流直接掐断，
WorkBuddy 收到半截流就「做一半停」。网关是上游 Go 二进制改不了源码，而
codex_shim（18000）只对 /responses 做翻译，对 chat/completions 只是原样透传，
所以 WorkBuddy 直连网关时完全没享受到那套鲁棒逻辑。

本文件给 WorkBuddy 单独加一层 chat->chat 中继，复用 codex_shim 已验证的原语，
并补三件 chat 路径没有的东西：
  1. 上下文开窗（_window_messages）：长会话历史超限时结构化压缩，防止上下文爆掉
     导致模型「光说不练」/ 跑飞（codex 那次 100+ 命令、47 分钟就是这么来的）。
  2. 上游流中断自动续接：检测到上游流没到 [DONE] 就结束（EOF / 超时），把已生成
     内容回灌为 assistant 消息 + 注入续跑指令，重新 POST；对 WorkBuddy 而言是一条
     无缝连续的流（同一条客户端连接保持打开，内部多发几次上游请求）。
  3. 标准 OpenAI SSE 原样透传：不动格式，WorkBuddy 收到的仍是标准 chat SSE。
  4. 429 限流自动退避：遇 429 按指数退避重试（默认 2s 起、最多 5 次、单次上限
     60s，响应头带 Retry-After 则优先采用），不占用常规重试预算；退避耗尽才报错。

架构：
    WorkBuddy --(chat/completions)--> wb_shim(18090) --> 网关(8080) --> opencode.ai
    WorkBuddy 改指向 127.0.0.1:18090 即可，网关/codex 路径完全不动。

端口可经环境变量覆盖（便于测试）：
    WB_LISTEN=127.0.0.1:18090   WB_GATEWAY=127.0.0.1:8080
"""

import os
import sys
import json
import socket
import threading
import time
import traceback

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def _split(addr, default):
    addr = (addr or default).strip()
    if ":" in addr:
        h, p = addr.rsplit(":", 1)
        return h or "127.0.0.1", int(p)
    return "127.0.0.1", int(addr)

LISTEN = _split(os.environ.get("WB_LISTEN"), "127.0.0.1:18090")
GATEWAY = _split(os.environ.get("WB_GATEWAY"), "127.0.0.1:8080")

MAX_RETRY = int(os.environ.get("WB_MAX_RETRY", "3"))   # 含首次；超过则优雅终止
IDLE_TIMEOUT = float(os.environ.get("WB_IDLE_TIMEOUT", "240"))  # 上游单次读超时(秒)
HDR_TIMEOUT = 30
BODY_TIMEOUT = 900
WIN_MAX_MSGS = int(os.environ.get("WB_WIN_MSGS", "150"))
WIN_MAX_CHARS = int(os.environ.get("WB_WIN_CHARS", "500000"))
CAP_TEXT = int(os.environ.get("WB_CAP", "8000"))

# 429 限流专用指数退避（不占 MAX_RETRY 预算）
R429_MAX = int(os.environ.get("WB_429_MAX", "5"))      # 退避重试上限次数
R429_BASE = float(os.environ.get("WB_429_BASE", "2"))   # 首次退避秒数（指数递增）
R429_CAP = float(os.environ.get("WB_429_CAP", "60"))    # 单次退避上限秒数

_SKIP_REQ_HEADERS = {"host", "content-length", "connection", "transfer-encoding",
                     "proxy-connection", "proxy-authorization"}

_RESUME_PENDING = "\x00PENDING\x00"   # 哨兵：需续跑但无精确文本

_dbg_lock = threading.Lock()

def _dbg(msg):
    try:
        with _dbg_lock:
            ts = time.strftime("%H:%M:%S")
            line = "[%s] %s" % (ts, msg)
            print(line, flush=True)
            base = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(base, "wb_shim.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 运行统计（供 GUI 状态展示）
# ----------------------------------------------------------------------------
_WB_STATS = {"requests": 0, "resumes": 0, "windows": 0, "errors": 0,
             "rate_limited": 0,
             "gateway": "127.0.0.1:8080", "listen": "127.0.0.1:18090"}
_WB_STATS_LOCK = threading.Lock()


def _bump(key, n=1):
    with _WB_STATS_LOCK:
        _WB_STATS[key] = _WB_STATS.get(key, 0) + n


# ----------------------------------------------------------------------------
# 复用自 codex_shim 的已验证原语（chunked 解帧 / SSE 解析 / 开窗 / 截断）
# ----------------------------------------------------------------------------
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


def _cap_text(s, cap=CAP_TEXT, head=6000, tail=2000):
    if not isinstance(s, str) or len(s) <= cap:
        return s
    return ("%s\n...[中间省略 %d 字符，工具输出过长已截断]...\n%s"
            % (s[:head], len(s) - head - tail, s[-tail:]))


def _window_messages(msgs, max_msgs=WIN_MAX_MSGS, max_chars=WIN_MAX_CHARS):
    """历史超限时的结构化压缩（不舍弃叙事，只舍弃细节）。

    策略（滑动窗口，对文本型历史也有效）
    --------------------------------------
    * 必保：system（若有）+ **第一条 user 消息（任务定义）**——丢了它模型就不知道
      自己在干什么，这是大忌；
    * 尾窗：最近 max_msgs 条原样保留（尾窗是消息后缀，天然不会把
      assistant(tool_calls) 与其 tool 结果拆开——拆了上游直接 400）；
    * **中间整段丢弃**，只插一条「历史已压缩」系统说明。
    注意：旧版只丢 tool/reasoning、保留中段文本结论，对「全文本」历史压不动
    （实测 601 条文本历史压完还是 602 条）。本版直接丢弃中段，保证输出量级
    ≈ max_msgs + 2，无论历史是工具型还是文本型都能框住上下文。
    """
    def size(m):
        try:
            return len(json.dumps(m, ensure_ascii=False))
        except Exception:
            return len(str(m))

    total = sum(size(m) for m in msgs)
    if len(msgs) <= max_msgs and total <= max_chars:
        return msgs

    # 滑动窗口：尾窗取最后 max_msgs 条（后缀）
    start = max(0, len(msgs) - max_msgs)

    # 落点推进到 user/system 边界，避免把 assistant(tool_calls) 与其 tool 结果
    # 从中间切开（拆了上游直接 400）。tool 调用对总夹在两条 user 之间，从 user
    # 边界开始必能包含完整配对。
    def _advance_to_boundary(s):
        while s < len(msgs) and msgs[s].get("role") not in ("user", "system"):
            s += 1
        return s

    start = _advance_to_boundary(start)
    if start >= len(msgs):
        # 后缀全是 assistant/tool（无 user/system 可落点）：退回原始窗口再推进；
        # 仍落空则保留最后 max_msgs，但绝不以孤儿 tool 消息开头（见下方兜底）。
        start = max(0, len(msgs) - max_msgs)
        start = _advance_to_boundary(start)

    # 字符上限：从最旧端继续丢。tool 调用对（assistant(tool_calls) + 紧随的 tool
    # 结果）必须成对被丢弃，绝不留下孤儿 tool——否则上游 400：call_id 对不上。
    while sum(size(m) for m in msgs[start:]) > max_chars and (len(msgs) - start) > 1:
        if (len(msgs) - start) >= 2 and msgs[start].get("role") == "assistant" \
           and msgs[start].get("tool_calls") and msgs[start + 1].get("role") == "tool":
            start += 2          # 整对丢弃
        else:
            start += 1
    # 最终兜底：头部若仍是孤儿 tool（异常结构），回退包含其 assistant(tool_calls)
    while start > 0 and msgs[start].get("role") == "tool":
        start -= 1

    tail = msgs[start:]
    # 单条超大消息截断，避免一条撑爆
    tail = [_cap_msg(m) for m in tail]

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

    trimmed = list(head)
    if start > 0:
        trimmed.append({"role": "system",
                        "content": "（注意：更早的对话历史已压缩——仅保留任务目标"
                                   "与最近 %d 条上下文，中间的对话已省略；"
                                   "请基于现有信息继续任务）" % len(tail)})
    trimmed.extend(tail)
    _dbg("window: %d msgs/%dB -> %d msgs/%dB (tail=%d, cut@%d)"
         % (len(msgs), total, len(trimmed),
            sum(size(m) for m in trimmed), len(tail), start))
    return trimmed


def _cap_msg(m):
    """单条消息内容过长时截断（用于尾窗兜底）。"""
    if not isinstance(m, dict):
        return m
    c = m.get("content")
    if isinstance(c, str) and len(c) > CAP_TEXT:
        mm = dict(m)
        mm["content"] = _cap_text(c)
        return mm
    return m



def _recv_until(conn, marker, limit=8 * 1024 * 1024):
    buf = b""
    while marker not in buf:
        if len(buf) > limit:
            raise ValueError("head too large")
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    if marker not in buf:
        raise ValueError("incomplete head")
    head, rest = buf.split(marker, 1)
    return head + marker, rest


def _read_body(conn, headers, rest):
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
            body += take_exact(size + 2)[:size]
        return body, buf
    if clen > 0:
        return take_exact(clen), buf
    return buf, b""


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


def _body_segments(up, init, chunked):
    """从上游 socket 读响应体，yield 已解 chunked 帧的字节段。"""
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


def _retry_after_secs(head):
    """解析 429 响应头的 Retry-After（仅支持秒数形式），失败返回 None。"""
    try:
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"retry-after:"):
                return max(0.0, min(float(line.split(b":", 1)[1].strip()), R429_CAP))
    except Exception:
        return None
    return None


def _open_chat_stream(payload, auth):
    """建立上游 chat 流连接并读响应头；遇 429 自动指数退避重试。

    返回 (up, head, rest, n429)。连接失败抛 OSError，无响应头抛 ValueError。
    429 退避 R429_MAX 次后若仍是 429，返回最后一次响应（由调用方判定失败）。
    退避不消耗调用方的常规重试预算（MAX_RETRY）。
    """
    n429 = 0
    while True:
        s = _post_chat(GATEWAY, payload, auth)
        try:
            head, rest = _recv_until(s, b"\r\n\r\n")
        except (ValueError, OSError):
            try:
                s.close()
            except OSError:
                pass
            raise
        if _status_code(head) != 429 or n429 >= R429_MAX:
            return s, head, rest, n429
        delay = _retry_after_secs(head)
        if delay is None:
            delay = min(R429_CAP, R429_BASE * (2 ** n429))
        try:
            while True:
                c = s.recv(65536)
                if not c:
                    break
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass
        n429 += 1
        _bump("rate_limited")
        _dbg("上游 429 限流 -> 退避 %.1fs（第 %d/%d 次）" % (delay, n429, R429_MAX))
        time.sleep(delay)


# ----------------------------------------------------------------------------
# 响应发送辅助
# ----------------------------------------------------------------------------
def _send_200_sse(conn):
    conn.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\n"
        b"X-Accel-Buffering: no\r\n"
        b"Connection: close\r\n\r\n")


def _send_200_json(conn, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    conn.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: %d\r\n" % len(body)
        + b"Connection: close\r\n\r\n" + body)


def _send_error(conn, status, message):
    obj = {"error": {"message": message, "type": "wb_shim_error"}}
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    try:
        conn.sendall(
            ("HTTP/1.1 %d Error\r\n" % status).encode()
            + b"Content-Type: application/json\r\n"
            + ("Content-Length: %d\r\n" % len(body)).encode()
            + b"Connection: close\r\n\r\n" + body)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# 核心：chat/completions 处理（开窗 + 续接 + 透传）
# ----------------------------------------------------------------------------
def _chat_stream_collect(seg_iter, conn):
    """边透传边解析上游 SSE。返回 dict：
        done        : 是否见到 data:[DONE]
        partial     : 已累计的文本内容（供续接回灌）
        finish      : 最后一个 finish_reason
        client_gone : 客户端写入是否失败（客户端已断开）

    上游 socket 的 OSError（超时/EOF）由 _body_segments 静默终止迭代，
    因此 for 正常结束时若未 done，即为「上游无 [DONE] 而结束」= 需要续接。
    """
    partial = []
    finish_reason = None
    done = False
    client_gone = False
    linebuf = b""
    for seg in seg_iter:
        try:
            conn.sendall(seg)           # 原样透传
        except OSError:
            client_gone = True
            break
        linebuf += seg
        while b"\n" in linebuf:
            line, linebuf = linebuf.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                done = True
                continue
            try:
                j = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            choices = j.get("choices") or []
            if not choices:
                continue
            d = choices[0].get("delta") or {}
            t = d.get("content")
            if isinstance(t, str) and t:
                partial.append(t)
            fr = choices[0].get("finish_reason")
            if fr:
                finish_reason = fr
    return {"done": done, "partial": "".join(partial),
            "finish": finish_reason, "client_gone": client_gone}


def _handle_chat(conn, auth, req):
    """处理一次 chat/completions 请求（可能含多次上游续接）。"""
    model = str(req.get("model", ""))
    client_stream = bool(req.get("stream", False))
    messages = req.get("messages") or []
    _bump("requests")

    # 1) 上下文开窗（仅在超限时压缩）
    orig_n = len(messages)
    messages = _window_messages(messages)
    if len(messages) != orig_n:
        _bump("windows")

    resume_partial = ""
    last_finish = None
    head_sent = False

    for attempt in range(1, MAX_RETRY + 1):
        payload = dict(req)
        if resume_partial:
            # 把中断前已生成内容回灌为 assistant，再注入续跑指令
            if resume_partial != _RESUME_PENDING:
                messages = messages + [{"role": "assistant",
                                        "content": resume_partial}]
            messages = messages + [{
                "role": "user",
                "content": "（上文因上游连接中断被截断，请从中断处继续完成，"
                           "不要重复已输出的内容，直接接着往下做。）"}]
            messages = _window_messages(messages)
            resume_partial = ""
        payload["messages"] = messages
        payload["stream"] = True   # 内部始终走流式，便于检测中断与续接

        up = None
        try:
            up, up_head, up_rest, n429 = _open_chat_stream(payload, auth)
        except OSError as e:
            _dbg("attempt %d: 连网关失败 %s" % (attempt, e))
            if attempt < MAX_RETRY:
                time.sleep(1.5)
                continue
            _fail(conn, client_stream, model,
                  "wb_shim 无法连到网关 %s:%d：%s" % (GATEWAY[0], GATEWAY[1], e))
            return
        except (ValueError, OSError) as e:
            _dbg("attempt %d: 网关无响应头 %s" % (attempt, e))
            if attempt < MAX_RETRY:
                time.sleep(1.5)
                continue
            _fail(conn, client_stream, model, "网关无响应：%s" % e)
            return

        status = _status_code(up_head)
        chunked = "chunked" in up_head.decode("latin-1").lower()
        _dbg("attempt %d: 上游 status=%d model=%s%s"
             % (attempt, status, model,
                ("（429 已退避 %d 次）" % n429) if n429 else ""))

        if status != 200:
            err = _extract_err(up_rest)
            try:
                while True:
                    c = up.recv(65536)
                    if not c:
                        break
            except Exception:
                pass
            if status == 429 and n429 >= R429_MAX:
                # 429 专项退避已耗尽：不再占用常规重试，直接报错
                _fail(conn, client_stream, model,
                      "上游 429 限流（已退避 %d 次仍未恢复）：%s" % (n429, err))
                return
            if attempt < MAX_RETRY:
                _dbg("attempt %d: 上游 %d %s -> 重试" % (attempt, status, err))
                time.sleep(2)
                continue
            _fail(conn, client_stream, model, "上游返回 %d：%s" % (status, err))
            return

        # 200：开始流式透传（头只发一次）
        if client_stream and not head_sent:
            _send_200_sse(conn)
            head_sent = True

        try:
            up.settimeout(IDLE_TIMEOUT)
            r = _chat_stream_collect(_body_segments(up, up_rest, chunked), conn)
        except OSError:
            r = {"done": False, "partial": "", "finish": last_finish,
                 "client_gone": False}

        if r["client_gone"]:
            _dbg("客户端已断开，停止")
            return
        if r["done"]:
            # 上游的 [DONE] 已由 _chat_stream_collect 原样透传，不再补发
            if not client_stream:
                _finish_nonstream(conn, payload, r["partial"], r["finish"])
            _dbg("attempt %d: 完成（done），finish=%s" % (attempt, r["finish"]))
            return
        if r["finish"]:
            # 模型已给出 finish_reason，仅缺 [DONE] 终止符——补一个干净终止
            last_finish = r["finish"]
            if client_stream:
                conn.sendall(b"data: [DONE]\n\n")
            else:
                _finish_nonstream(conn, payload, r["partial"], r["finish"])
            _dbg("attempt %d: 模型已结束但缺 [DONE]，补终止" % attempt)
            return
        # 真正的中断：上游在无 finish / 无 [DONE] 的情况下掐断——续接
        _dbg("attempt %d: 上游流中断（无 [DONE]/无 finish），续接" % attempt)
        _bump("resumes")
        last_finish = r["finish"]
        resume_partial = r["partial"] if r["partial"] else _RESUME_PENDING
        if attempt < MAX_RETRY:
            continue
        _finish_stream(conn, client_stream, model, "上游多次中断，已终止续接")
        return

    _fail(conn, client_stream, model, "超出最大重试次数")


def _finish_nonstream(conn, payload, text, finish_reason):
    msg = {"role": "assistant", "content": text}
    choice = {"index": 0, "message": msg,
              "finish_reason": finish_reason or "stop"}
    obj = {
        "id": "chatcmpl-wbshim",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model", ""),
        "choices": [choice],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    _send_200_json(conn, obj)


def _finish_stream(conn, client_stream, model, note):
    """流模式下向客户端发一条收尾内容 + [DONE]，避免 WorkBuddy 一直挂起。"""
    if not client_stream:
        _send_error(conn, 502, note)
        return
    try:
        chunk = {
            "id": "chatcmpl-wbshim",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0,
                         "delta": {"content": "\n[wb_shim] %s" % note},
                         "finish_reason": "stop"}],
        }
        conn.sendall(("data: %s\n\n" % json.dumps(chunk, ensure_ascii=False)).encode("utf-8"))
        conn.sendall(b"data: [DONE]\n\n")
    except OSError:
        pass


def _fail(conn, client_stream, model, message):
    _dbg("FAIL: %s" % message)
    _bump("errors")
    if client_stream:
        try:
            _send_200_sse(conn)
            chunk = {
                "id": "chatcmpl-wbshim",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0,
                             "delta": {"content": "\n[wb_shim 错误] %s" % message},
                             "finish_reason": "stop"}],
            }
            conn.sendall(("data: %s\n\n" % json.dumps(chunk, ensure_ascii=False)).encode("utf-8"))
            conn.sendall(b"data: [DONE]\n\n")
        except OSError:
            pass
    else:
        _send_error(conn, 502, message)


# ----------------------------------------------------------------------------
# 通用代理（GET /v1/models 等）
# ----------------------------------------------------------------------------
def _proxy_pass_clean(conn, method, path, headers, body, auth):
    up = None
    try:
        bh, bp = GATEWAY
        s = socket.create_connection((bh, bp), timeout=30)
    except OSError as e:
        _send_error(conn, 502, "wb_shim 无法连到网关：%s" % e)
        return
    try:
        fwd = [("%s %s HTTP/1.1" % (method, path)).encode()]
        for k, v in headers.items():
            if k in _SKIP_REQ_HEADERS:
                continue
            fwd.append(("%s: %s" % (k, v)).encode("latin-1"))
        fwd.append(("Host: %s:%d" % (bh, bp)).encode())
        if auth:
            fwd.append(("Authorization: %s" % auth).encode())
        if body:
            fwd.append(("Content-Length: %d" % len(body)).encode())
        else:
            fwd.append(b"Content-Length: 0")
        s.sendall(b"\r\n".join(fwd) + b"\r\n\r\n" + body)
        up_head, up_rest = _recv_until(s, b"\r\n\r\n")
        conn.sendall(up_head)
        if up_rest:
            conn.sendall(up_rest)
        while True:
            c = s.recv(65536)
            if not c:
                break
            conn.sendall(c)
    except OSError as e:
        _dbg("proxy_pass error: %s" % e)
    finally:
        try:
            s.close()
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 连接处理
# ----------------------------------------------------------------------------
def _serve_once(conn):
    try:
        conn.settimeout(HDR_TIMEOUT)
        head, rest = _recv_until(conn, b"\r\n\r\n")
        lines = head.split(b"\r\n")
        if not lines or not lines[0]:
            return False
        parts = lines[0].decode("latin-1").split(" ")
        if len(parts) < 2:
            return False
        method, path = parts[0], parts[1]
        headers = {}
        for ln in lines[1:]:
            if not ln:
                continue
            k, _, v = ln.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        body, _ = _read_body(conn, headers, rest)
        auth = headers.get("authorization", "")
        p = path.split("?")[0].rstrip("/")

        if method == "POST" and p.endswith("/chat/completions"):
            try:
                req = json.loads(body.decode("utf-8"))
            except Exception:
                _send_error(conn, 400, "请求体不是合法 JSON")
                return False
            if not isinstance(req, dict):
                _send_error(conn, 400, "请求体必须是对象")
                return False
            _handle_chat(conn, auth, req)
            return False   # 流式/非流式都已显式结束，关闭连接
        else:
            # GET /v1/models 等：直接代理
            _proxy_pass_clean(conn, method, path, headers, body, auth)
            return False
    except Exception:
        _dbg("SERVE EXCEPTION\n" + traceback.format_exc())
        return False


def _handle_conn(conn):
    try:
        while True:
            if not _serve_once(conn):
                break
    except Exception:
        pass
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 模块级启停 API（供 GUI 同进程线程调用，照搬 codex_shim 的 _STATE 模式）
# ----------------------------------------------------------------------------
_WB_STATE = {"srv": None, "thread": None, "stop": None}


def _serve_loop(srv, stop_flag):
    srv.settimeout(0.5)
    while not stop_flag.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        conn.settimeout(BODY_TIMEOUT)
        t = threading.Thread(target=_handle_conn, args=(conn,), daemon=True)
        t.start()
    try:
        srv.close()
    except OSError:
        pass


def start_wb(front=None, back=None, verbose=True):
    """启动 WorkBuddy 抗断流层，返回 (ok, message)。

    front: 本层监听地址（默认 127.0.0.1:18090）
    back : 转发目标（网关）地址（默认 127.0.0.1:8080）
    GUI 调用：start_wb(front='127.0.0.1:18090', back=listen)
    """
    stop_wb()
    global GATEWAY, LISTEN
    if front:
        try:
            LISTEN = _split(front, "127.0.0.1:18090")
        except Exception as e:
            return False, "监听地址格式错误：%s" % e
    if back:
        try:
            GATEWAY = _split(back, "127.0.0.1:8080")
        except Exception as e:
            return False, "网关地址格式错误：%s" % e
    with _WB_STATS_LOCK:
        _WB_STATS["listen"] = "%s:%d" % LISTEN
        _WB_STATS["gateway"] = "%s:%d" % GATEWAY
    host, port = LISTEN
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(128)
    except OSError as e:
        return False, "无法监听 %s:%d（%s）" % (host, port, e)
    stop_flag = threading.Event()
    _WB_STATE["srv"] = srv
    _WB_STATE["stop"] = stop_flag
    th = threading.Thread(target=_serve_loop,
                          args=(srv, stop_flag),
                          name="oa2a-wb-shim", daemon=True)
    th.start()
    _WB_STATE["thread"] = th
    time.sleep(0.2)
    if verbose:
        _dbg("wb_shim 监听 %s:%d，网关 %s:%d，MAX_RETRY=%d，IDLE_TIMEOUT=%.0fs，"
             "429退避=%d次/%.0fs起/上限%.0fs"
             % (host, port, GATEWAY[0], GATEWAY[1], MAX_RETRY, IDLE_TIMEOUT,
                R429_MAX, R429_BASE, R429_CAP))
    return True, "WorkBuddy 抗断流层已启动：%s:%d -> 网关 %s:%d" % (
        host, port, GATEWAY[0], GATEWAY[1])


def stop_wb():
    srv = _WB_STATE.get("srv")
    if srv is None:
        return False, "WorkBuddy 抗断流层未运行"
    _WB_STATE.get("stop", threading.Event()).set()
    try:
        srv.close()
    except OSError:
        pass
    _WB_STATE["srv"] = None
    _WB_STATE["thread"] = None
    time.sleep(0.2)
    return True, "WorkBuddy 抗断流层已停止"


def wb_is_running():
    return _WB_STATE.get("srv") is not None


def wb_stats():
    with _WB_STATS_LOCK:
        return dict(_WB_STATS)


def main():
    ok, msg = start_wb()
    if not ok:
        print("wb_shim 启动失败：%s" % msg)
        return
    print(msg)
    try:
        while wb_is_running():
            time.sleep(1)
    except KeyboardInterrupt:
        stop_wb()
        _dbg("wb_shim 退出")


if __name__ == "__main__":
    main()
