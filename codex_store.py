# -*- coding: utf-8 -*-
"""
Codex CLI 会话写入模块。

Codex 把会话存为 rollout JSONL:
  ~/.codex/sessions/YYYY/MM/DD/rollout-<本地时间>-<session_id>.jsonl
每行一个对象:
  - 首行 {"timestamp","type":"session_meta","payload":{session_id,id,timestamp,cwd,
          originator,cli_version,source,thread_source,model_provider,base_instructions,
          history_mode,context_window}}
  - 之后 {"timestamp","type":"response_item","payload":{...}}
      message: {type:"message", role:"user|assistant|developer", content:[{type:"input_text|output_text",text}]}
      (还有 reasoning / custom_tool_call / function_call 等, 均可省略)
会话要出现在历史/可 resume, 还需登记:
  - ~/.codex/session_index.jsonl : {"id","thread_name","updated_at"}
  - ~/.codex/history.jsonl       : {"session_id","ts","text"}  (每条用户输入一行)

Codex rollout 无内容寻址、无签名, 因此可以合成写入(与 Cursor store.db 不同)。
"""
import copy
import glob
import json
import os
import sqlite3
import time


def codex_root():
    return os.path.join(os.path.expanduser("~"), ".codex")


def sessions_root():
    return os.path.join(codex_root(), "sessions")


