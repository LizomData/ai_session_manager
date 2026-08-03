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


def write_codex_session(records, cwd="", title="", rand_bytes=None):
    """把归一化会话写成 Codex rollout 并登记到 index/history。

    rand_bytes: 用于生成 uuid7 的随机源(>=20 字节); 不传则用 os.urandom。
    返回 dict: {path, session_id, count}
    """
    if rand_bytes is None:
        rand_bytes = os.urandom(20)
    now = time.time()
    now_ms = int(now * 1000)
    session_id = uuid7(now_ms, rand_bytes[0:10])

    # session_meta: 以真实模板为基础覆盖关键字段
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
        lines.append({"timestamp": ts, "type": "response_item", "payload": item})
        if item["role"] == "user":
            user_texts.append(item["content"][0]["text"])

    # rollout 文件路径(本地日期/时间)
    lt = time.localtime(now)
    day_dir = os.path.join(sessions_root(), time.strftime("%Y", lt), time.strftime("%m", lt), time.strftime("%d", lt))
    os.makedirs(day_dir, exist_ok=True)
    fname = "rollout-%s-%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S", lt), session_id)
    path = os.path.join(day_dir, fname)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False))
            f.write("\n")

    # 登记 session_index.jsonl
    thread_name = (title or (user_texts[0] if user_texts else "Imported session")).strip().splitlines()[0][:60]
    with open(os.path.join(codex_root(), "session_index.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"id": session_id, "thread_name": thread_name,
                            "updated_at": _iso_micros_z(now)}, ensure_ascii=False) + "\n")

    # 追加 history.jsonl(每条用户输入一行)
    with open(os.path.join(codex_root(), "history.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        for txt in user_texts:
            f.write(json.dumps({"session_id": session_id, "ts": int(now), "text": txt}, ensure_ascii=False) + "\n")

    return {"path": os.path.abspath(path), "session_id": session_id,
            "count": sum(1 for x in lines if x["type"] == "response_item")}
