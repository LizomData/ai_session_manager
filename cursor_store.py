# -*- coding: utf-8 -*-
"""
Cursor CLI 会话存储(store.db)读写模块。

Cursor 把可恢复的会话存在 ~/.cursor/chats/<组hash>/<sessionId>/ 下:
  - store.db          : SQLite, 单表 blobs(id, data), id = sha256(data)(内容寻址)
  - meta.json         : {schemaVersion,title,cwd,createdAtMs,updatedAtMs,hasConversation}
  - prompt_history.json

store.db 内两类 blob:
  - 消息 blob : 纯 JSON {role, content, ...}  (role ∈ system/user/assistant/tool)
  - 节点 blob : protobuf, 用「原始 32 字节 sha256」引用消息与上下文, 组成 Merkle DAG

顺序重建: 回复节点会引用「到该步为止的完整有序消息列表」, 引用消息最多的节点即会话头,
其有序消息引用列表 = 整段对话。
"""
import hashlib
import json
import os
import sqlite3


# ---------------- protobuf 极简解码 ----------------
def _readv(d, i):
    r = 0
    s = 0
    while True:
        b = d[i]
        i += 1
        r |= (b & 0x7F) << s
        if not b & 0x80:
            return r, i
        s += 7


def _pb_fields(data):
    """返回 [(field_no, wire, value_bytes_or_int)], 只处理 varint / length-delimited / i32 / i64。"""
    out = []
    i = 0
    n = len(data)
    try:
        while i < n:
            key, i = _readv(data, i)
            fn, wt = key >> 3, key & 7
            if wt == 0:
                v, i = _readv(data, i)
                out.append((fn, 0, v))
            elif wt == 2:
                ln, i = _readv(data, i)
                out.append((fn, 2, data[i:i + ln]))
                i += ln
            elif wt == 5:
                out.append((fn, 5, data[i:i + 4]))
                i += 4
            elif wt == 1:
                out.append((fn, 1, data[i:i + 8]))
                i += 8
            else:
                break
    except (IndexError, KeyError):
        pass
    return out


# ---------------- 读取 ----------------
def _load_blobs(db_path):
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT id, data FROM blobs").fetchall()
    finally:
        con.close()
    return dict(rows)


def _as_json(data):
    try:
        j = json.loads(data)
        return j if isinstance(j, dict) else None
    except (ValueError, TypeError):
        return None


def _is_message(j):
    return isinstance(j, dict) and "role" in j and "content" in j


def reconstruct_order(blobs):
    """返回按对话顺序排列的消息 blob id 列表。"""
    raw2hex = {bytes.fromhex(b): b for b in blobs}
    msg_ids = {b for b, d in blobs.items() if _is_message(_as_json(d))}

    def node_msg_refs(data):
        refs = []
        ts = None
        for fn, wt, val in _pb_fields(data):
            if wt == 0 and fn == 26:
                ts = val
            elif wt == 2 and val in raw2hex:
                h = raw2hex[val]
                if h in msg_ids:
                    refs.append(h)
        return refs, ts

    best = []
    best_key = (-1, -1)
    for b, d in blobs.items():
        if _as_json(d) is not None and _is_message(_as_json(d)):
            continue  # 消息 blob 不作为节点
        refs, ts = node_msg_refs(d)
        # 去重保序
        seen = set()
        ordered = [r for r in refs if not (r in seen or seen.add(r))]
        key = (len(ordered), ts or 0)
        if key > best_key:
            best_key = key
            best = ordered

    # 兜底: 若没解析出节点(异常), 退化为 blob 存储顺序里的消息
    if not best:
        best = [b for b in blobs if b in msg_ids]
    else:
        # 补上任何遗漏的消息 blob(极少数分支消息未进头节点)
        inhead = set(best)
        for b in blobs:
            if b in msg_ids and b not in inhead:
                best.append(b)
    return best


