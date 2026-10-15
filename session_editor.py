# -*- coding: utf-8 -*-
"""
会话管理器 —— 本地 HTTP 服务(Claude Code / Cursor / Codex 会话的查看、编辑、互导)

页面在 web/(index.html / app.css / app.js), 版式参考 cc-switch 的会话管理:
  - 会话列表: 按项目或时间分组, 按来源 / 文字筛选; 标题取 custom-title / ai-title / 第一条提问
  - 阅读视图: 按「轮」组织(提问 → 执行过程 → 最终回复), 右侧对话目录, 会话内查找;
    轮次划分与视图数据由 reader_model.py 生成
  - 记录视图: 逐条查看 / 编辑原始记录
编辑操作: 改文本、删除(自动重链 parentUuid)、插入 / 复制 / 移动、截断、Fork、撤销 / 重做、备份回滚,
另有存到本地、导出到 Claude / Codex、导出 Markdown / TXT。

性能: 记录全部驻留后端内存, 浏览器按页取数据; 大块内容只下发预览, 展开时再取全文。

用法:
  "C:\\Program Files\\Python311\\python.exe" session_editor.py [--file 路径] [--dir 目录] [--port 8790] [--no-browser]

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
import reader_model as rm

# ---------------- 后端会话状态(单文件, 本地单用户) ----------------
STATE = {"path": None, "records": [], "dirty": False, "errors": [], "origin": "jsonl", "cwd": "",
         "style": "generic", "index": None, "rev": 0}

# 工具执行目录 / 本地会话保存目录 / 前端页面目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAVED_DIR = os.path.join(BASE_DIR, "saved_sessions")
WEB_DIR = os.path.join(BASE_DIR, "web")
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8")}
UNDO, REDO = [], []
UNDO_LIMIT = 300
LOCK = threading.Lock()
CAT_CACHE = rm.CatCache()
AGENT_NAMES = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor", "generic": "助手"}
READONLY_ORIGINS = {"cursor-store": "Cursor store.db 内容受完整性校验保护, 只读",
                    "codex-rollout": "Codex rollout 只读打开; 要编辑请先「存到本地」"}


def projects_root():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def cursor_root():
    return os.path.join(os.path.expanduser("~"), ".cursor", "projects")


def _under(ap, root):
    root = os.path.abspath(root)
    if ap.lower().startswith(root.lower() + os.sep):
        return True
    try:   # ~/.claude/projects 可能是目录联接, 用真实路径再比一次
        return os.path.realpath(ap).lower().startswith(os.path.realpath(root).lower() + os.sep)
    except OSError:
        return False


def _source_of(ap):
    if _under(ap, SAVED_DIR):
        return "saved"
    if _under(ap, projects_root()):
        return "claude"
    if _under(ap, cursor_root()):
        return "cursor"
    return "other"


def _skip_jsonl(ap):
    """子代理 transcript(agent-*.jsonl / subagents/)不单独列出, 和 cc-switch 一致。"""
    base = os.path.basename(ap)
    if base.startswith("agent-") or base == "journal.jsonl":
        return True
    return "subagents" in ap.replace("\\", "/").lower().split("/")


def _ms(sec):
    return int(sec * 1000) if sec else None


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
            if ap in seen or _skip_jsonl(ap):
                continue
            seen.add(ap)
            try:
                st = os.stat(ap)
            except OSError:
                continue
            src = _source_of(ap)
            m = rm.scan_jsonl_meta(ap)
            stem = os.path.splitext(os.path.basename(ap))[0]
            entry = {"path": ap, "name": os.path.basename(ap), "dir": os.path.dirname(ap),
                     "size": st.st_size, "mtime": st.st_mtime, "source": src,
                     "title": m.get("title"), "summary": m.get("summary"), "cwd": m.get("cwd") or "",
                     "created": m.get("created"), "last": m.get("last") or _ms(st.st_mtime),
                     "sessionId": m.get("sessionId") or stem}
            if src == "claude":
                entry["resume"] = "claude --resume %s" % entry["sessionId"]
            elif src == "cursor":
                rel = os.path.relpath(ap, cursor_root()).replace("\\", "/").split("/")
                entry["project"] = rel[0] if rel else ""
            elif src == "saved":
                entry["from"] = m.get("origin")
                entry["from_cwd"] = m.get("cwd")
            out.append(entry)
    # Cursor CLI 的 chat 会话(store.db, 可恢复的真实会话)
    try:
        for s in cursor_store.discover_chat_sessions():
            out.append({"path": s["path"], "name": "store.db", "dir": s["session_dir"], "size": s["size"],
                        "mtime": s["mtime"], "source": "cursor-chat", "cwd": s.get("cwd", ""),
                        "title": s["title"] or None, "created": s.get("created"), "last": _ms(s["mtime"]),
                        "sessionId": s["session_id"]})
    except Exception:
        pass
    # Codex CLI 会话(rollout, 索引在 state_5.sqlite threads)
    try:
        for s in codex_store.discover_codex_sessions():
            sid = s.get("session_id") or ""
            out.append({"path": s["path"], "name": os.path.basename(s["path"]), "dir": s["dir"],
                        "size": s["size"], "mtime": s["mtime"], "source": "codex", "cwd": s.get("cwd", ""),
                        "title": rm.one_line(s["name"], 80) if s.get("name") else None,
                        "last": _ms(s["mtime"]), "sessionId": sid,
                        "resume": ("codex resume %s" % sid) if sid else None})
    except Exception:
        pass
    out.sort(key=lambda x: x.get("last") or 0, reverse=True)
    return out


def detect_style(path, origin, records):
    """阅读视图用哪家的版式(步骤标题、头像、主题色)。"""
    if origin == "codex-rollout":
        return "codex"
    if origin == "cursor-store":
        return "cursor"
    src = _source_of(os.path.abspath(path)) if path else "other"
    if src in ("claude", "cursor"):
        return src
    meta = next((r for r in records[:3] if isinstance(r, dict) and r.get("type") == "cc_meta"), None)
    if meta:
        o = str(meta.get("origin") or "")
        if o.startswith("codex"):
            return "codex"
        if o.startswith("cursor"):
            return "cursor"
        if o == "claude":
            return "claude"
    for r in records[:50]:
        if isinstance(r, dict) and "uuid" in r and "sessionId" in r:
            return "claude"
        if isinstance(r, dict) and "role" in r and "message" in r:
            return "cursor"
    return "generic"


def invalidate():
    """记录变了: 丢掉轮次索引, 版本号 +1(前端据此判断目录是否过期)。"""
    STATE["index"] = None
    STATE["rev"] += 1


def get_index():
    if STATE["index"] is None:
        STATE["index"] = rm.build_index(STATE["records"], CAT_CACHE, STATE["style"], STATE["cwd"])
    return STATE["index"]


def reader_ctx():
    return {"style": STATE["style"], "cwd": STATE["cwd"]}


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
    invalidate()


def meta():
    origin = STATE.get("origin", "jsonl")
    return {"path": STATE["path"], "total": len(STATE["records"]), "dirty": STATE["dirty"],
            "errors": STATE["errors"], "canUndo": bool(UNDO), "canRedo": bool(REDO),
            "origin": origin, "cwd": STATE.get("cwd", ""), "rev": STATE["rev"],
            "style": STATE["style"], "agent": AGENT_NAMES.get(STATE["style"], "助手"),
            "readonly": READONLY_ORIGINS.get(origin)}


def _neighbor_skeleton(neighbor, role, text):
    """按相邻记录的风格造一条新消息(Claude 风格带 uuid 链, Cursor / 其它风格只有 role)。"""
    content = [{"type": "text", "text": text}]
    if isinstance(neighbor, dict) and "uuid" not in neighbor and ("role" in neighbor or "message" not in neighbor):
        return {"role": role, "message": {"content": content}}
    r = {"parentUuid": None, "isSidechain": False, "type": role,
         "message": {"role": role, "content": content}, "uuid": str(uuidlib.uuid4()),
         "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
    for k in ("userType", "entrypoint", "cwd", "sessionId", "version", "gitBranch"):
        if isinstance(neighbor, dict) and neighbor.get(k) is not None:
            r[k] = neighbor[k]
    return r


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
        self.send_header("Cache-Control", "no-store")
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
        if u.path in STATIC_FILES:
            fname, ctype = STATIC_FILES[u.path]
            try:
                with open(os.path.join(WEB_DIR, fname), "rb") as f:
                    return self._send(200, f.read(), ctype)
            except OSError as e:
                return self._send(500, "页面文件缺失: %s" % e, "text/plain; charset=utf-8")

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
                style = detect_style(path, origin, records)
                with LOCK:
                    STATE.update(path=os.path.abspath(path), records=records, dirty=False,
                                 errors=errors, origin=origin, cwd=cwd, style=style)
                    invalidate()
                    UNDO.clear()
                    REDO.clear()
                    return self._send(200, {"ok": True, "source": _source_of(os.path.abspath(path)), **meta()})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})

        if u.path == "/api/outline":
            with LOCK:
                if not STATE["path"]:
                    return self._send(400, {"ok": False, "error": "未打开会话"})
                idx = get_index()
                return self._send(200, {"ok": True, "rev": STATE["rev"], "turns": idx["entries"],
                                        "stats": idx["stats"], **meta()})

        if u.path == "/api/turns":
            try:
                ts = [int(x) for x in (q.get("ts", [""])[0]).split(",") if x.strip()]
            except ValueError:
                return self._send(400, {"ok": False, "error": "ts 非法"})
            mode = q.get("mode", ["all"])[0]
            with LOCK:
                if not STATE["path"]:
                    return self._send(400, {"ok": False, "error": "未打开会话"})
                idx = get_index()
                views = rm.turn_views(STATE["records"], idx, ts, reader_ctx(), mode)
                return self._send(200, {"ok": True, "rev": STATE["rev"], "turns": views})

        if u.path == "/api/step":
            def ref(name):
                v = q.get(name, [""])[0]
                if not v:
                    return None
                a, b = v.split(":")
                return {"idx": int(a), "b": int(b)}
            try:
                call, res = ref("call"), ref("res")
            except ValueError:
                return self._send(400, {"ok": False, "error": "参数非法"})
            with LOCK:
                n = len(STATE["records"])
                for r in (call, res):
                    if r and not (0 <= r["idx"] < n):
                        return self._send(400, {"ok": False, "error": "idx 越界"})
                try:
                    out = rm.step_detail(STATE["records"], call, res, reader_ctx())
                except Exception as e:
                    return self._send(500, {"ok": False, "error": str(e)})
                return self._send(200, {"ok": True, **out})

        if u.path == "/api/block":
            try:
                i = int(q.get("idx", ["-1"])[0])
                b = int(q.get("b", ["-1"])[0])
            except ValueError:
                return self._send(400, {"ok": False, "error": "参数非法"})
            field = q.get("f", ["text"])[0]
            with LOCK:
                if not (0 <= i < len(STATE["records"])):
                    return self._send(400, {"ok": False, "error": "idx 越界"})
                try:
                    text = rm.get_block(STATE["records"][i], b, field)
                except ValueError as e:
                    return self._send(400, {"ok": False, "error": str(e)})
                return self._send(200, {"ok": True, "text": text})

        if u.path == "/api/image":
            try:
                i = int(q.get("idx", ["-1"])[0])
                b = int(q.get("b", ["-1"])[0])
            except ValueError:
                return self._send(400, {"ok": False, "error": "参数非法"})
            with LOCK:
                if not (0 <= i < len(STATE["records"])):
                    return self._send(404, {"ok": False, "error": "idx 越界"})
                try:
                    data, mt = rm.image_bytes(STATE["records"][i], b)
                except Exception as e:
                    return self._send(404, {"ok": False, "error": str(e)})
            return self._send(200, data, mt)

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
            query = q.get("q", [""])[0]
            with LOCK:
                hits, total = rm.search(STATE["records"], query)
                if hits:
                    idx = get_index()
                    for h in hits:
                        h["t"] = rm.turn_of(idx, h["idx"])
                return self._send(200, {"ok": True, "q": query, "hits": hits, "total": total,
                                        "truncated": len(hits) >= 3000, "rev": STATE["rev"]})

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
                if fmt == "md":   # 按轮整理的可读 Markdown(提问 / 执行过程摘要 / 最终回复)
                    text = rm.transcript_markdown(
                        STATE["records"], get_index(), reader_ctx(), AGENT_NAMES.get(STATE["style"], "助手"),
                        include_steps=q.get("steps", ["1"])[0] != "0",
                        include_thinking=q.get("thinking", ["0"])[0] == "1")
                else:             # 逐条记录的纯文本(完整)
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
                invalidate()
                return self._send(200, {"ok": True, "idx": op.get("idx", op.get("to")), **meta()})

            if u.path == "/api/redo":
                if not REDO:
                    return self._send(200, {"ok": True, "noop": True, **meta()})
                op = REDO.pop()
                UNDO.append(_do_op(op))
                STATE["dirty"] = True
                invalidate()
                return self._send(200, {"ok": True, "idx": op.get("idx", op.get("to")), **meta()})

            if u.path == "/api/patch":
                i = need_idx()
                if i is None:
                    return self._send(400, {"ok": False, "error": "idx 越界"})
                try:
                    nr = rm.patched_record(STATE["records"][i], int(data.get("b", -1)),
                                           data.get("f", "text"), data.get("value"))
                except (ValueError, TypeError) as e:
                    return self._send(400, {"ok": False, "error": str(e)})
                apply_user_op({"type": "update", "idx": i, "record": nr})
                return self._send(200, {"ok": True, **meta()})

            if u.path == "/api/duplicate":
                i = need_idx()
                if i is None:
                    return self._send(400, {"ok": False, "error": "idx 越界"})
                src = STATE["records"][i]
                dup = copy.deepcopy(src)
                relink = isinstance(src, dict) and "uuid" in src
                if relink:
                    dup["uuid"] = str(uuidlib.uuid4())
                    dup["parentUuid"] = src.get("uuid")
                apply_user_op({"type": "insert", "idx": i + 1, "record": dup, "relink": relink})
                return self._send(200, {"ok": True, "idx": i + 1, **meta()})

            if u.path == "/api/insert-text":
                i = need_idx()
                where = data.get("where", "below")
                role = (data.get("role") or "user").strip()
                text = data.get("text") or ""
                if i is None and not (data.get("idx") == 0 and not STATE["records"]):
                    return self._send(400, {"ok": False, "error": "idx 越界"})
                if role not in ("user", "assistant", "system"):
                    return self._send(400, {"ok": False, "error": "角色需为 user / assistant / system"})
                nb = STATE["records"][i] if i is not None else None
                nr = _neighbor_skeleton(nb, role, text)
                at = (i + 1) if (i is not None and where == "below") else (i or 0)
                relink = "uuid" in nr and nb is not None
                if relink:
                    nr["parentUuid"] = nb.get("uuid") if where == "below" else nb.get("parentUuid")
                apply_user_op({"type": "insert", "idx": at, "record": nr, "relink": relink})
                return self._send(200, {"ok": True, "idx": at, **meta()})

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
                    invalidate()
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
