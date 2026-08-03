# -*- coding: utf-8 -*-
"""
Claude Code 会话记录(.jsonl)本地编辑器  —  后端分页版 + 搜索/过滤/撤销重做/插入移动/备份回滚

功能:
  - 查看/浏览: 把 JSONL 解析成可读对话(角色 / 文本 / thinking / 工具调用 / 工具结果)
  - 全文搜索: 跨全文搜关键词, 列出命中并一键跳转
  - 角色/类型过滤: 只看 user / assistant / tool_use / thinking 等
  - 修改文本: 就地编辑任意 text / thinking / 字符串 / 工具结果文本
  - 删除消息: 删除并自动把子节点 parentUuid 重链到父节点, 保持链表完整
  - 插入 / 移动 / 复制消息: 上下插入新消息、上移下移、复制某条
  - 截断: 从某条起截断会话
  - 撤销 / 重做: 编辑、删除、截断、插入、移动均可逐步回退
  - 备份管理 / 回滚: 列出所有 .bak.*, 一键回滚到某个时间点
  - 导出: Markdown 或纯文本

性能: 数据全部驻留后端内存, 浏览器每次只取当前页(默认 50 条), 几十 MB 也秒开。

用法:
  "C:\\Program Files\\Python311\\python.exe" session_editor.py [--file 路径.jsonl] [--dir 目录] [--port 8790] [--no-browser]

保存时先把原文件复制成 <文件名>.bak.<时间戳> 备份, 再原子覆盖写入。仅监听 127.0.0.1。
"""
import argparse
import copy
import glob
import json
import os
import shutil
import threading
import time
import uuid as uuidlib
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import cursor_store

# ---------------- 后端会话状态(单文件, 本地单用户) ----------------
STATE = {"path": None, "records": [], "dirty": False, "errors": [], "origin": "jsonl"}
UNDO, REDO = [], []
UNDO_LIMIT = 300
LOCK = threading.Lock()


def projects_root():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def cursor_root():
    return os.path.join(os.path.expanduser("~"), ".cursor", "projects")


def _source_of(ap):
    cl = os.path.abspath(projects_root())
    cu = os.path.abspath(cursor_root())
    if ap.startswith(cl):
        return "claude"
    if ap.startswith(cu):
        return "cursor"
    return "other"


def discover_sessions(extra_dir=None):
    roots = []
    if extra_dir:
        roots.append(extra_dir)
    roots.append(projects_root())
    roots.append(cursor_root())
    seen, out = set(), []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            seen.add(ap)
            try:
                st = os.stat(ap)
            except OSError:
                continue
            out.append({"path": ap, "name": os.path.basename(ap), "dir": os.path.dirname(ap),
                        "size": st.st_size, "mtime": st.st_mtime, "source": _source_of(ap)})
    # Cursor CLI 的 chat 会话(store.db, 可恢复的真实会话)
    try:
        for s in cursor_store.discover_chat_sessions():
            out.append({"path": s["path"], "name": s["title"] or s["session_id"],
                        "dir": s["session_dir"], "size": s["size"], "mtime": s["mtime"],
                        "source": "cursor-chat", "cwd": s.get("cwd", "")})
    except Exception:
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def load_jsonl(path):
    records, errors = [], []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                errors.append({"line": i + 1, "error": str(e)})
    return records, errors


def save_jsonl(path, records):
    bak = None
    if os.path.exists(path):
        bak = "%s.bak.%d" % (path, int(time.time()))
        shutil.copy2(path, bak)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
    os.replace(tmp, path)
    return bak


# ---------------- 记录辅助 ----------------
def record_role(rec):
    t = rec.get("type")
    if t == "summary":
        return "summary"
    if t == "turn_ended":
        return "turn_ended"
    m = rec.get("message") or {}
    # Cursor: role 在顶层; Claude Code: 在 message.role 或 type
    return rec.get("role") or m.get("role") or t or "?"


def record_to_text(rec):
    if rec.get("type") == "summary":
        return rec.get("summary", "")
    if rec.get("type") == "turn_ended":
        return "[turn_ended: %s]" % rec.get("status", "")
    m = rec.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return c
    parts = []
    if isinstance(c, list):
        for p in c:
            t = p.get("type")
            if t == "text":
                parts.append(p.get("text", ""))
            elif t == "thinking":
                parts.append(p.get("thinking", ""))
            elif t == "tool_use":
                parts.append("[工具调用 %s]\n%s" % (p.get("name", ""), json.dumps(p.get("input"), ensure_ascii=False)))
            elif t == "tool_result":
                tc = p.get("content")
                parts.append("[工具结果]\n%s" % (tc if isinstance(tc, str) else json.dumps(tc, ensure_ascii=False)))
    return "\n\n".join(x for x in parts if x and x.strip())


def record_matches(rec, tokens):
    if not tokens:
        return True
    if record_role(rec) in tokens:
        return True
    m = rec.get("message") or {}
    c = m.get("content")
    if isinstance(c, list):
        for p in c:
            if p.get("type") in tokens:
                return True
    if "text" in tokens and isinstance(c, str):
        return True
    return False


def filtered_indices(tokens):
    return [i for i, r in enumerate(STATE["records"]) if record_matches(r, tokens)]


# ---------------- 导出 ----------------
def build_export(records, fmt):
    lines = []
    for idx, rec in enumerate(records):
        role = record_role(rec)
        txt = record_to_text(rec)
        if fmt == "md":
            ts = rec.get("timestamp")
            head = "### #%d · %s" % (idx, role)
            if ts:
                head += "  \n<sub>%s</sub>" % ts
            lines.append(head)
            lines.append("\n" + (txt or "_(空)_") + "\n")
        else:
            lines.append("==== #%d [%s] ====" % (idx, role))
            lines.append(txt or "(空)")
            lines.append("")
    return "\n".join(lines)