def uuid7(ms, rand):
    """按 UUIDv7 布局构造字符串(48 位毫秒时间戳 + 版本/变体 + 随机)。rand 需 >=10 字节。"""
    b = bytearray(16)
    b[0:6] = int(ms).to_bytes(6, "big")
    b[6] = 0x70 | (rand[0] & 0x0F)
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)
    b[9:16] = rand[3:10]
    h = b.hex()
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def _iso_z(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % (int(t * 1000) % 1000)


def _iso_micros_z(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%06d0Z" % (int(t * 1_000_000) % 1_000_000)


def _load_template_meta():
    """取最新真实 rollout 的 session_meta.payload 作模板(保证与本机 Codex 版本 schema 一致)。"""
    files = sorted(glob.glob(os.path.join(sessions_root(), "**", "rollout-*.jsonl"), recursive=True),
                   key=os.path.getmtime, reverse=True)
    for f in files:
        try:
            first = json.loads(open(f, encoding="utf-8").readline())
        except (ValueError, OSError):
            continue
        if first.get("type") == "session_meta" and isinstance(first.get("payload"), dict):
            return copy.deepcopy(first["payload"])
    return None


def _text_of(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for p in content:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "text":
            parts.append(p.get("text", ""))
        elif t == "thinking" and (p.get("thinking") or "").strip():   # 加密的空思考块不导出
            parts.append("[思考]\n" + p.get("thinking", ""))
        elif t == "tool_use":
            parts.append("[工具调用 %s]\n%s" % (p.get("name"), json.dumps(p.get("input"), ensure_ascii=False)))
        elif t == "tool_result":
            tc = p.get("content")
            parts.append("[工具结果]\n" + (tc if isinstance(tc, str) else json.dumps(tc, ensure_ascii=False)))
    return "\n\n".join(x for x in parts if x and x.strip())


def _role_of(rec):
    return rec.get("role") or (rec.get("message") or {}).get("role") or rec.get("type")


def _message_item(rec):
    role = _role_of(rec)
    text = _text_of((rec.get("message") or {}).get("content"))
    if not text.strip():
        return None
    if role == "assistant":
        crole, ctype = "assistant", "output_text"
    elif role == "user":
        crole, ctype = "user", "input_text"
    else:  # system / tool / developer / 其它
        crole, ctype = "developer", "input_text"
    return {"type": "message", "role": crole, "content": [{"type": ctype, "text": text}]}


def _strip_ext_prefix(p):
    return p[4:] if isinstance(p, str) and p.startswith("\\\\?\\") else p


def discover_codex_sessions():
    """列出 Codex 会话(优先读 state_5.sqlite threads, 带标题/cwd)。"""
    out = []
    db = os.path.join(codex_root(), "state_5.sqlite")
    if os.path.isfile(db):
        try:
            con = sqlite3.connect(db)
            try:
                rows = con.execute(
                    "SELECT id, rollout_path, title, cwd, updated_at_ms, first_user_message "
                    "FROM threads WHERE archived=0 ORDER BY updated_at_ms DESC").fetchall()
            finally:
                con.close()
            for sid, rp, title, cwd, ums, fum in rows:
                if not rp or not os.path.isfile(rp):
                    continue
                try:
                    sz = os.path.getsize(rp)
                except OSError:
                    sz = 0
                out.append({"path": os.path.abspath(rp), "name": title or fum or sid,
                            "dir": os.path.dirname(rp), "cwd": _strip_ext_prefix(cwd or ""),
                            "size": sz, "mtime": (ums or 0) / 1000.0, "session_id": sid})
            if out:
                return out
        except sqlite3.Error:
            pass
    # 回退: 扫描 rollout 文件
    for f in glob.glob(os.path.join(sessions_root(), "**", "rollout-*.jsonl"), recursive=True):
        try:
            st = os.stat(f)
        except OSError:
            continue
        out.append({"path": os.path.abspath(f), "name": os.path.basename(f),
                    "dir": os.path.dirname(f), "cwd": "", "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def is_rollout(path):
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
        return bool(first) and json.loads(first).get("type") == "session_meta"
    except (ValueError, OSError):
        return False


def read_rollout_meta(path):
    try:
        with open(path, encoding="utf-8") as f:
            first = json.loads(f.readline())
        if first.get("type") == "session_meta":
            return first.get("payload", {})
    except (ValueError, OSError):
        pass
    return {}


def _norm_codex_content(content):
    out = []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    for p in content:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t in ("input_text", "output_text", "text"):
            out.append({"type": "text", "text": p.get("text", "")})
        else:
            out.append({"type": "text", "text": json.dumps(p, ensure_ascii=False)})
    return out


def _parse_args(v):
    """function_call.arguments 是 JSON 字符串; 能解析成对象就解析, 否则原样保留。"""
    if isinstance(v, str):
        try:
            j = json.loads(v)
            if isinstance(j, dict):
                return j
        except ValueError:
            pass
    return v


def read_rollout(path):
    """把 Codex rollout 解析为编辑器可渲染的归一化记录(从 response_item 重建对话)。

    保留 call_id(工具调用与结果配对)、时间戳和 turn_context 里的模型名, 供阅读视图使用。
    """
    records = []
    model = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            p = o.get("payload", {})
            if not isinstance(p, dict):
                continue
            if o.get("type") == "turn_context":
                if isinstance(p.get("model"), str) and p["model"]:
                    model = p["model"]
                continue
            if o.get("type") != "response_item":
                continue
            ts = o.get("timestamp")
            pt = p.get("type")
            rec = None
            if pt == "message":
                rec = {"role": p.get("role"),
                       "message": {"content": _norm_codex_content(p.get("content", []))}}
                if p.get("role") == "assistant" and model:
                    rec["message"]["model"] = model
            elif pt in ("custom_tool_call", "function_call", "local_shell_call", "web_search_call"):
                if pt == "local_shell_call":
                    name, inp = "local_shell", (p.get("action") or {})
                elif pt == "web_search_call":
                    name, inp = "web_search", (p.get("action") or {})
                else:
                    name, inp = p.get("name"), _parse_args(p.get("input", p.get("arguments")))
                block = {"type": "tool_use", "name": name, "input": inp}
                if p.get("call_id"):
                    block["id"] = p["call_id"]
                rec = {"role": "assistant", "message": {"content": [block]}}
                if model:
                    rec["message"]["model"] = model
            elif pt in ("custom_tool_call_output", "function_call_output", "local_shell_call_output"):
                o2 = p.get("output")
                if isinstance(o2, list):
                    txt = "\n".join(x.get("text", "") for x in o2 if isinstance(x, dict))
                else:
                    txt = o2 if isinstance(o2, str) else json.dumps(o2, ensure_ascii=False)
                block = {"type": "tool_result", "content": txt}
                if p.get("call_id"):
                    block["tool_use_id"] = p["call_id"]
                rec = {"role": "tool", "message": {"content": [block]}}
            elif pt == "reasoning":
                # 推理正文是加密的; 有摘要就显示摘要, 没有则标成「内容不可见」
                summ = p.get("summary") if isinstance(p.get("summary"), list) else []
                txt = "\n\n".join(x["text"] for x in summ if isinstance(x, dict) and isinstance(x.get("text"), str))
                block = {"type": "thinking", "thinking": txt}
                if not txt.strip():
                    block["redacted"] = True
                    if isinstance(p.get("encrypted_content"), str):
                        block["encrypted_content"] = p["encrypted_content"]   # 密文原文, 可展开查看
                rec = {"role": "assistant", "message": {"content": [block]}}
                if model:
                    rec["message"]["model"] = model
            if rec is not None:
                if ts:
                    rec["timestamp"] = ts
                records.append(rec)
    return records


def _register_thread(session_id, rollout_path, cwd, title, first_user_message, now_ms):
    """往 state_5.sqlite 的 threads 表插一行(这是 `codex resume` 真正读取的索引)。

    以一条真实 user 线程为模板复制所有列, 只覆盖身份/时间/路径等; 失败返回 (False, 原因)。
    """
    db = os.path.join(codex_root(), "state_5.sqlite")
    if not os.path.isfile(db):
        return False, "无 state_5.sqlite(该 Codex 版本索引方式不同)"
    try:
        con = sqlite3.connect(db, timeout=5)
    except sqlite3.Error as e:
        return False, str(e)
    try:
        cols = [c[1] for c in con.execute("PRAGMA table_info(threads)")]
        if not cols:
            return False, "threads 表不存在"
        row = con.execute(
            "SELECT * FROM threads WHERE thread_source='user' ORDER BY updated_at_ms DESC LIMIT 1").fetchone()
        tmpl = dict(zip(cols, row)) if row else {c: None for c in cols}
        cwd_val = cwd
        if cwd_val and not cwd_val.startswith("\\\\?\\"):
            cwd_val = "\\\\?\\" + cwd_val
        override = {
            "id": session_id, "rollout_path": rollout_path, "cwd": cwd_val or "",
            "title": title, "first_user_message": first_user_message, "preview": first_user_message,
            "created_at": now_ms // 1000, "updated_at": now_ms // 1000,
            "created_at_ms": now_ms, "updated_at_ms": now_ms,
            "recency_at": now_ms // 1000, "recency_at_ms": now_ms,
            "tokens_used": 0, "archived": 0, "thread_source": "user",
            "git_sha": None, "git_branch": None, "git_origin_url": None,
            "agent_path": None, "agent_nickname": None, "agent_role": None,
        }
        vals = [override.get(c, tmpl.get(c)) for c in cols]
        con.execute("INSERT OR REPLACE INTO threads (%s) VALUES (%s)"
                    % (",".join(cols), ",".join("?" * len(cols))), vals)
        con.commit()
        return True, None
    except sqlite3.Error as e:
        return False, str(e)
    finally:
        con.close()


def write_codex_session(records, cwd="", title="", rand_bytes=None):
    """把归一化会话写成 Codex rollout, 登记到 threads 索引, 并安全追加 history.jsonl。

    返回 dict: {path, session_id, count, registered, register_error}
    """
    if rand_bytes is None:
        rand_bytes = os.urandom(20)
    now = time.time()
    now_ms = int(now * 1000)
    session_id = uuid7(now_ms, rand_bytes[0:10])

    meta = _load_template_meta() or {
        "originator": "codex_cli", "cli_version": "unknown", "source": "cli",
        "thread_source": "user", "model_provider": "openai",
        "base_instructions": {"text": ""}, "history_mode": "legacy",
    }
    meta["session_id"] = session_id
    meta["id"] = session_id
    meta["timestamp"] = _iso_z(now)
    if cwd:
        meta["cwd"] = cwd
    meta["context_window"] = {"window_id": uuid7(now_ms, rand_bytes[10:20])}

    ts = _iso_z(now)
    lines = [{"timestamp": ts, "type": "session_meta", "payload": meta}]
    user_texts = []
    for rec in records:
        if not isinstance(rec, dict) or rec.get("type") == "turn_ended":
            continue
        item = _message_item(rec)
        if not item:
            continue
        role = item["role"]
        txt = item["content"][0]["text"]
        # 复刻真实每轮结构: 用户 = response_item + user_message 事件;
        # 助手 = agent_message 事件 + response_item(Codex 的对话transcript靠这些事件渲染)
        if role == "user":
            lines.append({"timestamp": ts, "type": "response_item", "payload": item})
            lines.append({"timestamp": ts, "type": "event_msg",
                          "payload": {"type": "user_message", "message": txt, "images": []}})
            user_texts.append(txt)
        elif role == "assistant":
            lines.append({"timestamp": ts, "type": "event_msg",
                          "payload": {"type": "agent_message", "message": txt}})
            lines.append({"timestamp": ts, "type": "response_item", "payload": item})
        else:
            lines.append({"timestamp": ts, "type": "response_item", "payload": item})

    lt = time.localtime(now)
    day_dir = os.path.join(sessions_root(), time.strftime("%Y", lt), time.strftime("%m", lt), time.strftime("%d", lt))
    os.makedirs(day_dir, exist_ok=True)
    fname = "rollout-%s-%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S", lt), session_id)
    path = os.path.abspath(os.path.join(day_dir, fname))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False))
            f.write("\n")

    thread_name = (title or (user_texts[0] if user_texts else "Imported session")).strip().splitlines()[0][:80]
    first_user = (user_texts[0] if user_texts else "").splitlines()[0][:200] if user_texts else ""

    # 关键: 登记到 threads(codex resume 真正读取的索引)
    registered, register_error = _register_thread(session_id, path, cwd, thread_name, first_user, now_ms)

    # 安全追加 history.jsonl(append 模式, 绝不截断)
    try:
        with open(os.path.join(codex_root(), "history.jsonl"), "a", encoding="utf-8", newline="\n") as f:
            for txt in user_texts:
                f.write(json.dumps({"session_id": session_id, "ts": int(now), "text": txt}, ensure_ascii=False) + "\n")
    except OSError:
        pass

    return {"path": path, "session_id": session_id,
            "count": sum(1 for x in lines if x["type"] == "response_item"),
            "registered": registered, "register_error": register_error}