# ---------------- 归一化为编辑器可渲染格式(Cursor jsonl 风格) ----------------
def _norm_part(p):
    if not isinstance(p, dict):
        return {"type": "text", "text": str(p)}
    t = p.get("type")
    if t == "text":
        return {"type": "text", "text": p.get("text", "")}
    if t == "reasoning":
        return {"type": "thinking", "thinking": p.get("text", "")}
    if t in ("tool-call", "tool_use"):
        return {"type": "tool_use", "name": p.get("toolName") or p.get("name"),
                "input": p.get("args", p.get("input", {})),
                "id": p.get("toolCallId") or p.get("id")}
    if t in ("tool-result", "tool_result"):
        res = p.get("result", p.get("output", p.get("content")))
        return {"type": "tool_result", "tool_use_id": p.get("toolCallId") or p.get("tool_use_id"),
                "content": res}
    return p


def _norm_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [_norm_part(p) for p in content]
    return content


def read_store(db_path):
    """读取 store.db, 返回归一化后的记录列表(Cursor jsonl 风格: {role, message:{content}})。"""
    blobs = _load_blobs(db_path)
    order = reconstruct_order(blobs)
    records = []
    for bid in order:
        j = _as_json(blobs[bid])
        if not _is_message(j):
            continue
        rec = {"role": j["role"], "message": {"content": _norm_content(j.get("content"))}}
        if "id" in j:
            rec["_id"] = j["id"]
        records.append(rec)
    records.append({"type": "turn_ended", "status": "success"})
    return records