# ---------------- 操作与撤销/重做 ----------------
def _do_op(op):
    """执行 op(直接修改 STATE['records']), 返回其逆操作。"""
    recs = STATE["records"]
    t = op["type"]
    if t == "update":
        i = op["idx"]
        old = recs[i]
        recs[i] = op["record"]
        return {"type": "update", "idx": i, "record": old}
    if t == "delete":
        i = op["idx"]
        victim = recs[i]
        vu = victim.get("uuid")
        pu = victim.get("parentUuid", None)
        children = []
        if vu is not None:
            for r in recs:
                if r.get("parentUuid") == vu:
                    children.append(r.get("uuid"))
                    r["parentUuid"] = pu
        del recs[i]
        return {"type": "restore_delete", "idx": i, "record": victim, "children": children, "vu": vu}
    if t == "restore_delete":
        i = op["idx"]
        recs.insert(i, op["record"])
        childset = set(op["children"])
        for r in recs:
            if r.get("uuid") in childset:
                r["parentUuid"] = op["vu"]
        return {"type": "delete", "idx": i}
    if t == "truncate":
        i = op["idx"]
        removed = recs[i + 1:]
        del recs[i + 1:]
        return {"type": "restore_truncate", "idx": i, "records": removed}
    if t == "restore_truncate":
        i = op["idx"]
        recs[i + 1:i + 1] = op["records"]
        return {"type": "truncate", "idx": i}
    if t == "insert":
        i = op["idx"]
        recs.insert(i, op["record"])
        relinked = None
        if op.get("relink") and i + 1 < len(recs):
            child = recs[i + 1]
            relinked = {"uuid": child.get("uuid"), "old_parent": child.get("parentUuid")}
            child["parentUuid"] = op["record"].get("uuid")
        return {"type": "undo_insert", "idx": i, "relinked": relinked}
    if t == "undo_insert":
        i = op["idx"]
        rel = op.get("relinked")
        if rel:
            for r in recs:
                if r.get("uuid") == rel["uuid"]:
                    r["parentUuid"] = rel["old_parent"]
                    break
        removed = recs.pop(i)
        return {"type": "insert", "idx": i, "record": removed, "relink": rel is not None}
    if t == "move":
        f, to = op["from"], op["to"]
        r = recs.pop(f)
        recs.insert(to, r)
        return {"type": "move", "from": to, "to": f}
    raise ValueError("unknown op %s" % t)


def apply_user_op(op):
    inv = _do_op(op)
    UNDO.append(inv)
    if len(UNDO) > UNDO_LIMIT:
        del UNDO[:len(UNDO) - UNDO_LIMIT]
    REDO.clear()
    STATE["dirty"] = True


