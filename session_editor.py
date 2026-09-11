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
import re
import shutil
import threading
import time
import uuid as uuidlib
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import cursor_store
import codex_store
import claude_store

# ---------------- 后端会话状态(单文件, 本地单用户) ----------------
STATE = {"path": None, "records": [], "dirty": False, "errors": [], "origin": "jsonl", "cwd": ""}

# 工具执行目录 / 本地会话保存目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAVED_DIR = os.path.join(BASE_DIR, "saved_sessions")
UNDO, REDO = [], []
UNDO_LIMIT = 300
LOCK = threading.Lock()


def projects_root():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def cursor_root():
    return os.path.join(os.path.expanduser("~"), ".cursor", "projects")


def _source_of(ap):
    if ap.startswith(os.path.abspath(SAVED_DIR)):
        return "saved"
    if ap.startswith(os.path.abspath(projects_root())):
        return "claude"
    if ap.startswith(os.path.abspath(cursor_root())):
        return "cursor"
    return "other"


def discover_sessions(extra_dir=None):
    roots = []
    if extra_dir:
        roots.append(extra_dir)
    roots.append(SAVED_DIR)
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
            src = _source_of(ap)
            entry = {"path": ap, "name": os.path.basename(ap), "dir": os.path.dirname(ap),
                     "size": st.st_size, "mtime": st.st_mtime, "source": src}
            if src == "saved":
                try:
                    first = json.loads(open(ap, encoding="utf-8").readline())
                    if isinstance(first, dict) and first.get("type") == "cc_meta":
                        entry["from"] = first.get("origin")
                        entry["from_cwd"] = first.get("cwd")
                except (ValueError, OSError):
                    pass
            out.append(entry)
    # Cursor CLI 的 chat 会话(store.db, 可恢复的真实会话)
    try:
        for s in cursor_store.discover_chat_sessions():
            out.append({"path": s["path"], "name": s["title"] or s["session_id"],
                        "dir": s["session_dir"], "size": s["size"], "mtime": s["mtime"],
                        "source": "cursor-chat", "cwd": s.get("cwd", "")})
    except Exception:
        pass
    # Codex CLI 会话(rollout, 索引在 state_5.sqlite threads)
    try:
        for s in codex_store.discover_codex_sessions():
            out.append({"path": s["path"], "name": s["name"], "dir": s["dir"], "size": s["size"],
                        "mtime": s["mtime"], "source": "codex", "cwd": s.get("cwd", "")})
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


def _infer_cwd(records):
    for r in records:
        if isinstance(r, dict) and r.get("cwd"):
            return r["cwd"]
    return ""


_NOISE_PREFIXES = ("<local-command-caveat>", "<command-name>", "<command-message>",
                   "<local-command-stdout>", "<local-command-stderr>")


def _record_text(rec):
    m = rec.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _clean_for_export(records):
    """导出前剔除非对话噪声: cc_meta 元数据、Claude 的斜杠命令/本地命令 caveat 记录。"""
    out = []
    for r in records:
        if not isinstance(r, dict):
            continue
        if r.get("type") == "cc_meta":
            continue
        if any(_record_text(r).strip().startswith(p) for p in _NOISE_PREFIXES):
            continue
        out.append(r)
    return out


def _role_name(r):
    return r.get("role") or (r.get("message") or {}).get("role") or r.get("type")


def _prepare_export(records, lean=True, max_chars=0, last_n=0):
    """按选项精简导出内容, 缓解目标 CLI 上下文过长。

    lean: 只保留 user/assistant, 并去掉 tool_use/tool_result/thinking 块(体积大头)。
    max_chars: 每个文本块上限(0=不限)。
    last_n: 只保留最近 N 条(0=全部)。
    """
    def cut(t):
        return t[:max_chars] + "…[截断]" if max_chars > 0 and len(t) > max_chars else t

    out = []
    for r in _clean_for_export(records):
        if lean and _role_name(r) not in ("user", "assistant"):
            continue
        m = r.get("message") or {}
        c = m.get("content")
        if isinstance(c, str):
            newc = cut(c)
        elif isinstance(c, list):
            parts = []
            for p in c:
                if not isinstance(p, dict):
                    continue
                t = p.get("type")
                if lean and t in ("tool_use", "tool_result", "thinking"):
                    continue
                if t in ("text", "thinking") and (p.get("text") or p.get("thinking")):
                    q = dict(p)
                    if "text" in q:
                        q["text"] = cut(q["text"])
                    if "thinking" in q:
                        q["thinking"] = cut(q["thinking"])
                    parts.append(q)
                else:
                    parts.append(p)
            if not parts:
                continue
            newc = parts
        else:
            continue
        nr = dict(r)
        nr["message"] = dict(m)
        nr["message"]["content"] = newc
        out.append(nr)
    if last_n > 0:
        out = out[-last_n:]
    return out


