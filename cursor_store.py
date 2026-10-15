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
                "created": meta.get("createdAtMs") or None,
                "size": st.st_size,
                "mtime": (meta.get("updatedAtMs", 0) / 1000.0) or st.st_mtime,
            })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ---------------- 写入 Cursor: 不可行(已实测确认) ----------------
# 结论: Cursor 会话内容受服务端签发的 token/签名完整性保护(线程节点里每个气泡带
# ~100 字符 token, reasoning 块带 provider signature)。诊断实测:
#   - 把真实 store.db 原样复制到新会话 -> Cursor 正常渲染(说明放文件夹本身可行)
#   - 仅改动一句文本并沿 DAG 级联重哈希 -> Cursor 渲染为空(内容一改, token 对不上)
# 因此无法合成 / 改写 / 导入任意会话到 Cursor。本模块仅提供只读能力。

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