def meta():
    return {"path": STATE["path"], "total": len(STATE["records"]), "dirty": STATE["dirty"],
            "errors": STATE["errors"], "canUndo": bool(UNDO), "canRedo": bool(REDO),
            "origin": STATE.get("origin", "jsonl")}


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")

        if u.path == "/api/list":
            try:
                return self._send(200, {"ok": True, "sessions": discover_sessions(q.get("dir", [None])[0])})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})

        if u.path == "/api/open":
            path = q.get("path", [None])[0]
            if not path or not os.path.isfile(path):
                return self._send(404, {"ok": False, "error": "文件不存在: %s" % path})
            try:
                if os.path.basename(path).lower() == "store.db":
                    records, errors, origin = cursor_store.read_store(path), [], "cursor-store"
                else:
                    records, errors, origin = (*load_jsonl(path), "jsonl")
                with LOCK:
                    STATE.update(path=os.path.abspath(path), records=records, dirty=False,
                                 errors=errors, origin=origin)
                    UNDO.clear()
                    REDO.clear()
                    return self._send(200, {"ok": True, **meta()})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})

        if u.path == "/api/page":
            try:
                start = int(q.get("start", ["0"])[0])
                count = int(q.get("count", ["50"])[0])
            except ValueError:
                return self._send(400, {"ok": False, "error": "start/count 非法"})
            tokens = set(t for t in (q.get("filter", [""])[0]).split(",") if t and t != "all")
            with LOCK:
                recs = STATE["records"]
                if tokens:
                    idxs = filtered_indices(tokens)
                else:
                    idxs = None
                total = len(idxs) if idxs is not None else len(recs)
                start = max(0, min(start, total))
                if idxs is not None:
                    sl = idxs[start:start + count]
                    items = [{"idx": i, "rec": recs[i]} for i in sl]
                else:
                    items = [{"idx": start + k, "rec": recs[start + k]}
                             for k in range(0, min(count, total - start))]
            return self._send(200, {"ok": True, "start": start, "total": total, "items": items})

        if u.path == "/api/search":
            ql = (q.get("q", [""])[0]).lower()
            out = []
            if ql:
                with LOCK:
                    for i, r in enumerate(STATE["records"]):
                        txt = record_to_text(r)
                        pos = txt.lower().find(ql)
                        if pos >= 0:
                            s = max(0, pos - 30)
                            snip = txt[s:pos + len(ql) + 40].replace("\n", " ")
                            out.append({"idx": i, "role": record_role(r),
                                        "snippet": ("…" if s > 0 else "") + snip})
                            if len(out) >= 300:
                                break
            return self._send(200, {"ok": True, "results": out})

        if u.path == "/api/export":
            fmt = q.get("fmt", ["txt"])[0]
            with LOCK:
                text = build_export(STATE["records"], fmt)
            return self._send(200, text, "text/plain; charset=utf-8")

        if u.path == "/api/backups":
            with LOCK:
                path = STATE["path"]
            if not path:
                return self._send(400, {"ok": False, "error": "未打开文件"})
            out = []
            for p in glob.glob(path + ".bak.*"):
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append({"path": p, "name": os.path.basename(p), "size": st.st_size, "mtime": st.st_mtime})
            out.sort(key=lambda x: x["mtime"], reverse=True)
            return self._send(200, {"ok": True, "backups": out})

        return self._send(404, {"ok": False, "error": "not found"})

    # ---- POST ----
    def do_POST(self):
        u = urlparse(self.path)
        try:
            data = self._body()
        except Exception as e:
            return self._send(400, {"ok": False, "error": "请求体解析失败: %s" % e})

        def need_idx(name="idx"):
            v = data.get(name)
            if not isinstance(v, int) or v < 0 or v >= len(STATE["records"]):
                return None
            return v

        with LOCK:
            if u.path == "/api/update":
                i = need_idx()
                if i is None or "record" not in data:
                    return self._send(400, {"ok": False, "error": "idx 越界或缺 record"})
                apply_user_op({"type": "update", "idx": i, "record": data["record"]})
                return self._send(200, {"ok": True, **meta()})

            if u.path == "/api/delete":
                i = need_idx()
                if i is None:
                    return self._send(400, {"ok": False, "error": "idx 越界"})
                apply_user_op({"type": "delete", "idx": i})
                return self._send(200, {"ok": True, **meta()})

            if u.path == "/api/truncate":
                i = need_idx()
                if i is None:
                    return self._send(400, {"ok": False, "error": "idx 越界"})
                apply_user_op({"type": "truncate", "idx": i})
                return self._send(200, {"ok": True, **meta()})

            if u.path == "/api/insert":
                i = data.get("idx")
                if not isinstance(i, int) or i < 0 or i > len(STATE["records"]):
                    return self._send(400, {"ok": False, "error": "idx 越界"})
                if "record" not in data:
                    return self._send(400, {"ok": False, "error": "缺 record"})
                apply_user_op({"type": "insert", "idx": i, "record": data["record"],
                               "relink": bool(data.get("relink"))})
                return self._send(200, {"ok": True, "idx": i, **meta()})

            if u.path == "/api/move":
                f = data.get("from")
                to = data.get("to")
                n = len(STATE["records"])
                if not (isinstance(f, int) and isinstance(to, int) and 0 <= f < n and 0 <= to < n):
                    return self._send(400, {"ok": False, "error": "from/to 越界"})
                if f != to:
                    apply_user_op({"type": "move", "from": f, "to": to})
                return self._send(200, {"ok": True, **meta()})

            if u.path == "/api/undo":
                if not UNDO:
                    return self._send(200, {"ok": True, "noop": True, **meta()})
                op = UNDO.pop()
                REDO.append(_do_op(op))
                STATE["dirty"] = True
                return self._send(200, {"ok": True, **meta()})

            if u.path == "/api/redo":
                if not REDO:
                    return self._send(200, {"ok": True, "noop": True, **meta()})
                op = REDO.pop()
                UNDO.append(_do_op(op))
                STATE["dirty"] = True
                return self._send(200, {"ok": True, **meta()})

            if u.path == "/api/rollback":
                bak = data.get("backup")
                path = STATE["path"]
                if not path:
                    return self._send(400, {"ok": False, "error": "未打开文件"})
                if not bak or not bak.startswith(path + ".bak.") or not os.path.isfile(bak):
                    return self._send(400, {"ok": False, "error": "非法备份路径"})
                try:
                    records, errors = load_jsonl(bak)
                    STATE.update(records=records, dirty=True, errors=errors)
                    UNDO.clear()
                    REDO.clear()
                    return self._send(200, {"ok": True, **meta()})
                except Exception as e:
                    return self._send(500, {"ok": False, "error": str(e)})

            if u.path == "/api/fork":
                i = need_idx()
                if i is None:
                    return self._send(400, {"ok": False, "error": "idx 越界"})
                if not STATE["path"]:
                    return self._send(400, {"ok": False, "error": "未打开文件"})
                new_sid = data.get("sessionId") or str(uuidlib.uuid4())
                rewrite = data.get("rewriteSessionId", True)
                prefix = copy.deepcopy(STATE["records"][:i + 1])
                if rewrite:
                    for r in prefix:
                        if isinstance(r, dict) and "sessionId" in r:
                            r["sessionId"] = new_sid
                out_dir = data.get("dir") or os.path.dirname(STATE["path"])
                out_path = data.get("path") or os.path.join(out_dir, new_sid + ".jsonl")
                out_path = os.path.abspath(out_path)
                if os.path.exists(out_path):
                    return self._send(400, {"ok": False, "error": "目标已存在: %s" % out_path})
                try:
                    tmp = out_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                        for r in prefix:
                            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
                            f.write("\n")
                    os.replace(tmp, out_path)
                except Exception as e:
                    return self._send(500, {"ok": False, "error": str(e)})
                return self._send(200, {"ok": True, "path": out_path, "sessionId": new_sid, "count": len(prefix)})

            if u.path == "/api/save":
                path = STATE["path"]
                if not path:
                    return self._send(400, {"ok": False, "error": "未打开文件"})
                if STATE.get("origin") == "cursor-store":
                    return self._send(400, {"ok": False,
                        "error": "Cursor store.db 为只读(内容受完整性校验保护, 不可改写)"})
                try:
                    bak = save_jsonl(path, STATE["records"])
                    STATE["dirty"] = False
                    return self._send(200, {"ok": True, "backup": bak, "count": len(STATE["records"])})
                except Exception as e:
                    return self._send(500, {"ok": False, "error": str(e)})

        return self._send(404, {"ok": False, "error": "not found"})


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>会话记录编辑器</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#fff; --border:#e2e5ea; --text:#1c1f24; --muted:#6b7280;
    --accent:#3b6ef5; --danger:#dc2626; --user:#eef4ff; --asst:#f3f4f6;
    --tool:#fff7ed; --think:#f5f3ff; --chip:#eef1f5; --flash:#fde68a;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#15171b; --panel:#1e2127; --border:#2c313a; --text:#e6e8eb; --muted:#9aa1ad;
      --accent:#5b8bff; --danger:#f87171; --user:#1b2740; --asst:#23262d;
      --tool:#2a2318; --think:#241f38; --chip:#2a2f38; --flash:#5b4a1a;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text)}
  header{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--border);padding:8px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  header h1{font-size:15px;margin:0 8px 0 0;font-weight:650}
  .bar2{position:sticky;top:49px;z-index:19;background:var(--panel);border-bottom:1px solid var(--border);padding:6px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  select,input,button{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--border);border-radius:7px;padding:6px 10px}
  select{max-width:46vw}
  button{cursor:pointer}
  button.primary{background:var(--accent);color:#fff;border-color:transparent}
  button.danger{color:var(--danger)}
  button:disabled{opacity:.45;cursor:not-allowed}
  .spacer{flex:1}
  .stat{color:var(--muted);font-size:12px}
  main{max-width:980px;margin:0 auto;padding:14px}
  .fchip{padding:4px 11px;border-radius:20px;font-size:12px;cursor:pointer;user-select:none}
  .fchip.on{background:var(--accent);color:#fff;border-color:transparent}
  .searchbox{position:relative;display:flex;gap:6px;align-items:center}
  .searchbox input{width:220px}
  .results{position:absolute;top:38px;left:0;width:min(680px,86vw);max-height:60vh;overflow:auto;background:var(--panel);border:1px solid var(--border);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.25);z-index:40;display:none}
  .results.on{display:block}
  .ritem{padding:8px 12px;border-bottom:1px solid var(--border);cursor:pointer;font-size:13px}
  .ritem:hover{background:var(--chip)}
  .ritem b{color:var(--accent)}
  .rmeta{color:var(--muted);font-size:11px;margin-right:8px}
  .msg{background:var(--panel);border:1px solid var(--border);border-radius:10px;margin:10px 0;overflow:hidden}
  .msg.user{background:var(--user)}
  .msg.assistant{background:var(--asst)}
  .msg.flash{animation:fl 1.4s ease}
  @keyframes fl{0%,40%{background:var(--flash)}100%{}}
  .msg-head{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--border);flex-wrap:wrap}
  .role{font-weight:650;text-transform:capitalize}
  .role.user{color:#2563eb}.role.assistant{color:#7c3aed}.role.system{color:#059669}
  .chip{background:var(--chip);color:var(--muted);border-radius:20px;padding:2px 9px;font-size:11px}
  .idx{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
  .msg-head .acts{display:flex;gap:4px;margin-left:auto}
  .msg-head .acts button{padding:3px 7px;font-size:12px}
  .body{padding:10px 12px;display:flex;flex-direction:column;gap:10px}
  .block{border:1px dashed var(--border);border-radius:8px;padding:8px 10px}
  .block .btype{font-size:11px;color:var(--muted);margin-bottom:5px;display:flex;gap:8px;align-items:center}
  .block.think{background:var(--think)}.block.tool_use{background:var(--tool)}.block.tool_result{background:var(--tool)}
  textarea{width:100%;min-height:38px;resize:vertical;font:13px/1.5 ui-monospace,"Cascadia Code",Consolas,monospace;background:transparent;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:7px}
  textarea.text{font-family:inherit}
  pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,Consolas,monospace;color:var(--muted);max-height:220px;overflow:auto}
  .empty{color:var(--muted);text-align:center;padding:60px 0}
  .pager{display:none;gap:8px;align-items:center;justify-content:center;flex-wrap:wrap;padding:8px 0}
  .pager.on{display:flex}
  .pager button{padding:4px 10px}
  .pager .pinfo{color:var(--muted);font-size:12px;min-width:150px;text-align:center}
  .pager input{width:64px;text-align:center}
  .more{cursor:pointer;color:var(--accent);font-size:11px;background:none;border:none;padding:2px 0}
  .toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#111;color:#fff;padding:10px 16px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:60}
  .toast.show{opacity:.95}
  .dirty{color:var(--danger);font-weight:650}
  .hint{color:var(--muted);font-size:12px}
  .loading{text-align:center;color:var(--muted);padding:30px}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:70}
  .modal.on{display:flex}
  .modal .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;width:min(620px,92vw);max-height:80vh;overflow:auto;padding:16px}
  .modal h2{margin:0 0 10px;font-size:15px}
  .bak{display:flex;gap:10px;align-items:center;padding:8px 4px;border-bottom:1px solid var(--border);font-size:13px}
  .bak .bn{flex:1;word-break:break-all}