# ---------------- Fork: 分析分叉点之后的文件改动 ----------------
_EDIT_TOOLS = {"edit", "multiedit", "write", "notebookedit", "strreplace",
               "createfile", "searchreplace", "applypatch", "search_replace", "create_file"}
_BASH_TOOLS = {"bash", "shell", "terminal", "runcommand", "run_terminal_cmd", "runterminalcmd"}
_FILE_KEYS = ("file_path", "path", "notebook_path", "target_file", "filePath", "targetFile", "filepath")


def analyze_file_changes(records, start):
    """扫描 records[start:] 里的工具调用, 汇总被改动的文件。"""
    edited = {}
    bash = []
    for r in records[start:]:
        if not isinstance(r, dict):
            continue
        m = r.get("message") or {}
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for p in content:
            if not isinstance(p, dict) or p.get("type") != "tool_use":
                continue
            name = p.get("name") or ""
            nl = name.lower()
            inp = p.get("input") if isinstance(p.get("input"), dict) else {}
            if nl in _EDIT_TOOLS:
                fp = next((inp[k] for k in _FILE_KEYS if isinstance(inp.get(k), str)), None)
                if fp:
                    e = edited.setdefault(fp, {"tools": set(), "count": 0})
                    e["tools"].add(name)
                    e["count"] += 1
            elif nl in _BASH_TOOLS:
                cmd = inp.get("command") or inp.get("cmd") or ""
                if cmd:
                    bash.append(cmd)
    return {
        "edited": [{"file": f, "tools": sorted(v["tools"]), "count": v["count"]}
                   for f, v in edited.items()],
        "bash": bash,
    }


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
            "origin": STATE.get("origin", "jsonl"), "cwd": STATE.get("cwd", "")}


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
                    cwd = cursor_store.read_session_meta(os.path.dirname(path)).get("cwd", "")
                elif codex_store.is_rollout(path):
                    records, errors, origin = codex_store.read_rollout(path), [], "codex-rollout"
                    cwd = codex_store._strip_ext_prefix(codex_store.read_rollout_meta(path).get("cwd", ""))
                else:
                    records, errors, origin = (*load_jsonl(path), "jsonl")
                    cwd = _infer_cwd(records)
                with LOCK:
                    STATE.update(path=os.path.abspath(path), records=records, dirty=False,
                                 errors=errors, origin=origin, cwd=cwd)
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

        if u.path == "/api/fork-analysis":
            try:
                idx = int(q.get("idx", ["-1"])[0])
            except ValueError:
                return self._send(400, {"ok": False, "error": "idx 非法"})
            with LOCK:
                n = len(STATE["records"])
                if idx < 0 or idx >= n:
                    return self._send(400, {"ok": False, "error": "idx 越界"})
                res = analyze_file_changes(STATE["records"], idx + 1)
            return self._send(200, {"ok": True, **res})

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

            if u.path == "/api/export-to":
                if not STATE["records"]:
                    return self._send(400, {"ok": False, "error": "未打开会话"})
                target = data.get("target")
                cwd = data.get("cwd") or _infer_cwd(STATE["records"])
                title = data.get("title") or ""
                opts = data.get("options") or {}
                try:
                    recs = _prepare_export(STATE["records"],
                                           lean=bool(opts.get("lean", True)),
                                           max_chars=int(opts.get("maxChars", 0) or 0),
                                           last_n=int(opts.get("lastN", 0) or 0))
                except (TypeError, ValueError):
                    recs = _clean_for_export(STATE["records"])
                try:
                    if target == "codex":
                        res = codex_store.write_codex_session(recs, cwd=cwd, title=title)
                    elif target == "claude":
                        res = claude_store.write_claude_session(recs, cwd=cwd, title=title)
                    else:
                        return self._send(400, {"ok": False, "error": "target 需为 codex / claude"})
                except Exception as e:
                    return self._send(500, {"ok": False, "error": str(e)})
                return self._send(200, {"ok": True, "target": target, "cwd": cwd, **res})

            if u.path == "/api/save-local":
                if not STATE["records"]:
                    return self._send(400, {"ok": False, "error": "未打开会话"})
                src = STATE["path"] or "session"
                base = os.path.basename(src)
                if base.lower() == "store.db":
                    base = os.path.basename(os.path.dirname(src)) + ".jsonl"
                name = data.get("name") or base
                name = re.sub(r"[^\w.\-]", "_", name)
                if not name.endswith(".jsonl"):
                    name += ".jsonl"
                try:
                    os.makedirs(SAVED_DIR, exist_ok=True)
                    out_path = os.path.join(SAVED_DIR, name)
                    if os.path.exists(out_path):
                        root, ext = os.path.splitext(name)
                        out_path = os.path.join(SAVED_DIR, "%s.%d%s" % (root, int(time.time()), ext))
                    # 剥掉旧的来源元数据; 若之前保存过则沿用最初的来源, 否则按当前判定
                    prior = next((r for r in STATE["records"]
                                  if isinstance(r, dict) and r.get("type") == "cc_meta"), None)
                    recs = [r for r in STATE["records"]
                            if not (isinstance(r, dict) and r.get("type") == "cc_meta")]
                    if prior and prior.get("origin"):
                        origin = prior["origin"]
                    elif STATE.get("origin") == "cursor-store":
                        origin = "cursor-store"
                    else:
                        origin = _source_of(STATE["path"]) if STATE.get("path") else "jsonl"
                    meta_line = {"type": "cc_meta", "origin": origin,
                                 "source_path": (prior or {}).get("source_path") or STATE.get("path"),
                                 "cwd": STATE.get("cwd") or _infer_cwd(recs),
                                 "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    save_jsonl(out_path, [meta_line] + recs)
                except Exception as e:
                    return self._send(500, {"ok": False, "error": str(e)})
                return self._send(200, {"ok": True, "path": os.path.abspath(out_path),
                                        "count": len(recs)})

            if u.path == "/api/save":
                path = STATE["path"]
                if not path:
                    return self._send(400, {"ok": False, "error": "未打开文件"})
                if STATE.get("origin") == "cursor-store":
                    return self._send(400, {"ok": False,
                        "error": "Cursor store.db 为只读(内容受完整性校验保护, 不可改写)"})
                if STATE.get("origin") == "codex-rollout":
                    return self._send(400, {"ok": False,
                        "error": "Codex rollout 为只读打开; 请用「存到本地」另存为可编辑 jsonl"})
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
  .md{font:14px/1.65 inherit;word-break:break-word}
  .md p{margin:0 0 .7em}.md p:last-child{margin-bottom:0}
  .md h1,.md h2,.md h3,.md h4{margin:.9em 0 .4em;font-weight:650;line-height:1.3}
  .md h1{font-size:1.4em}.md h2{font-size:1.22em}.md h3{font-size:1.08em}.md h4{font-size:1em}
  .md ul,.md ol{margin:.3em 0 .7em;padding-left:1.6em}
  .md li{margin:.15em 0}
  .md blockquote{margin:.4em 0;padding:2px 10px;border-left:3px solid var(--accent);color:var(--muted)}
  .md pre.md-code{background:var(--chip);padding:10px 12px;border-radius:6px;max-height:420px;color:var(--text);font-size:12.5px}
  .md code{font:12.5px/1.5 ui-monospace,Consolas,monospace;background:var(--chip);padding:.1em .35em;border-radius:4px}
  .md pre.md-code code{background:none;padding:0}
  .md a{color:var(--accent)}
  .md table{border-collapse:collapse;margin:.5em 0;font-size:13px;max-width:100%;overflow:auto;display:block}
  .md th,.md td{border:1px solid var(--border);padding:4px 8px}
  .md th{background:var(--chip)}
  .md hr{border:none;border-top:1px solid var(--border);margin:.8em 0}
  .md img{max-width:100%;border-radius:6px}
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
  .erow{display:flex;gap:10px;align-items:center;margin:9px 0}
  .elabel{min-width:92px;color:var(--muted);font-size:13px}
  .eopt{display:inline-flex;gap:5px;align-items:center;cursor:pointer;margin-right:14px}
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
    <option value="codex">Codex 会话</option>
    <option value="saved">本地保存</option>
    <option value="other">其它</option>
  </select>
  <input id="sessFilter" placeholder="筛选(名称/目录/cwd)" title="按文件名、项目目录、cwd 过滤会话" style="width:150px">
  <select id="sessions" title="选择会话文件"></select>
  <button id="reload" title="刷新会话列表">↻</button>
  <input id="pathInput" placeholder="或粘贴 .jsonl 绝对路径" style="width:210px">
  <button id="openPath">打开</button>
  <div class="spacer"></div>
  <button id="undoBtn" title="撤销 Ctrl+Z" disabled>↶ 撤销</button>
  <button id="redoBtn" title="重做 Ctrl+Y" disabled>↷ 重做</button>
  <button id="bakBtn" title="备份管理/回滚">备份</button>
  <button id="saveLocalBtn" title="把当前会话(含改动)另存到工具目录的 saved_sessions/">存到本地</button>
  <button id="exportToBtn" title="导出到其它 CLI 的会话历史(Claude / Codex)">导出到…</button>
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
<div class="modal" id="exportModal"><div class="card">
  <h2>导出到其它 CLI</h2>
  <div class="erow"><span class="elabel">目标</span>
    <label class="eopt"><input type="radio" name="etarget" value="claude" checked> Claude Code</label>
    <label class="eopt"><input type="radio" name="etarget" value="codex"> Codex</label>
  </div>
  <div class="erow"><span class="elabel">工作目录 cwd</span><input id="ecwd" placeholder="决定归属工作区" style="flex:1"></div>
  <div class="erow"><span class="elabel">标题</span><input id="etitle" placeholder="留空取首条用户消息" style="flex:1"></div>
  <div class="erow"><span class="elabel">内容</span>
    <label class="eopt"><input type="checkbox" id="elean" checked> 精简(仅正文,去工具/思考)</label>
  </div>
  <div class="erow"><span class="elabel">每条上限</span><input id="emax" type="number" min="0" value="8000" style="width:90px"> 字符(0=不限)</div>
  <div class="erow"><span class="elabel">最近条数</span><input id="elastn" type="number" min="0" value="0" style="width:90px"> 条(0=全部)</div>
  <div class="hint" id="ehint" style="margin-top:6px"></div>
  <div style="text-align:right;margin-top:14px">
    <button id="exportCancel">取消</button>
    <button id="exportGo" class="primary">导出</button>
  </div>
</div></div>

<script>
let CUR_PATH="", TOTAL=0, CUR_PAGE=0, PAGE_SIZE=50, DIRTY=false;
let PAGE_ITEMS=[];               // [{idx, rec}]
let CUR_CWD="";
const ACTIVE_FILTER = new Set();
const PRE_LIMIT = 4000;
const SRC_LABEL = {claude:"Claude", cursor:"Cursor(jsonl)", "cursor-store":"Cursor 会话(store.db)",
                   "cursor-chat":"Cursor 会话(store.db)", codex:"Codex 会话", saved:"本地保存", jsonl:"jsonl", other:"其它"};

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
  if("cwd" in j) CUR_CWD=j.cwd;
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
  const q=($("#sessFilter").value||"").trim().toLowerCase();
  const list=ALL_SESSIONS.filter(s=>{
    if(src!=="all" && s.source!==src) return false;
    if(q){ const hay=(s.name+" "+(s.dir||"")+" "+(s.from_cwd||"")).toLowerCase(); if(!hay.includes(q)) return false; }
    return true;
  });
  const sel=$("#sessions");
  const tag={claude:"[Claude]",cursor:"[Cursor]","cursor-chat":"[Cursor·db]",codex:"[Codex]",saved:"[本地]",other:"[其它]"};
  sel.innerHTML=`<option value="">— 选择会话 (${list.length}) —</option>`;
  for(const s of list){
    const o=document.createElement("option"); o.value=s.path;
    const from = s.source==="saved"&&s.from ? `  ·  源:${SRC_LABEL[s.from]||s.from}` : "";
    o.textContent=`${tag[s.source]||""} ${s.name.slice(0,10)}…  ·  ${fmtSize(s.size)}  ·  ${fmtTime(s.mtime)}  ·  ${s.dir.split(/[\\/]/).pop()}${from}`;
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
  if(rec.type==="cc_meta"){ blocks.push({kind:"cc_meta",editable:false,part:{origin:rec.origin,cwd:rec.cwd,source_path:rec.source_path,saved_at:rec.saved_at}}); return blocks; }
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
  if(rec.type==="cc_meta") return "来源信息";
  return rec.role||(rec.message&&rec.message.role)||rec.type||"?";   // Cursor 顶层 role / Claude message.role
}


function escHtml(s){
  return (""+s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}
function renderMd(src){
  const fences=[];
  let raw=(""+(src||"")).replace(/\r\n/g,"\n");
  raw=raw.replace(/```([^\n]*)\n([\s\S]*?)```/g,(_,lang,code)=>{
    fences.push('<pre class="md-code"><code>'+escHtml(code.replace(/\n$/,""))+"</code></pre>");
    return "\n%%F"+(fences.length-1)+"%%\n";
  });
  const inline=t=>{
    t=escHtml(t);
    t=t.replace(/`([^`]+)`/g,"<code>$1</code>");
    t=t.replace(/!\[([^\]]*)\]\((https?:[^)\s]+)\)/g,'<img alt="$1" src="$2">');
    t=t.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
    t=t.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
    t=t.replace(/__([^_]+)__/g,"<strong>$1</strong>");
    t=t.replace(/~~([^~]+)~~/g,"<del>$1</del>");
    t=t.replace(/(^|[^*])\*([^*\n]+)\*/g,"$1<em>$2</em>");
    t=t.replace(/(^|[^_])_([^_\n]+)_/g,"$1<em>$2</em>");
    return t;
  };
  const fenceOf=l=>{ const m=/^%%F(\d+)%%$/.exec((l||"").trim()); return m?fences[+m[1]]:null; };
  const lines=raw.split("\n"), out=[];
  let i=0;
  while(i<lines.length){
    const l=lines[i], f=fenceOf(l);
    if(f){ out.push(f); i++; continue; }
    if(/^\s*$/.test(l)){ i++; continue; }
    const hm=/^(#{1,4})\s+(.+)$/.exec(l);
    if(hm){ const n=hm[1].length; out.push("<h"+n+">"+inline(hm[2])+"</h"+n+">"); i++; continue; }
    if(/^(-{3,}|\*{3,}|_{3,})$/.test(l.trim())){ out.push("<hr>"); i++; continue; }
    if(/^>\s?/.test(l)){
      const qs=[];
      while(i<lines.length && /^>\s?/.test(lines[i])){ qs.push(lines[i].replace(/^>\s?/, "")); i++; }
      out.push("<blockquote>"+qs.map(inline).join("<br>")+"</blockquote>"); continue;
    }
    if(/^\|.+\|/.test(l) && i+1<lines.length && /^\|?[\s:|-]+$/.test(lines[i+1]) && /---/.test(lines[i+1])){
      const split=row=>row.replace(/^\||\|$/g,"").split("|").map(x=>x.trim());
      const heads=split(l); i+=2;
      const rows=[];
      while(i<lines.length && /^\|.+\|/.test(lines[i])){ rows.push(split(lines[i])); i++; }
      out.push("<table><thead><tr>"+heads.map(h=>"<th>"+inline(h)+"</th>").join("")+"</tr></thead><tbody>"+
        rows.map(r=>"<tr>"+r.map(c=>"<td>"+inline(c)+"</td>").join("")+"</tr>").join("")+"</tbody></table>");
      continue;
    }
    if(/^\s*([-*+]|\d+\.)\s+/.test(l)){
      const ordered=/^\s*\d+\./.test(l), items=[];
      while(i<lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])){
        items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "")); i++;
      }
      const tag=ordered?"ol":"ul";
      out.push("<"+tag+">"+items.map(x=>{
        const t=/^\[(x|X| )\]\s+(.*)$/.exec(x);
        if(t) return "<li><input type=checkbox disabled"+(t[1]===" "?" ":" checked ")+"> "+inline(t[2])+"</li>";
        return "<li>"+inline(x)+"</li>";
      }).join("")+"</"+tag+">");
      continue;
    }
    const para=[l]; i++;
    while(i<lines.length){
      const n=lines[i];
      if(!n.trim() || fenceOf(n) || /^(#{1,4}\s|>\s?|-{3,}|\*{3,}|_{3,}|\s*([-*+]|\d+\.)\s+)/.test(n) || /^\|.+\|/.test(n)) break;
      para.push(n); i++;
    }
    out.push("<p>"+para.map(inline).join("<br>")+"</p>");
  }
  return out.join("");
}
function mountTextBlock(bd, label, b, idx, rec){
  const btn=document.createElement("button"); btn.className="more"; btn.textContent="编辑"; btn.title="编辑原文 / 预览 Markdown";
  label.appendChild(btn);
  const view=document.createElement("div"); view.className="md";
  const ta=document.createElement("textarea"); ta.className="text"; ta.style.display="none";
  let editing=false;
  const grow=()=>{ ta.style.height="auto"; ta.style.height=Math.min(ta.scrollHeight+2,600)+"px"; };
  const showView=()=>{
    editing=false; btn.textContent="编辑"; ta.style.display="none"; view.style.display="block";
    view.innerHTML=renderMd(b.get()||"");
  };
  const showEdit=()=>{
    editing=true; btn.textContent="预览"; view.style.display="none"; ta.style.display="block";
    ta.value=b.get()||""; grow(); ta.focus();
  };
  btn.onclick=()=>{ if(editing){ b.set(ta.value); scheduleUpdate(idx,rec); showView(); } else showEdit(); };
  view.addEventListener("dblclick", showEdit);
  ta.addEventListener("input",()=>{ b.set(ta.value); scheduleUpdate(idx,rec); grow(); });
  ta.addEventListener("keydown",e=>{ if(e.key==="Escape"){ b.set(ta.value); scheduleUpdate(idx,rec); showView(); } });
  bd.appendChild(view); bd.appendChild(ta); showView();
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
    label.textContent = b.kind==="cc_meta" ? "来源信息" : (b.kind+(b.name?(": "+b.name):"")+(b.editable?"":" (只读)"));
    bd.appendChild(label);
    if(b.kind==="cc_meta"){
      const p=b.part||{}; const esc=s=>(""+s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
      const rows=[["来源", SRC_LABEL[p.origin]||p.origin||"?"],["工作目录 cwd", p.cwd||"(未知)"],
                  ["源文件", p.source_path||"(未知)"],["保存时间", p.saved_at||""]];
      const t=document.createElement("div");
      t.innerHTML=rows.map(([k,v])=>`<div style="display:flex;gap:8px;margin:2px 0"><span style="color:var(--muted);min-width:92px">${k}</span><span style="word-break:break-all">${esc(v)}</span></div>`).join("");
      bd.appendChild(t); body.appendChild(bd); continue;
    }
    if(b.editable && (b.kind==="text"||b.kind==="summary")){
      mountTextBlock(bd, label, b, idx, rec);
    }else if(b.editable){
      const ta=document.createElement("textarea");
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
  await flushUpdates();
  let warn="";
  try{
    const a=await (await fetch(`/api/fork-analysis?idx=${idx}`)).json();
    if(a.ok){
      const ne=a.edited.length, nb=a.bash.length;
      if(ne||nb){
        warn="\n\n⚠ 分叉点之后有文件改动 —— Fork 只分叉对话, 不会还原文件:";
        if(ne){
          warn+=`\n\n· 编辑类改动(可用 git 还原) ${ne} 个文件:`;
          warn+="\n   "+a.edited.slice(0,15).map(e=>`${e.file}  [${e.tools.join('/')}×${e.count}]`).join("\n   ");
          if(ne>15) warn+=`\n   …另 ${ne-15} 个`;
        }
        if(nb){
          warn+=`\n\n· ${nb} 条 Bash 命令(无法从记录还原, 可能改过文件/产物)`;
          warn+="\n   "+a.bash.slice(0,5).map(c=>"$ "+c.split("\n")[0].slice(0,70)).join("\n   ");
          if(nb>5) warn+=`\n   …另 ${nb-5} 条`;
        }
        warn+="\n\n若要真正回到该分叉点, 请自行把这些文件还原到当时状态(建议用 git)。";
      }
    }
  }catch(e){}
  if(!confirm(`从 #${idx} fork 出新会话?\n将复制「开头到此条」共 ${idx+1} 条为一个新 sessionId 的独立文件(写入同目录), 当前会话不受影响。${warn}`)) return;
  const j=await (await fetch("/api/fork",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({idx})})).json();
  if(!j.ok){ toast("fork 失败: "+j.error); return; }
  await loadSessions();
  if(confirm(`已生成新会话:\n${j.path}\n\n${j.count} 条 · sessionId ${j.sessionId.slice(0,8)}…\n\n是否立即打开该 fork?`)) await openFile(j.path);
  else toast("已生成: "+j.path.split(/[\\/]/).pop());
}
function exportTarget(){ const el=document.querySelector('input[name=etarget]:checked'); return el?el.value:"claude"; }
function updateEhint(){
  $("#ehint").textContent = exportTarget()==="claude"
    ? "→ ~/.claude/projects/<cwd编码>/<sessionId>.jsonl;在该 cwd 下 “claude --resume” 可见(cwd 必填)。"
    : "→ ~/.codex/sessions/… 并登记历史;“codex resume” 可见。cwd 留空则不归属特定工作区。";
}
function openExport(){
  if(!CUR_PATH){ toast("请先打开会话"); return; }
  $("#ecwd").value = CUR_CWD||"";
  $("#etitle").value = "";
  updateEhint();
  $("#exportModal").classList.add("on");
}
async function doExport(){
  const target=exportTarget();
  const cwd=$("#ecwd").value.trim();
  const title=$("#etitle").value.trim();
  if(target==="claude" && !cwd){ toast("Claude 需要填 cwd 才能 resume"); return; }
  await flushUpdates();
  const options={ lean: $("#elean").checked,
                  maxChars: parseInt($("#emax").value,10)||0,
                  lastN: parseInt($("#elastn").value,10)||0 };
  const body={target, options}; if(cwd) body.cwd=cwd; if(title) body.title=title;
  const j=await (await fetch("/api/export-to",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json();
  if(!j.ok){ toast("导出失败: "+j.error); return; }
  $("#exportModal").classList.remove("on");
  let hint;
  if(target==="claude"){
    hint=`在该 cwd 下运行 “claude --resume” 应能看到(项目目录:\n${j.project_dir})`;
  }else{
    hint = j.registered
      ? `已登记到 Codex 会话索引(state_5.sqlite)。若 Codex 正开着, 请重启后在 “codex resume” 里查看。`
      : `⚠ 未能登记到 Codex 索引: ${j.register_error||"未知"}\n(rollout 已写入, 但可能不显示。若因数据库被占用, 请关闭 Codex 后重试。)`;
  }
  alert(`已导出到 ${target.toUpperCase()}:\n${j.path}\n\nsession_id: ${j.session_id}\n消息数: ${j.count}\ncwd: ${j.cwd||"(空)"}\n\n${hint}`);
}
async function saveLocal(){
  if(!CUR_PATH){ toast("请先打开会话"); return; }
  await flushUpdates();
  const dft=(CUR_PATH.split(/[\\/]/).pop()||"session").replace(/\.jsonl$/,"");
  const name=prompt("另存文件名(保存到工具目录 saved_sessions/):", dft);
  if(name===null) return;
  const j=await (await fetch("/api/save-local",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})})).json();
  if(!j.ok){ toast("保存失败: "+j.error); return; }
  await loadSessions();
  toast("已存到本地: "+j.path.split(/[\\/]/).pop()+" ("+j.count+" 条)");
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
$("#sessFilter").oninput=renderSessionOptions;
$("#reload").onclick=loadSessions;
$("#openPath").onclick=()=>openFile($("#pathInput").value.trim());
$("#pathInput").addEventListener("keydown",e=>{ if(e.key==="Enter") openFile(e.target.value.trim()); });
$("#saveBtn").onclick=save;
$("#saveLocalBtn").onclick=saveLocal;
$("#exportToBtn").onclick=openExport;
$("#exportGo").onclick=doExport;
$("#exportCancel").onclick=()=>$("#exportModal").classList.remove("on");
$("#exportModal").onclick=e=>{ if(e.target.id==="exportModal") $("#exportModal").classList.remove("on"); };
document.querySelectorAll('input[name=etarget]').forEach(r=> r.onchange=updateEhint);
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