def read_session_meta(session_dir):
    meta_path = os.path.join(session_dir, "meta.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


# ---------------- 发现 Cursor chat 会话 ----------------
def chats_root():
    return os.path.join(os.path.expanduser("~"), ".cursor", "chats")


def discover_chat_sessions():
    root = chats_root()
    out = []
    if not os.path.isdir(root):
        return out
    for group in os.listdir(root):
        gdir = os.path.join(root, group)
        if not os.path.isdir(gdir):
            continue
        for sid in os.listdir(gdir):
            sdir = os.path.join(gdir, sid)
            db = os.path.join(sdir, "store.db")
            if not os.path.isfile(db):
                continue
            meta = read_session_meta(sdir)
            try:
                st = os.stat(db)
            except OSError:
                continue
            out.append({
                "path": os.path.abspath(db), "session_dir": os.path.abspath(sdir),
                "session_id": sid, "group": group,
                "title": meta.get("title", ""), "cwd": meta.get("cwd", ""),
                "has_conversation": meta.get("hasConversation", False),
                "size": st.st_size,
                "mtime": (meta.get("updatedAtMs", 0) / 1000.0) or st.st_mtime,
            })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ---------------- 写入(实验性): 合成可被 Cursor 读取的 store.db 会话 ----------------
def _uvarint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_key(fn, wt):
    return _uvarint((fn << 3) | wt)


def _pb_ld(fn, b):
    return _pb_key(fn, 2) + _uvarint(len(b)) + b


def _pb_vint(fn, v):
    return _pb_key(fn, 0) + _uvarint(v)


def _denorm_part(p):
    """编辑器块 → Cursor store 消息 part(read_store 归一化的逆)。"""
    if not isinstance(p, dict):
        return {"type": "text", "text": str(p)}
    t = p.get("type")
    if t == "text":
        return {"type": "text", "text": p.get("text", "")}
    if t == "thinking":
        return {"type": "reasoning", "text": p.get("thinking", "")}
    if t == "tool_use":
        return {"type": "tool-call", "toolCallId": p.get("id") or ("call_" + uuid_hex()),
                "toolName": p.get("name"), "args": p.get("input", {})}
    if t == "tool_result":
        return {"type": "tool-result", "toolCallId": p.get("tool_use_id"), "result": p.get("content")}
    return p


def _denorm_content(content):
    if isinstance(content, list):
        return [_denorm_part(p) for p in content]
    return content


def uuid_hex():
    import uuid as _u
    return _u.uuid4().hex


def _md5_hex(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def build_store_blobs(records):
    """把记录构建成 {id:data} blob 字典 + 有序消息 id 列表 + 用户查询列表。"""
    blobs = {}
    ordered = []
    prompts = []
    for rec in records:
        if rec.get("type") == "turn_ended":
            continue
        role = rec.get("role") or (rec.get("message") or {}).get("role") or rec.get("type")
        if role not in ("system", "user", "assistant", "tool"):
            continue
        content = _denorm_content((rec.get("message") or {}).get("content"))
        msg = {"role": role, "content": content}
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        bid = hashlib.sha256(data).hexdigest()
        blobs[bid] = data
        ordered.append(bid)
        if role == "user" and isinstance(content, list):
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    prompts.append(p.get("text", ""))
    return blobs, ordered, prompts


def build_head_node(ordered_ids, cwd, updated_ms, timezone="Asia/Shanghai"):
    node = b""
    for bid in ordered_ids:
        node += _pb_ld(1, bytes.fromhex(bid))     # f1: 有序消息引用
    if cwd:
        url = "file:///" + cwd.replace("\\", "/")
        node += _pb_ld(9, url.encode("utf-8"))    # f9: cwd
    node += _pb_vint(10, 1)                        # f10
    node += _pb_ld(22, b"cli")                     # f22: 客户端
    node += _pb_vint(26, updated_ms)               # f26: 时间戳(ms)
    node += _pb_ld(27, timezone.encode("utf-8"))   # f27: 时区
    return node


def write_store(records, cwd, title="", out_root=None, session_id=None,
                created_ms=None, updated_ms=None, timezone="Asia/Shanghai"):
    """合成一个 Cursor chat 会话(store.db + meta.json + prompt_history.json)。

    返回 dict: {session_dir, db, session_id, group, count}
    注意: 实验性 —— 结构复刻自真实会话, 能否被 Cursor 实际 resume 需在 Cursor 里验证。
    """
    import time as _t
    now = int(_t.time() * 1000)
    created_ms = created_ms or now
    updated_ms = updated_ms or now
    session_id = session_id or str(__import__("uuid").uuid4())
    root = out_root or chats_root()
    group = _md5_hex(cwd) if cwd else uuid_hex()
    sdir = os.path.join(root, group, session_id)
    os.makedirs(sdir, exist_ok=True)

    blobs, ordered, prompts = build_store_blobs(records)
    head = build_head_node(ordered, cwd, updated_ms, timezone)
    hid = hashlib.sha256(head).hexdigest()
    blobs[hid] = head

    db = os.path.join(sdir, "store.db")
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, data BLOB)")
        con.executemany("INSERT OR REPLACE INTO blobs (id, data) VALUES (?, ?)",
                        [(bid, data) for bid, data in blobs.items()])
        con.commit()
    finally:
        con.close()

    meta = {"schemaVersion": 1, "createdAtMs": created_ms, "hasConversation": True,
            "title": title or "Imported session", "updatedAtMs": updated_ms, "cwd": cwd}
    with open(os.path.join(sdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    with open(os.path.join(sdir, "prompt_history.json"), "w", encoding="utf-8") as f:
        json.dump(prompts, f, ensure_ascii=False, indent=2)

    return {"session_dir": os.path.abspath(sdir), "db": os.path.abspath(db),
            "session_id": session_id, "group": group, "count": len(ordered)}


if __name__ == "__main__":
    import sys
    recs = read_store(sys.argv[1])
    for r in recs:
        role = r.get("role") or r.get("type")
        c = r.get("message", {}).get("content")
        if isinstance(c, list):
            desc = " ".join(
                (p.get("text") or p.get("thinking") or ("[%s:%s]" % (p.get("type"), p.get("name", "")))
                 or "").replace("\n", " ")[:50] for p in c)
        else:
            desc = str(c)[:70]
        print("%-9s | %s" % (role, desc[:80]))