</style>
</head>
<body>
<header>
  <h1>会话编辑器</h1>
  <select id="source" title="按来源过滤">
    <option value="all">全部来源</option>
    <option value="claude">Claude</option>
    <option value="cursor">Cursor(jsonl)</option>
    <option value="cursor-chat">Cursor 会话(store.db)</option>
    <option value="other">其它</option>
  </select>
  <select id="sessions" title="选择会话文件"></select>
  <button id="reload" title="刷新会话列表">↻</button>
  <input id="pathInput" placeholder="或粘贴 .jsonl 绝对路径" style="width:210px">
  <button id="openPath">打开</button>
  <div class="spacer"></div>
  <button id="undoBtn" title="撤销 Ctrl+Z" disabled>↶ 撤销</button>
  <button id="redoBtn" title="重做 Ctrl+Y" disabled>↷ 重做</button>
  <button id="bakBtn" title="备份管理/回滚">备份</button>
  <button id="saveBtn" class="primary" disabled>保存</button>
  <button id="exportMd">导出MD</button>
  <button id="exportTxt">导出TXT</button>
</header>
<div class="bar2">
  <span class="stat" id="stat"></span>
  <div class="spacer"></div>
  <span class="hint">过滤:</span>
  <span class="fchip chip" data-tok="user">user</span>
  <span class="fchip chip" data-tok="assistant">assistant</span>
  <span class="fchip chip" data-tok="tool_use">tool_use</span>
  <span class="fchip chip" data-tok="tool_result">tool_result</span>
  <span class="fchip chip" data-tok="thinking">thinking</span>
  <span class="fchip chip" data-tok="system">system</span>
  <div class="searchbox">
    <input id="searchInput" placeholder="全文搜索…回车">
    <button id="searchBtn">搜索</button>
    <div class="results" id="results"></div>
  </div>
