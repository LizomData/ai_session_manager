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
        elif t == "thinking":
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