</div>
<main>
  <div class="pager" id="pagerTop"></div>
  <div id="list"></div>
  <div class="pager" id="pagerBot"></div>
  <div class="empty" id="empty">← 先在上方选择或输入一个会话文件</div>
</main>
<div class="toast" id="toast"></div>
<div class="modal" id="bakModal"><div class="card">
  <h2>备份管理 / 回滚</h2>
  <div id="bakList"><div class="hint">加载中…</div></div>
  <div style="text-align:right;margin-top:12px"><button id="bakClose">关闭</button></div>
</div></div>

<script>
let CUR_PATH="", TOTAL=0, CUR_PAGE=0, PAGE_SIZE=50, DIRTY=false;
let PAGE_ITEMS=[];               // [{idx, rec}]
const ACTIVE_FILTER = new Set();
const PRE_LIMIT = 4000;

const $ = s => document.querySelector(s);
const listEl=$("#list"), emptyEl=$("#empty");

function toast(m){ const t=$("#toast"); t.textContent=m; t.classList.add("show"); setTimeout(()=>t.classList.remove("show"),1800); }
function fmtSize(n){ return n>1048576?(n/1048576).toFixed(1)+"M":n>1024?(n/1024).toFixed(0)+"K":n+"B"; }
function fmtTime(ts){ try{return new Date(ts*1000).toLocaleString();}catch(e){return "";} }
function pageCount(){ return Math.max(1, Math.ceil(TOTAL/PAGE_SIZE)); }
function filterStr(){ return [...ACTIVE_FILTER].join(","); }
function uuid(){ return crypto.randomUUID? crypto.randomUUID()
  : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g,c=>{const r=Math.random()*16|0;return (c==="x"?r:(r&0x3|0x8)).toString(16);}); }

function applyMeta(j){
  if("total" in j) TOTAL=j.total;
  if("dirty" in j){ DIRTY=j.dirty; $("#saveBtn").disabled=!DIRTY; }
  if("canUndo" in j) $("#undoBtn").disabled=!j.canUndo;
  if("canRedo" in j) $("#redoBtn").disabled=!j.canRedo;
  $("#stat").innerHTML=statText();
}
function statText(){ if(!CUR_PATH) return ""; return `${TOTAL} 条`+(DIRTY?' · <span class="dirty">未保存</span>':' · 已保存'); }

// ---- 编辑防抖提交 ----
const pending=new Map(); let editTimer=null;
function scheduleUpdate(idx,rec){ pending.set(idx,rec); DIRTY=true; $("#saveBtn").disabled=false; $("#stat").innerHTML=statText();
  clearTimeout(editTimer); editTimer=setTimeout(flushUpdates,400); }
async function flushUpdates(){
  clearTimeout(editTimer); editTimer=null;
  const items=[...pending]; pending.clear();
  for(const [idx,rec] of items){
    const r=await fetch("/api/update",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({idx,record:rec})});
    applyMeta(await r.json());
  }
}

let ALL_SESSIONS=[];
function renderSessionOptions(){
  const src=$("#source").value;
  const list=ALL_SESSIONS.filter(s=> src==="all"? true : s.source===src);
  const sel=$("#sessions");
  const tag={claude:"[Claude]",cursor:"[Cursor]","cursor-chat":"[Cursor·db]",other:"[其它]"};
  sel.innerHTML=`<option value="">— 选择会话 (${list.length}) —</option>`;
  for(const s of list){
    const o=document.createElement("option"); o.value=s.path;
    o.textContent=`${tag[s.source]||""} ${s.name.slice(0,10)}…  ·  ${fmtSize(s.size)}  ·  ${fmtTime(s.mtime)}  ·  ${s.dir.split(/[\\/]/).pop()}`;
    sel.appendChild(o);
  }
}
async function loadSessions(){
  const j=await (await fetch("/api/list")).json();
  ALL_SESSIONS = j.ok? j.sessions : [];
  renderSessionOptions();
}

async function openFile(path){
  if(!path) return;
  if(DIRTY && !confirm("当前有未保存的修改, 确认放弃并打开新文件?")) return;
  await flushUpdates();
  emptyEl.style.display="none"; listEl.innerHTML='<div class="loading">加载中…</div>';
  const j=await (await fetch("/api/open?path="+encodeURIComponent(path))).json();
  if(!j.ok){ toast("加载失败: "+j.error); listEl.innerHTML=""; return; }
  CUR_PATH=j.path; CUR_PAGE=0; ACTIVE_FILTER.clear(); syncChips();
  $("#pathInput").value=j.path;
  applyMeta(j);
  if(j.errors&&j.errors.length) toast(`有 ${j.errors.length} 行解析失败(已跳过)`);
  await loadPage(0);
}

async function loadPage(page){
  if(!CUR_PATH) return;
  page=Math.max(0,Math.min(page,pageCount()-1)); CUR_PAGE=page;
  const start=page*PAGE_SIZE;
  const j=await (await fetch(`/api/page?start=${start}&count=${PAGE_SIZE}&filter=${encodeURIComponent(filterStr())}`)).json();
  if(!j.ok){ toast("翻页失败: "+j.error); return; }
  TOTAL=j.total; PAGE_ITEMS=j.items; $("#stat").innerHTML=statText();
  renderPage();
}

// ---- 内容块 ----
function getBlocks(rec){
  const blocks=[];
  if(rec.type==="summary"){ blocks.push({kind:"summary",editable:true,get:()=>rec.summary||"",set:v=>rec.summary=v}); return blocks; }
  if(rec.type==="turn_ended"){ blocks.push({kind:"turn_ended",editable:false,part:{status:rec.status}}); return blocks; }
  const m=rec.message; if(!m) return blocks;
  const c=m.content;
  if(typeof c==="string"){ blocks.push({kind:"text",editable:true,get:()=>m.content,set:v=>m.content=v}); return blocks; }
  if(Array.isArray(c)) for(const part of c){
    if(part.type==="text") blocks.push({kind:"text",editable:true,get:()=>part.text,set:v=>part.text=v,part});
    else if(part.type==="thinking") blocks.push({kind:"think",editable:true,get:()=>part.thinking||"",set:v=>part.thinking=v,part});
    else if(part.type==="tool_use") blocks.push({kind:"tool_use",editable:false,name:part.name,part});
    else if(part.type==="tool_result"){ if(typeof part.content==="string") blocks.push({kind:"tool_result",editable:true,get:()=>part.content,set:v=>part.content=v,part}); else blocks.push({kind:"tool_result",editable:false,part}); }
    else blocks.push({kind:part.type||"?",editable:false,part});
  }
  return blocks;
}
function roleOf(rec){
  if(rec.type==="summary") return "summary";
  if(rec.type==="turn_ended") return "turn_ended";
  return rec.role||(rec.message&&rec.message.role)||rec.type||"?";   // Cursor 顶层 role / Claude message.role
}

function renderRecord(rec, idx){
  const role=roleOf(rec);
  const el=document.createElement("div"); el.className="msg "+role; el.dataset.idx=idx;
  const head=document.createElement("div"); head.className="msg-head";
  head.innerHTML=`<span class="idx">#${idx}</span><span class="role ${role}">${role}</span>`+
    (rec.timestamp?`<span class="chip">${new Date(rec.timestamp).toLocaleString()}</span>`:"")+
    (rec.isSidechain?`<span class="chip">sidechain</span>`:"")+
    (rec.uuid?`<span class="chip" title="${rec.uuid}">uuid ${String(rec.uuid).slice(0,6)}</span>`:"");
  const acts=document.createElement("div"); acts.className="acts";
  const mk=(t,title,fn,cls)=>{ const b=document.createElement("button"); b.textContent=t; b.title=title; if(cls)b.className=cls; b.onclick=fn; return b; };
  acts.appendChild(mk("＋↑","在上方插入消息",()=>insertMsg(idx,"above")));
  acts.appendChild(mk("＋↓","在下方插入消息",()=>insertMsg(idx,"below")));
  acts.appendChild(mk("⧉","复制此条",()=>duplicateMsg(idx,rec)));
  acts.appendChild(mk("↑","上移",()=>moveMsg(idx,idx-1)));
  acts.appendChild(mk("↓","下移",()=>moveMsg(idx,idx+1)));
  acts.appendChild(mk("⑂","从此条 fork 新会话(含开头到此条)",()=>forkFrom(idx)));
  acts.appendChild(mk("删除","删除(自动重链)",()=>deleteRecord(idx,rec),"danger"));
  acts.appendChild(mk("截断","删除此条之后所有",()=>truncateAfter(idx)));
  head.appendChild(acts); el.appendChild(head);

  const body=document.createElement("div"); body.className="body";
  const blocks=getBlocks(rec);
  if(!blocks.length){ const d=document.createElement("div"); d.className="hint"; d.textContent="(无可显示内容)"; body.appendChild(d); }
  for(const b of blocks){
    const bd=document.createElement("div"); bd.className="block "+b.kind;
    const label=document.createElement("div"); label.className="btype";
    label.textContent=b.kind+(b.name?(": "+b.name):"")+(b.editable?"":" (只读)"); bd.appendChild(label);
    if(b.editable){
      const ta=document.createElement("textarea"); if(b.kind==="text"||b.kind==="summary") ta.className="text";
      ta.value=b.get();
      const grow=()=>{ ta.style.height="auto"; ta.style.height=Math.min(ta.scrollHeight+2,600)+"px"; };
      ta.addEventListener("input",()=>{ b.set(ta.value); scheduleUpdate(idx,rec); grow(); });
      ta.addEventListener("change",()=>{ b.set(ta.value); scheduleUpdate(idx,rec); });
      bd.appendChild(ta); requestAnimationFrame(grow);
    }else{
      let content = b.kind==="tool_use"&&b.part? JSON.stringify(b.part.input,null,2)
        : b.kind==="tool_result"&&b.part? JSON.stringify(b.part.content,null,2)
        : b.part? JSON.stringify(b.part,null,2) : "";
      content=content||"";
      const pre=document.createElement("pre");
      if(content.length>PRE_LIMIT){
        let ex=false; const more=document.createElement("button"); more.className="more";
        const ap=()=>{ pre.textContent=ex?content:content.slice(0,PRE_LIMIT)+" …"; more.textContent=ex?"收起":`展开全部 (${content.length} 字符)`; };
        more.onclick=()=>{ ex=!ex; ap(); }; ap(); bd.appendChild(pre); bd.appendChild(more);
      }else{ pre.textContent=content; bd.appendChild(pre); }
    }
    body.appendChild(bd);
  }
  el.appendChild(body); return el;
}

function renderPager(where){
  const total=pageCount(), on=TOTAL>PAGE_SIZE;
  where.className="pager"+(on?" on":""); if(!on){ where.innerHTML=""; return; }
  const start=CUR_PAGE*PAGE_SIZE, end=Math.min(start+PAGE_SIZE,TOTAL);
  where.innerHTML="";
  const mk=(t,fn,dis)=>{ const b=document.createElement("button"); b.textContent=t; b.disabled=!!dis; b.onclick=fn; return b; };
  where.appendChild(mk("« 首页",()=>gotoPage(0),CUR_PAGE===0));
  where.appendChild(mk("‹ 上一页",()=>gotoPage(CUR_PAGE-1),CUR_PAGE===0));
  const info=document.createElement("span"); info.className="pinfo"; info.textContent=`第 ${CUR_PAGE+1}/${total} 页 · 共 ${TOTAL} 条`;
  where.appendChild(info);
  where.appendChild(mk("下一页 ›",()=>gotoPage(CUR_PAGE+1),CUR_PAGE>=total-1));
  where.appendChild(mk("末页 »",()=>gotoPage(total-1),CUR_PAGE>=total-1));
  const jump=document.createElement("input"); jump.type="number"; jump.min=1; jump.max=total; jump.placeholder="跳页";
  jump.onkeydown=e=>{ if(e.key==="Enter"){ const p=parseInt(jump.value,10); if(p>=1&&p<=total) gotoPage(p-1); } };
  where.appendChild(jump);
  const ps=document.createElement("select"); ps.title="每页条数";
  [25,50,100,200].forEach(n=>{ const o=document.createElement("option"); o.value=n; o.textContent=n+"/页"; if(n===PAGE_SIZE)o.selected=true; ps.appendChild(o); });
  ps.onchange=()=>{ PAGE_SIZE=parseInt(ps.value,10); loadPage(0); };
  where.appendChild(ps);
}
async function gotoPage(p){ await flushUpdates(); await loadPage(p); window.scrollTo({top:0}); }

function renderPage(){
  emptyEl.style.display=TOTAL?"none":"block";
  renderPager($("#pagerTop"));
  const frag=document.createDocumentFragment();
  PAGE_ITEMS.forEach(it=> frag.appendChild(renderRecord(it.rec,it.idx)));
  listEl.replaceChildren(frag);
  renderPager($("#pagerBot"));
}

// ---- 结构操作 ----
async function structOp(url, payload){
  await flushUpdates();
  const j=await (await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload||{})})).json();
  if(!j.ok){ toast("操作失败: "+j.error); return null; }
  applyMeta(j);
  return j;
}
async function deleteRecord(idx,rec){
  if(!confirm(`删除 #${idx} (${roleOf(rec)})? 其子节点会自动重新链接到父节点。`)) return;
  const j=await structOp("/api/delete",{idx}); if(j) await loadPage(Math.min(CUR_PAGE,pageCount()-1));
}
async function truncateAfter(idx){
  const n=TOTAL-(idx+1); if(n<=0){ toast("此条已是最后一条"); return; }
  if(!confirm(`删除 #${idx} 之后的 ${n} 条消息?`)) return;
  const j=await structOp("/api/truncate",{idx}); if(j) await loadPage(Math.min(CUR_PAGE,pageCount()-1));
}
function isCursor(rec){ return rec && rec.role!==undefined && rec.uuid===undefined; }
function skeleton(neighbor, role){
  if(isCursor(neighbor)) return { role, message:{content:[{type:"text",text:""}]} };  // Cursor 风格: 顶层 role, 无链表字段
  const r={ uuid:uuid(), parentUuid:null, type:role, message:{role,content:[{type:"text",text:""}]}, timestamp:new Date().toISOString() };
  for(const k of ["sessionId","cwd","version","gitBranch","userType","entrypoint"]) if(neighbor&&neighbor[k]!==undefined) r[k]=neighbor[k];
  return r;
}
async function insertMsg(idx, where){
  const rec=PAGE_ITEMS.find(it=>it.idx===idx)?.rec; if(!rec) return;
  const role = prompt("新消息角色 (user / assistant / system):","user"); if(!role) return;
  const nr=skeleton(rec, role.trim());
  const insertIdx = where==="below"? idx+1 : idx;
  const claudeStyle = nr.uuid!==undefined;
  if(claudeStyle) nr.parentUuid = where==="below"? (rec.uuid||null) : (rec.parentUuid||null);
  const j=await structOp("/api/insert",{idx:insertIdx, record:nr, relink:claudeStyle});
  if(j){ await loadPage(Math.floor(insertIdx/PAGE_SIZE)); flash(insertIdx); }
}
async function duplicateMsg(idx, rec){
  const copy=JSON.parse(JSON.stringify(rec));
  if(copy.uuid!==undefined){ copy.uuid=uuid(); copy.parentUuid=rec.uuid||null; }   // 仅 Claude 风格才改 uuid
  const j=await structOp("/api/insert",{idx:idx+1, record:copy, relink:false});
  if(j){ await loadPage(Math.floor((idx+1)/PAGE_SIZE)); flash(idx+1); }
}
async function moveMsg(from, to){
  if(to<0||to>=TOTAL){ return; }
  const j=await structOp("/api/move",{from,to});
  if(j){ await loadPage(Math.floor(to/PAGE_SIZE)); flash(to); }
}
function flash(idx){ const el=listEl.querySelector(`[data-idx="${idx}"]`); if(el){ el.scrollIntoView({block:"center"}); el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); } }

async function forkFrom(idx){
  if(!confirm(`从 #${idx} fork 出新会话?\n将复制「开头到此条」共 ${idx+1} 条为一个新 sessionId 的独立文件(写入同目录), 当前会话不受影响。`)) return;
  await flushUpdates();
  const j=await (await fetch("/api/fork",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({idx})})).json();
  if(!j.ok){ toast("fork 失败: "+j.error); return; }
  await loadSessions();
  if(confirm(`已生成新会话:\n${j.path}\n\n${j.count} 条 · sessionId ${j.sessionId.slice(0,8)}…\n\n是否立即打开该 fork?`)) await openFile(j.path);
  else toast("已生成: "+j.path.split(/[\\/]/).pop());
}
async function undo(){ const j=await structOp("/api/undo",{}); if(j&&!j.noop) await loadPage(Math.min(CUR_PAGE,pageCount()-1)); }
async function redo(){ const j=await structOp("/api/redo",{}); if(j&&!j.noop) await loadPage(Math.min(CUR_PAGE,pageCount()-1)); }

async function save(){
  if(!CUR_PATH) return; await flushUpdates();
  const j=await (await fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})).json();
  if(j.ok){ DIRTY=false; $("#saveBtn").disabled=true; $("#stat").innerHTML=statText(); toast("已保存"+(j.backup?(" · 备份: "+j.backup.split(/[\\/]/).pop()):"")); }
  else toast("保存失败: "+j.error);
}
async function exportAs(fmt){
  if(!CUR_PATH) return; await flushUpdates();
  const text=await (await fetch("/api/export?fmt="+fmt)).text();
  const blob=new Blob([text],{type:"text/plain;charset=utf-8"}); const a=document.createElement("a");
  const base=(CUR_PATH.split(/[\\/]/).pop()||"session").replace(/\.jsonl$/,"");
  a.href=URL.createObjectURL(blob); a.download=base+(fmt==="md"?".md":".txt"); a.click(); URL.revokeObjectURL(a.href);
}

// ---- 过滤 ----
function syncChips(){ document.querySelectorAll(".fchip").forEach(c=> c.classList.toggle("on", ACTIVE_FILTER.has(c.dataset.tok))); }
document.querySelectorAll(".fchip").forEach(c=> c.onclick=async()=>{
  const t=c.dataset.tok; if(ACTIVE_FILTER.has(t)) ACTIVE_FILTER.delete(t); else ACTIVE_FILTER.add(t);
  syncChips(); await flushUpdates(); await loadPage(0);
});

// ---- 搜索 ----
async function doSearch(){
  const q=$("#searchInput").value.trim(); const box=$("#results");
  if(!q){ box.classList.remove("on"); return; }
  const j=await (await fetch("/api/search?q="+encodeURIComponent(q))).json();
  box.innerHTML="";
  if(!j.results.length){ box.innerHTML='<div class="ritem hint">无匹配</div>'; box.classList.add("on"); return; }
  const esc=s=>s.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  const re=new RegExp("("+q.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","ig");
  for(const r of j.results){
    const d=document.createElement("div"); d.className="ritem";
    d.innerHTML=`<span class="rmeta">#${r.idx} ${r.role}</span>`+esc(r.snippet).replace(re,"<b>$1</b>");
    d.onclick=()=>{ box.classList.remove("on"); jumpTo(r.idx); };
    box.appendChild(d);
  }
  box.classList.add("on");
}
async function jumpTo(idx){
  if(ACTIVE_FILTER.size){ ACTIVE_FILTER.clear(); syncChips(); }
  await flushUpdates(); await loadPage(Math.floor(idx/PAGE_SIZE)); flash(idx);
}

// ---- 备份 ----
async function openBak(){
  if(!CUR_PATH){ toast("请先打开文件"); return; }
  $("#bakModal").classList.add("on"); const list=$("#bakList"); list.innerHTML='<div class="hint">加载中…</div>';
  const j=await (await fetch("/api/backups")).json();
  if(!j.ok){ list.innerHTML='<div class="hint">'+j.error+'</div>'; return; }
  if(!j.backups.length){ list.innerHTML='<div class="hint">暂无备份(保存或回滚后会自动生成)</div>'; return; }
  list.innerHTML="";
  for(const b of j.backups){
    const row=document.createElement("div"); row.className="bak";
    row.innerHTML=`<span class="bn">${b.name}<br><span class="hint">${fmtTime(b.mtime)} · ${fmtSize(b.size)}</span></span>`;
    const btn=document.createElement("button"); btn.textContent="回滚到此"; btn.className="danger";
    btn.onclick=async()=>{
      if(!confirm("回滚到该备份? 当前内容会被替换(需再点保存才写回文件, 保存时会再自动备份当前状态)。")) return;
      const r=await (await fetch("/api/rollback",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({backup:b.path})})).json();
      if(r.ok){ applyMeta(r); CUR_PAGE=0; ACTIVE_FILTER.clear(); syncChips(); await loadPage(0); $("#bakModal").classList.remove("on"); toast("已回滚(未写盘, 请检查后保存)"); }
      else toast("回滚失败: "+r.error);
    };
    row.appendChild(btn); list.appendChild(row);
  }
}

// ---- 事件 ----
$("#sessions").onchange=e=>openFile(e.target.value);
$("#source").onchange=renderSessionOptions;
$("#reload").onclick=loadSessions;
$("#openPath").onclick=()=>openFile($("#pathInput").value.trim());
$("#pathInput").addEventListener("keydown",e=>{ if(e.key==="Enter") openFile(e.target.value.trim()); });
$("#saveBtn").onclick=save;
$("#undoBtn").onclick=undo; $("#redoBtn").onclick=redo;
$("#exportMd").onclick=()=>exportAs("md"); $("#exportTxt").onclick=()=>exportAs("txt");
$("#searchBtn").onclick=doSearch;
$("#searchInput").addEventListener("keydown",e=>{ if(e.key==="Enter") doSearch(); if(e.key==="Escape") $("#results").classList.remove("on"); });
$("#bakBtn").onclick=openBak; $("#bakClose").onclick=()=>$("#bakModal").classList.remove("on");
$("#bakModal").onclick=e=>{ if(e.target.id==="bakModal") $("#bakModal").classList.remove("on"); };
document.addEventListener("click",e=>{ if(!e.target.closest(".searchbox")) $("#results").classList.remove("on"); });
window.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==="s"){ e.preventDefault(); if(DIRTY) save(); }
  else if((e.ctrlKey||e.metaKey)&&e.key==="z"){ e.preventDefault(); undo(); }
  else if((e.ctrlKey||e.metaKey)&&(e.key==="y"||(e.shiftKey&&e.key==="Z"))){ e.preventDefault(); redo(); }
});
window.addEventListener("beforeunload",e=>{ if(DIRTY){ e.preventDefault(); e.returnValue=""; }});

loadSessions();
const AUTO=new URLSearchParams(location.search).get("file");
if(AUTO) openFile(AUTO);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Claude Code 会话记录编辑器")
    ap.add_argument("--file")
    ap.add_argument("--dir")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    if args.file:
        url += "?file=" + os.path.abspath(args.file)
    print("会话编辑器已启动: %s" % url)
    print("按 Ctrl+C 停止。")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        srv.shutdown()


if __name__ == "__main__":
    main()
