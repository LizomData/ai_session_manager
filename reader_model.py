# -*- coding: utf-8 -*-
"""
会话阅读视图(参考 cc-switch「会话管理」的阅读页)。

把各来源的记录(Claude jsonl / Cursor transcript / Cursor store.db / Codex rollout / 本地保存)
整理成按「轮」组织的视图: 提问 → 执行过程(工具调用 / 思考 / 过程说明) → 最终回复。

分工: 这里给记录分类、建轮次索引(对话目录 / 分页 / 查找定位用), 按需生成某几轮的视图模型
和步骤详情; 前端只负责渲染。所有修改仍按记录下标(idx)走原有的编辑 / 撤销通道, 本模块只读。
"""
import base64
import bisect
import json
import os
import re
from datetime import datetime, timedelta, timezone

# ---------------- 上限 ----------------
TEXT_MAX = 30000          # 提问 / 回复 / 说明单段下发上限, 超出时前端按需取全文
THINK_PREVIEW = 400       # 思考预览字数
FAIL_PREVIEW_LINES = 6    # 失败步骤常显的输出行数
FAIL_PREVIEW_CHARS = 600
SNIPPET = 160             # 目录摘要字数
TITLE_CHARS = 200         # 步骤标题字数
DETAIL_OUT_MAX = 20000    # 步骤详情里输出的下发上限
PARAMS_MAX = 6000         # 步骤详情里参数的下发上限
DIFF_MAX_LINES = 600
EVENT_DETAIL_MAX = 4000

# ---------------- 记录类别 ----------------
Q = "question"      # 真人提问(开启新一轮)
SLASH = "slash"     # 斜杠命令 / ! 命令(开启新一轮, 作为该轮的前置事件)
TOOLRES = "result"  # 工具结果
AGENT = "agent"     # 助手输出(文本 / 思考 / 工具调用)
EVENT = "event"     # 事件: 中断 / 压缩 / Hook 错误 / PR
INJ = "injected"    # 注入的上下文(系统提示、caveat、环境信息…), 阅读视图默认隐藏
NOISE = "noise"     # 非对话记录(快照、队列、标题、元数据…), 阅读视图不显示

INJ_PREFIXES = (
    "<local-command-caveat>", "<local-command-stdout>", "<local-command-stderr>",
    "<task-notification>", "<user_info>", "<environment_context>", "# AGENTS.md instructions",
    "<user_instructions>", "<recommended_plugins>", "<permissions instructions>",
    "<collaboration_mode>", "<skills_instructions>", "<apps_instructions>", "<plugins_instructions>",
    "Base directory for this skill:", "Caveat: The messages below were generated",
    "<bash-stdout>", "<bash-stderr>",
)
COMPACT_PREFIXES = (
    "This session is being continued from a previous conversation",
    "Your conversation was summarized due to context constraints",
)

_SR_RE = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S)
_UQ_RE = re.compile(r"<user_query>\s*(.*?)\s*(?:</user_query>|\Z)", re.S)
_CURSOR_TS_RE = re.compile(
    r"<timestamp>\s*(?:\w+,\s*)?([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4}),\s*(\d{1,2}):(\d{2})"
    r"\s*([AP]M)\s*\(UTC\s*([+-]\d{1,2})(?::?(\d{2}))?\)")
_OPEN_TAG_RE = re.compile(r"<([A-Za-z][\w-]*)[^>]*>")
_EXIT_RE = re.compile(r"^(?:Error:\s*)?Exit code:?\s*(-?\d+)", re.M)
_WALL_RE = re.compile(r"Wall time:?\s*([\d.]+)\s*s", re.I)
_PATCH_FILE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$", re.M)
_CODEX_CMD_RE = re.compile(r"shell_command\(\{\s*command:\s*\"((?:[^\"\\]|\\.)*)\"")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}

# ---------------- 工具归类 ----------------
_KIND = {}
for _n in ("bash", "shell", "exec_command", "shell_command", "local_shell", "run_terminal_cmd",
           "run_shell_command", "terminal", "runcommand", "write_stdin", "powershell"):
    _KIND[_n] = "shell"
for _n in ("read", "read_file", "readfile", "read_many_files", "view", "notebookread", "read_mcp_resource"):
    _KIND[_n] = "read"
for _n in ("grep", "glob", "toolsearch", "grep_search", "file_search", "codebase_search",
           "search_file_content", "list_directory", "list_dir", "ls", "find", "semanticsearch",
           "semantic_search", "listagents"):
    _KIND[_n] = "search"
for _n in ("edit", "multiedit", "notebookedit", "strreplace", "str_replace", "search_replace",
           "apply_patch", "replace", "edit_file", "edit_notebook", "delete", "delete_file", "deletefile"):
    _KIND[_n] = "edit"
for _n in ("write", "create_file", "write_file", "createfile"):
    _KIND[_n] = "write"
for _n in ("webfetch", "websearch", "web_search", "web_fetch", "google_web_search", "fetch", "open_page"):
    _KIND[_n] = "web"
for _n in ("agent", "task", "sendmessage", "taskstop", "spawn_agent", "send_message", "taskoutput"):
    _KIND[_n] = "agent"
for _n in ("askuserquestion", "request_user_input", "ask_user", "askquestion"):
    _KIND[_n] = "ask"
for _n in ("todowrite", "update_plan", "todo_write", "todoread", "write_todos"):
    _KIND[_n] = "todo"

# Codex 风格的动词(其余来源用工具原名, 显示为 Name(arg))
CODEX_VERBS = {"shell": "Ran", "read": "Read", "search": "Search", "edit": "Edited", "write": "Added",
               "web": "Searched", "mcp": "Called", "agent": "Spawned agent", "ask": "Asked",
               "todo": "Updated plan"}
PATH_KEYS = ("file_path", "path", "target_file", "notebook_path", "filePath", "filepath", "file")
SEARCH_PROGRAMS = {"rg", "grep", "fd", "find", "ls", "findstr", "select-string", "get-childitem", "dir"}


# ---------------- 基础取值 ----------------
def rec_role(rec):
    if not isinstance(rec, dict):
        return "?"
    m = rec.get("message")
    return rec.get("role") or (m.get("role") if isinstance(m, dict) else None) or rec.get("type") or "?"


def content_of(rec):
    m = rec.get("message") if isinstance(rec, dict) else None
    return m.get("content") if isinstance(m, dict) else None


def text_blocks(content):
    """[(b, text)]; b = -1 表示 content 本身就是字符串。"""
    if isinstance(content, str):
        return [(-1, content)]
    out = []
    if isinstance(content, list):
        for b, p in enumerate(content):
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
                out.append((b, p["text"]))
    return out


def image_blocks(content):
    out = []
    if isinstance(content, list):
        for b, p in enumerate(content):
            if isinstance(p, dict) and p.get("type") == "image":
                src = p.get("source") if isinstance(p.get("source"), dict) else {}
                out.append((b, src.get("media_type") or "image/png"))
    return out


def ts_ms(v):
    if isinstance(v, (int, float)) and v > 0:
        return int(v if v > 1e12 else v * 1000)
    if isinstance(v, str) and v:
        try:
            return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def strip_reminders(t):
    return _SR_RE.sub("", t).strip() if "<system-reminder>" in t else t.strip()


def one_line(text, limit=TITLE_CHARS):
    if not isinstance(text, str):
        return ""
    s = text.strip()
    if not s:
        return ""
    lines = s.splitlines()
    first = lines[0].rstrip()
    more = any(x.strip() for x in lines[1:])
    if len(first) > limit:
        return first[:limit] + "…"
    return first + ("…" if more else "")


def snippet(text, n=SNIPPET):
    """去掉 Markdown 记号, 只留可读文字作目录 / 列表摘要。"""
    if not text:
        return ""
    s = text[:n * 8]
    s = re.sub(r"```[\s\S]*?(```|$)", " ", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[#>*`~|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n] + ("…" if len(s) > n else "")


def short_path(p, cwd):
    """工作目录之下显示相对路径(工作目录本身为 .), 家目录下显示 ~/…"""
    if not isinstance(p, str) or not p:
        return ""
    q = p.replace("\\", "/")
    if cwd:
        c = cwd.replace("\\", "/").rstrip("/")
        if c and q.lower() == c.lower():
            return "."
        if c and q.lower().startswith(c.lower() + "/"):
            return q[len(c) + 1:]
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if home and q.lower().startswith(home.lower() + "/"):
        return "~/" + q[len(home) + 1:]
    return p


def _tag(text, tag):
    o, c = "<%s>" % tag, "</%s>" % tag
    s = text.find(o)
    if s < 0:
        return None
    s += len(o)
    e = text.find(c, s)
    return text[s:] if e < 0 else text[s:e]


def slash_command(t):
    """<command-name>/compact</command-name><command-args>…</command-args> → '/compact …'"""
    if t.startswith("<bash-input>"):
        cmd = (_tag(t, "bash-input") or "").strip()
        return ("!" + cmd) if cmd else None
    if not t.startswith("<command-"):
        return None
    name = (_tag(t, "command-name") or "").strip()
    if not name:
        return None
    args = (_tag(t, "command-args") or "").strip()
    return name + (" " + args if args else "")


def is_injected_text(t):
    if t.startswith(INJ_PREFIXES) or "<local-command-caveat>" in t[:300]:
        return True
    m = _OPEN_TAG_RE.match(t)   # 整段被一对自定义标签包住的(<environment_context>…</environment_context> 之类)
    return bool(m) and t.rstrip().endswith("</%s>" % m.group(1))


def injected_label(t):
    t = (t or "").strip()
    m = re.match(r"#\s*(AGENTS\.md|CLAUDE\.md|GEMINI\.md)\b", t)
    if m:
        return m.group(1)
    m = _OPEN_TAG_RE.match(t)
    if m:
        return m.group(1)
    first = t.split("\n", 1)[0]
    return first[:32] + ("…" if len(first) > 32 else "")


def cursor_query(text):
    m = _UQ_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def cursor_ts(text):
    """<timestamp>Wednesday, Sep 9, 2026, 2:22 PM (UTC+8)</timestamp> → ms"""
    m = _CURSOR_TS_RE.search(text[:400])
    if not m:
        return None
    mon = _MONTHS.get(m.group(1))
    if not mon:
        return None
    try:
        hour = int(m.group(4)) % 12 + (12 if m.group(6) == "PM" else 0)
        off = int(m.group(7))
        mins = int(m.group(8) or 0)
        tz = timezone(timedelta(hours=off, minutes=mins if off >= 0 else -mins))
        dt = datetime(int(m.group(3)), mon, int(m.group(2)), hour, int(m.group(5)), tzinfo=tz)
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


# ---------------- 分类 ----------------
def classify(rec):
    """→ (类别, 附加信息 dict | None)"""
    if not isinstance(rec, dict):
        return NOISE, None
    t = rec.get("type")
    if t == "system":
        return _classify_system(rec)
    if t == "pr-link":
        url = rec.get("prUrl")
        if isinstance(url, str) and url.strip():
            num = rec.get("prNumber")
            return EVENT, {"ek": "pr", "label": ("已关联 PR #%s" % num) if num else "已关联 PR", "url": url}
        return NOISE, None
    m = rec.get("message")
    if not isinstance(m, dict) and "role" not in rec:
        return NOISE, None
    if rec.get("isSidechain"):
        return INJ, {"label": "子代理消息"}
    role = rec_role(rec)
    if role == "assistant":
        return AGENT, None
    if role == "tool":
        return TOOLRES, None
    if role != "user":
        return INJ, {"label": str(role)}
    return _classify_user(rec)


def _classify_system(rec):
    st = rec.get("subtype")
    if st == "compact_boundary":
        return EVENT, {"ek": "compact", "label": "上下文已压缩"}
    if st == "stop_hook_summary":
        errs = rec.get("hookErrors")
        if isinstance(errs, list) and errs:
            return EVENT, {"ek": "hook", "label": "Hook 执行出错", "detail": "\n".join(str(e) for e in errs)}
        return NOISE, None
    if st == "local_command":
        return INJ, {"label": "本地命令输出"}
    if isinstance(rec.get("message"), dict):
        return INJ, {"label": "system"}
    return NOISE, None


def _classify_user(rec):
    content = content_of(rec)
    if rec.get("isCompactSummary"):
        return EVENT, {"ek": "compact", "label": "上下文已压缩", "detail": True}
    has_result = isinstance(content, list) and any(
        isinstance(p, dict) and p.get("type") == "tool_result" for p in content)
    texts = text_blocks(content)
    first = next((x.strip() for _, x in texts if x.strip()), "")
    if has_result:
        return TOOLRES, ({"aborted": True} if first.startswith("[Request interrupted") else None)
    if rec.get("isMeta"):
        return INJ, {"label": injected_label(first) or "meta"}
    images = image_blocks(content)
    if not first and not images:
        return NOISE, None
    if first:
        if first.startswith("[Request interrupted") or first.startswith("<turn_aborted>"):
            return EVENT, {"ek": "abort", "label": "已中断"}
        cmd = slash_command(first)
        if cmd:
            return SLASH, {"ek": "slash", "label": ("执行了 " if cmd.startswith("!") else "运行了 ") + cmd}
        joined = "\n\n".join(x for _, x in texts)
        if "<user_query>" in joined:
            return Q, {"text": cursor_query(joined), "cts": cursor_ts(joined)}
        if first.startswith(COMPACT_PREFIXES):
            return EVENT, {"ek": "compact", "label": "上下文已压缩", "detail": True}
        if is_injected_text(first):
            return INJ, {"label": injected_label(first)}
        shown = "\n\n".join(s for s in (strip_reminders(x) for _, x in texts) if s)
        if not shown and not images:
            return INJ, {"label": "system-reminder"}
        return Q, {"text": shown}
    return Q, {"text": ""}


class CatCache:
    """记录 → 类别 的缓存(按对象身份); 大会话每次修改后重建索引时只重算变过的记录。"""

    def __init__(self):
        self._m = {}

    def classify_all(self, records):
        old = self._m
        new = {}
        out = []
        for r in records:
            k = id(r)
            e = old.get(k)
            if e is None or e[0] is not r:
                e = (r, classify(r))
            new[k] = e
            out.append(e[1])
        self._m = new
        return out


# ---------------- 工具: 归类 / 标题 / 改动 ----------------
def _s(inp, *keys):
    if not isinstance(inp, dict):
        return ""
    for k in keys:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return " ".join(v)
    return ""


def _first_str(inp):
    if isinstance(inp, dict):
        for v in inp.values():
            if isinstance(v, str) and v.strip():
                return one_line(v, 120)
    return ""


def _shell_command(inp):
    if isinstance(inp, dict):
        v = inp.get("command", inp.get("cmd", inp.get("script")))
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            if len(v) >= 3 and v[0].split("/")[-1] in ("bash", "sh", "zsh", "pwsh", "powershell") \
                    and v[1] in ("-lc", "-c", "-Command"):
                return v[2]
            return " ".join(v)
        if isinstance(v, str):
            return v
    if isinstance(inp, str):
        return inp
    return ""


def _codex_exec_command(code):
    m = _CODEX_CMD_RE.search(code)
    if not m:
        return None
    try:
        return json.loads('"%s"' % m.group(1))
    except ValueError:
        return m.group(1)


def _is_search_cmd(cmd):
    first = (cmd or "").strip().split(None, 1)
    prog = first[0].replace("\\", "/").split("/")[-1].lower() if first else ""
    return prog in SEARCH_PROGRAMS


def _generic_kind(lname):
    if lname.startswith("web"):
        return "web"
    if any(k in lname for k in ("shell", "bash", "exec", "terminal", "command")):
        return "shell"
    if lname.startswith("read"):
        return "read"
    if any(k in lname for k in ("grep", "glob", "search")):
        return "search"
    if any(k in lname for k in ("edit", "patch", "replace")):
        return "edit"
    if lname.startswith("write") or lname.startswith("create"):
        return "write"
    return "other"


def _patch_text(inp):
    if isinstance(inp, str):
        return inp
    if isinstance(inp, dict):
        v = inp.get("input", inp.get("patch"))
        if isinstance(v, str):
            return v
    return None


def _change_paths(inp):
    p = _s(inp, *PATH_KEYS)
    if p:
        return [p]
    patch = _patch_text(inp)
    if patch and "*** " in patch:
        return [m.group(2) for m in _PATCH_FILE_RE.finditer(patch)]
    return []


def _first_key(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str):
            return v
    return None


def _diff_pairs(inp):
    """→ (旧新文本对列表, apply_patch 文本); 两者其一有效。"""
    if isinstance(inp, dict):
        edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
        pairs = []
        for e in edits:
            if not isinstance(e, dict):
                continue
            old = _first_key(e, ("old_string", "oldString", "old_str", "oldText"))
            new = _first_key(e, ("new_string", "newString", "new_str", "newText"))
            if old is not None or new is not None:
                pairs.append((old or "", new or ""))
        if pairs:
            return pairs, None
        c = _first_key(inp, ("content", "contents", "file_text", "text", "code"))
        if c is not None:
            return [("", c)], None
    patch = _patch_text(inp)
    if patch and "*** " in patch:
        return None, patch
    return [], None


def _line_change(old, new):
    ol, nl = old.splitlines(), new.splitlines()
    p = 0
    while p < len(ol) and p < len(nl) and ol[p] == nl[p]:
        p += 1
    s = 0
    while s < len(ol) - p and s < len(nl) - p and ol[-1 - s] == nl[-1 - s]:
        s += 1
    return len(nl) - p - s, len(ol) - p - s, p, s


def diff_counts(inp):
    pairs, patch = _diff_pairs(inp)
    a = d = 0
    if patch is not None:
        for ln in patch.splitlines():
            if ln.startswith(("+++", "---", "***")):
                continue
            if ln.startswith("+"):
                a += 1
            elif ln.startswith("-"):
                d += 1
        return a, d
    for old, new in pairs or []:
        x, y, _, _ = _line_change(old, new)
        a += x
        d += y
    return a, d


def diff_lines(inp, cwd, limit=DIFF_MAX_LINES):
    """改动的展示行: [{t: file|gap|add|del|ctx, s}], 是否截断。"""
    pairs, patch = _diff_pairs(inp)
    out = []
    if patch is not None:
        ops = {"Add": "新增", "Update": "修改", "Delete": "删除"}
        for ln in patch.splitlines():
            if ln.startswith("*** Begin Patch") or ln.startswith("*** End Patch"):
                continue
            m = re.match(r"\*\*\* (Add|Update|Delete) File: (.+)", ln)
            if m:
                out.append({"t": "file", "s": "%s %s" % (ops[m.group(1)], short_path(m.group(2).strip(), cwd))})
                continue
            if ln.startswith("*** Move to: "):
                out.append({"t": "file", "s": "移动到 " + short_path(ln[13:].strip(), cwd)})
                continue
            if ln.startswith("@@"):
                if out and out[-1]["t"] not in ("file", "gap"):
                    out.append({"t": "gap"})
                continue
            if ln.startswith("+"):
                out.append({"t": "add", "s": ln[1:]})
            elif ln.startswith("-"):
                out.append({"t": "del", "s": ln[1:]})
            else:
                out.append({"t": "ctx", "s": ln[1:] if ln.startswith(" ") else ln})
            if len(out) >= limit:
                return out, True
        return out, False
    for k, (old, new) in enumerate(pairs or []):
        if k:
            out.append({"t": "gap"})
        ol, nl = old.splitlines(), new.splitlines()
        _, _, p, s = _line_change(old, new)
        out += [{"t": "ctx", "s": x} for x in ol[max(0, p - 3):p]]
        out += [{"t": "del", "s": x} for x in ol[p:len(ol) - s]]
        out += [{"t": "add", "s": x} for x in nl[p:len(nl) - s]]
        out += [{"t": "ctx", "s": x} for x in ol[len(ol) - s:len(ol) - s + 3]]
        if len(out) >= limit:
            return out[:limit], True
    return out, False


def tool_info(name, inp, ctx, full):
    """→ {tk, paths[, verb, target, detail, add, del]}; 轻量模式只算归类和改动路径(统计用)。"""
    lname = (name or "").lower()
    tk = _KIND.get(lname)
    server = tool = None
    if lname.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 2 else ""
        tool = "__".join(parts[2:]) if len(parts) > 2 else name
        tk = "mcp"
    cmd = None
    if lname == "exec" and isinstance(inp, str):   # Codex: exec 里是 JS, 取出 shell 命令
        cmd = _codex_exec_command(inp)
        tk = "shell" if cmd else "other"
    if tk is None:
        tk = _generic_kind(lname)
    if tk == "shell":
        if cmd is None:
            cmd = _shell_command(inp)
        if _is_search_cmd(cmd):
            tk = "search"
    out = {"tk": tk, "paths": _change_paths(inp) if tk in ("edit", "write") else []}
    if not full:
        return out

    cwd = ctx.get("cwd")
    verb_style = ctx.get("style") == "codex"
    verb = name or "工具"
    if ctx.get("style") == "claude" and name in ("Edit", "MultiEdit"):
        verb = "Update"     # Claude Code CLI 把 Edit 显示为 Update
    target = detail = ""
    if tk == "mcp":
        verb = "%s.%s" % (server, tool)
        target = _first_str(inp)
    elif cmd is not None and tk in ("shell", "search"):
        target = one_line(cmd)
        detail = one_line(_s(inp, "description", "explanation", "justification"), 120)
    elif tk == "read":
        target = short_path(_s(inp, *PATH_KEYS), cwd)
        off, lim = (inp.get("offset"), inp.get("limit")) if isinstance(inp, dict) else (None, None)
        if isinstance(off, int) and isinstance(lim, int) and lim > 0:
            target += ":%d-%d" % (off, off + lim - 1)
        elif isinstance(off, int):
            target += ":%d" % off
        elif isinstance(lim, int) and lim > 0:
            target += ":1-%d" % lim
    elif tk == "search":
        pat = _s(inp, "pattern", "query", "glob_pattern", "regex", "search_term", "q", "name")
        where = _s(inp, "path", "target_directory", "directory", "dir", "glob", "include")
        target = one_line(pat) + ((" in " + short_path(where, cwd)) if where and where != pat else "")
        detail = _s(inp, "output_mode")
    elif tk in ("edit", "write"):
        paths = out["paths"]
        target = ("%d 个文件" % len(paths)) if len(paths) > 1 else (short_path(paths[0], cwd) if paths else "")
        out["add"], out["del"] = diff_counts(inp)
        if verb_style:
            patch = _patch_text(inp) or ""
            ops = {m.group(1) for m in _PATCH_FILE_RE.finditer(patch)}
            if ops == {"Add"}:
                verb = "Added"
            elif ops == {"Delete"}:
                verb = "Deleted"
    elif tk == "web":
        target = one_line(_s(inp, "url", "query", "search_query", "q"))
    elif tk == "agent":
        target = one_line(_s(inp, "description", "prompt", "message", "task", "name"))
        detail = _s(inp, "subagent_type", "model", "agent_type")
    elif tk == "ask":
        qs = inp.get("questions") if isinstance(inp, dict) else None
        q0 = qs[0].get("question") if isinstance(qs, list) and qs and isinstance(qs[0], dict) else None
        target = one_line(q0 if isinstance(q0, str) else _s(inp, "question", "prompt"))
    elif tk == "todo":
        items = None
        if isinstance(inp, dict):
            items = inp.get("todos", inp.get("plan", inp.get("items")))
        target = ("%d 项" % len(items)) if isinstance(items, list) else ""
    else:
        target = _first_str(inp) if isinstance(inp, dict) else one_line(inp if isinstance(inp, str) else "", 120)
    if verb_style:
        if tk == "mcp":
            target = "%s(%s)" % (verb, target)
            verb = "Called"
        elif tk not in ("edit", "write") or verb == name:
            verb = CODEX_VERBS.get(tk, name or "工具")
    out.update(verb=verb, target=target[:TITLE_CHARS], detail=detail)
    return out


# ---------------- 工具结果 ----------------
def result_items(rec):
    content = content_of(rec)
    out = []
    if isinstance(content, list):
        for b, p in enumerate(content):
            if isinstance(p, dict) and p.get("type") == "tool_result":
                out.append((b, p))
    elif isinstance(content, str) and rec_role(rec) == "tool":
        out.append((-1, {"type": "tool_result", "content": content}))
    return out


def result_text(p):
    c = p.get("content", p.get("output"))
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str))
    if c is None:
        return ""
    return json.dumps(c, ensure_ascii=False)


def _result_head(p, n=400):
    c = p.get("content", p.get("output"))
    if isinstance(c, str):
        return c[:n]
    if isinstance(c, list):
        for x in c:
            if isinstance(x, dict) and isinstance(x.get("text"), str):
                return x["text"][:n]
        return ""
    if c is None:
        return ""
    return json.dumps(c, ensure_ascii=False)[:n]


def _preview(text, lines, chars):
    parts = text.split("\n", lines)
    s = "\n".join(parts[:lines])[:chars]
    return s


def _attach_result(step, i, b, p, ts, tur, full):
    step["res"] = {"idx": i, "b": b}
    head = _result_head(p)
    st = "success"
    if p.get("is_error") is True:
        st = "error"
    if head.lstrip().startswith("[Request interrupted") or (tur and tur.get("interrupted") is True):
        st = "interrupted"
    elif head.startswith("Script failed"):
        st = "error"
    m = _EXIT_RE.search(head)
    if m:
        ec = int(m.group(1))
        step["exit"] = ec
        if ec != 0 and st == "success" and step.get("tk") in ("shell", "search"):
            st = "error"
    step["st"] = st
    if not full:
        return
    w = _WALL_RE.search(head)
    if w:
        try:
            step["dur"] = int(float(w.group(1)) * 1000)
        except ValueError:
            pass
    elif step.get("tk") in ("shell", "search") and step.get("cts") and ts and ts >= step["cts"]:
        step["dur"] = ts - step["cts"]
    text = result_text(p)
    step["lines"] = (text.count("\n") + 1) if text.strip() else 0
    if st in ("error", "interrupted"):
        step["pv"] = _preview(text, FAIL_PREVIEW_LINES, FAIL_PREVIEW_CHARS)


def cipher_of(p):
    """加密思考块的密文原文(按原样返回, 不做任何解码)。"""
    v = p.get("signature") or p.get("data") or p.get("encrypted_content") or ""
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def agent_items(rec, i):
    content = content_of(rec)
    if isinstance(content, str):
        return [("text", i, -1, content)] if content.strip() else []
    out = []
    if isinstance(content, list):
        for b, p in enumerate(content):
            if not isinstance(p, dict):
                continue
            ty = p.get("type")
            if ty == "text":
                tx = p.get("text")
                if isinstance(tx, str) and tx.strip():
                    out.append(("text", i, b, tx))
            elif ty in ("thinking", "redacted_thinking", "reasoning"):
                tx = p.get("thinking") or p.get("text") or ""
                if not isinstance(tx, str):
                    tx = ""
                if tx.strip():
                    out.append(("think", i, b, tx, False, 0))
                elif ty == "redacted_thinking" or p.get("signature") or p.get("redacted") or p.get("encrypted_content"):
                    # 正文为空、只有密文: 照 cc-switch 显示一行「思考内容不可见」, 可展开看密文原文
                    out.append(("think", i, b, "", True, len(cipher_of(p))))
            elif ty in ("tool_use", "server_tool_use"):
                out.append(("call", i, b, p))
            elif ty == "image":
                out.append(("image", i, b, None))
    return out


# ---------------- 轮 ----------------
def _event(i, info, rec, full):
    ev = {"id": "e%d" % i, "idx": i, "ek": info.get("ek"), "label": info.get("label", "")}
    if info.get("url"):
        ev["url"] = info["url"]
    d = info.get("detail")
    if d is True:   # 正文就是记录文本(压缩摘要)
        if full:
            text = "\n\n".join(x for _, x in text_blocks(content_of(rec)))
            ev["detail"] = text[:EVENT_DETAIL_MAX]
            ev["dtotal"] = len(text)
            ev["blocks"] = [{"b": b, "len": len(x)} for b, x in text_blocks(content_of(rec))]
    elif d and full:
        ev["detail"] = d[:EVENT_DETAIL_MAX]
        ev["dtotal"] = len(d)
    return ev


def _question(i, rec, info, ts, full):
    content = content_of(rec)
    text = (info or {}).get("text", "")
    q = {"idx": i, "ts": (info or {}).get("cts") or ts, "total": len(text)}
    imgs = image_blocks(content)
    if full:
        q["text"] = text[:TEXT_MAX]
        q["blocks"] = [{"b": b, "len": len(x)} for b, x in text_blocks(content)]
        q["images"] = [{"idx": i, "b": b, "mt": mt} for b, mt in imgs]
    else:
        q["snip"] = snippet(text) or ("（图片）" if imgs else "")
    return q


def _rec_chars(rec):
    return len(whole_text(rec))


def walk_turn(records, cats, first, last, t, ctx, full):
    """把 records[first..last] 组织成一轮。full=False 时只算目录 / 统计需要的字段。"""
    turn = {"t": t, "key": "t%d" % t, "first": first, "last": last, "q": None,
            "lead": [], "steps": [], "final": None, "trail": [], "inj": [],
            "aborted": False, "ts": None, "endTs": None, "ats": None, "model": None}
    items = []          # ("text", idx, b, text) / ("step", step) / ("event", ev)
    calls = {}          # tool_use id → step
    pending = []        # 还没配上结果的调用(结果不带 id 时按先后配对)
    for i in range(first, last + 1):
        cat, info = cats[i]
        if cat == NOISE:
            continue
        rec = records[i]
        ts = ts_ms(rec.get("timestamp"))
        if ts:
            if turn["ts"] is None:
                turn["ts"] = ts
            turn["endTs"] = ts
        if cat == INJ:
            turn["inj"].append({"idx": i, "label": (info or {}).get("label", ""),
                                "chars": _rec_chars(rec) if full else 0})
        elif cat == SLASH:
            turn["lead"].append(_event(i, info, rec, full))
        elif cat == Q:
            if turn["q"] is None:
                turn["q"] = _question(i, rec, info, ts, full)
        elif cat == EVENT:
            ev = _event(i, info, rec, full)
            if info.get("ek") == "abort":
                turn["aborted"] = True
            if turn["q"] is None and not items:
                turn["lead"].append(ev)
            else:
                items.append(("event", ev))
        elif cat == AGENT:
            m = rec.get("message")
            if isinstance(m, dict) and isinstance(m.get("model"), str) and m["model"] \
                    and not m["model"].startswith("<"):
                turn["model"] = m["model"]
            for it in agent_items(rec, i):
                if turn["ats"] is None and ts:
                    turn["ats"] = ts
                kind = it[0]
                if kind == "text":
                    items.append(it)
                elif kind == "think":
                    st = {"kind": "thinking", "id": "%d:%d" % (i, it[2]), "idx": i, "b": it[2],
                          "chars": len(it[3]), "red": it[4], "cipher": it[5]}
                    dur = rec.get("thinkingDurationMs")
                    if isinstance(dur, (int, float)) and dur > 0:
                        st["dur"] = int(dur)
                    if full and not it[4]:
                        st["pv"] = it[3][:THINK_PREVIEW]
                    items.append(("step", st))
                elif kind == "call":
                    p = it[3]
                    inp = p.get("input")
                    if inp is None and "arguments" in p:
                        inp = p.get("arguments")
                    st = {"kind": "tool", "id": "%d:%d" % (i, it[2]), "name": p.get("name") or "",
                          "call": {"idx": i, "b": it[2]}, "res": None, "st": "unknown", "cts": ts}
                    st.update(tool_info(st["name"], inp, ctx, full))
                    items.append(("step", st))
                    cid = p.get("id")
                    if cid:
                        calls[cid] = st
                    pending.append(st)
                else:
                    items.append(("step", {"kind": "image", "id": "%d:%d" % (i, it[2]), "idx": i, "b": it[2]}))
        elif cat == TOOLRES:
            if info and info.get("aborted"):
                turn["aborted"] = True
            tur = rec.get("toolUseResult") if isinstance(rec.get("toolUseResult"), dict) else None
            for b, p in result_items(rec):
                cid = p.get("tool_use_id")
                owner = calls.get(cid) if cid else None
                if owner is None and not cid:
                    owner = next((s for s in pending if s["res"] is None), None)
                if owner is not None and owner["res"] is None:
                    _attach_result(owner, i, b, p, ts, tur, full)
                    try:
                        pending.remove(owner)
                    except ValueError:
                        pass
                else:
                    orphan = {"kind": "tool", "id": "%d:%d" % (i, b), "name": "", "tk": "other", "paths": [],
                              "verb": "工具输出", "target": "", "detail": "", "call": None, "res": None,
                              "st": "success"}
                    _attach_result(orphan, i, b, p, ts, tur, full)
                    items.append(("step", orphan))

    # 最终回复: 本轮最后一个工具调用之后的文本
    last_call = -1
    for k, it in enumerate(items):
        if it[0] == "step" and it[1]["kind"] == "tool" and it[1].get("call"):
            last_call = k
    finals = [it for k, it in enumerate(items) if k > last_call and it[0] == "text"]
    steps = []
    for k, it in enumerate(items):
        if it[0] == "text":
            if k > last_call:
                continue
            st = {"kind": "note", "id": "%d:%d" % (it[1], it[2]), "idx": it[1], "b": it[2], "total": len(it[3])}
            if full:
                st["text"] = it[3][:TEXT_MAX]
            steps.append(st)
        elif it[0] == "step":
            steps.append(it[1])
        else:
            ev = dict(it[1])
            ev["kind"] = "event"
            steps.append(ev)
    j = len(steps)                      # 末尾的事件挪到最终回复之后
    while j > 0 and steps[j - 1]["kind"] == "event":
        j -= 1
    turn["trail"] = steps[j:]
    steps = steps[:j]

    for s in steps:                     # 没配上结果的调用
        if s["kind"] == "tool" and s.get("call") and s["res"] is None:
            if not ctx.get("has_results"):
                s["st"] = "unknown"
            elif turn["aborted"]:
                s["st"] = "interrupted"
            elif ctx.get("last_turn"):
                s["st"] = "pending"
            else:
                s["st"] = "unknown"
    turn["steps"] = steps

    if finals:
        texts = [f[3] for f in finals]
        frec = records[finals[0][1]]
        fm = frec.get("message") if isinstance(frec.get("message"), dict) else {}
        final = {"items": [{"idx": f[1], "b": f[2], "len": len(f[3])} for f in finals],
                 "total": sum(len(x) for x in texts) + 2 * (len(texts) - 1),
                 "ts": ts_ms(frec.get("timestamp")),
                 "model": fm.get("model") if isinstance(fm.get("model"), str) else None}
        if full:
            final["text"] = "\n\n".join(texts)[:TEXT_MAX]
        else:
            final["snip"] = snippet("\n\n".join(x[:SNIPPET * 4] for x in texts[:3]))
        turn["final"] = final

    tools = [s for s in steps if s["kind"] == "tool"]
    files = set()
    for s in tools:
        if s["st"] not in ("error", "interrupted") and s["tk"] in ("edit", "write"):
            files.update(s.get("paths") or [])
    start = (turn["q"] or {}).get("ts") or turn["ts"]
    dur = (turn["endTs"] - start) if (start and turn["endTs"] and turn["endTs"] > start) else None
    turn["sum"] = {"steps": len(tools), "cmds": sum(1 for s in tools if s["tk"] == "shell"),
                   "files": len(files), "errors": sum(1 for s in tools if s["st"] in ("error", "interrupted")),
                   "changes": sum(1 for s in tools if s["tk"] in ("edit", "write")), "dur": dur}
    return turn


# ---------------- 索引 ----------------
def build_index(records, cache, style, cwd):
    """全会话的轮次索引: 每轮的记录范围 + 目录摘要 + 统计。"""
    cats = cache.classify_all(records)
    has_results = any(c[0] == TOOLRES for c in cats)
    bounds = []
    start = None
    for i, (cat, _) in enumerate(cats):
        if start is None:
            start = i
        elif cat in (Q, SLASH):
            bounds.append((start, i - 1))
            start = i
    if start is not None:
        bounds.append((start, len(records) - 1))

    ctx = {"style": style, "cwd": cwd, "has_results": has_results, "last_turn": False}
    entries = []
    for t, (a, b) in enumerate(bounds):
        ctx["last_turn"] = t == len(bounds) - 1
        tv = walk_turn(records, cats, a, b, t, ctx, full=False)
        sm = tv["sum"]
        slash = next((e["label"] for e in tv["lead"] if e.get("ek") == "slash"), None)
        entries.append({
            "t": t, "first": a, "last": b, "n": b - a + 1,
            "q": tv["q"]["snip"] if tv["q"] else None, "qts": tv["q"]["ts"] if tv["q"] else None,
            "r": tv["final"]["snip"] if tv["final"] else None, "fin": bool(tv["final"]),
            "steps": sm["steps"], "fail": sm["errors"] > 0, "ab": tv["aborted"], "chg": sm["changes"],
            "slash": slash, "inj": len(tv["inj"]), "ts": tv["ts"], "end": tv["endTs"], "model": tv["model"],
        })

    title = None
    for r in reversed(records):
        if isinstance(r, dict):
            if r.get("type") == "custom-title" and isinstance(r.get("customTitle"), str) and r["customTitle"].strip():
                title = r["customTitle"].strip()
                break
    if not title:
        for r in reversed(records):
            if isinstance(r, dict) and r.get("type") == "ai-title" and isinstance(r.get("aiTitle"), str) \
                    and r["aiTitle"].strip():
                title = r["aiTitle"].strip()
                break
    if not title:
        q0 = next((e["q"] for e in entries if e["q"]), None)
        title = one_line(q0, 80) if q0 else None
    sid = next((r.get("sessionId") for r in records[:200]
                if isinstance(r, dict) and isinstance(r.get("sessionId"), str)), None)
    times = [e["ts"] for e in entries if e["ts"]] + [e["end"] for e in entries if e["end"]]
    stats = {
        "questions": sum(1 for e in entries if e["q"] is not None),
        "tools": sum(e["steps"] for e in entries),
        "model": next((e["model"] for e in reversed(entries) if e["model"]), None),
        "first": min(times) if times else None, "last": max(times) if times else None,
        "records": len(records), "turns": len(entries), "title": title, "sessionId": sid,
    }
    return {"cats": cats, "bounds": bounds, "entries": entries, "stats": stats, "has_results": has_results}


def turn_of(index, idx):
    firsts = [a for a, _ in index["bounds"]]
    return max(0, bisect.bisect_right(firsts, idx) - 1)


def turn_views(records, index, ts_list, ctx, mode="all"):
    bounds = index["bounds"]
    ctx = dict(ctx, has_results=index["has_results"])
    out = []
    for t in ts_list:
        if not (0 <= t < len(bounds)):
            continue
        a, b = bounds[t]
        ctx["last_turn"] = t == len(bounds) - 1
        tv = walk_turn(records, index["cats"], a, b, t, ctx, full=True)
        for s in tv["steps"]:
            s.pop("paths", None)
            s.pop("cts", None)
        if mode == "chat":
            tv["steps"] = []
        elif mode == "changes":
            tv["steps"] = [s for s in tv["steps"] if s["kind"] == "tool" and s.get("tk") in ("edit", "write")]
        out.append(tv)
    return out


# ---------------- 查找 ----------------
def search_text(rec):
    if not isinstance(rec, dict):
        return ""
    if rec.get("type") == "summary":
        return rec.get("summary", "") or ""
    content = content_of(rec)
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
            parts.append(p.get("text") or "")
        elif t in ("thinking", "reasoning"):
            parts.append(p.get("thinking") or p.get("text") or "")
        elif t in ("tool_use", "server_tool_use"):
            inp = p.get("input")
            parts.append(inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False))
        elif t == "tool_result":
            parts.append(result_text(p))
    return "\n".join(x for x in parts if isinstance(x, str))


def search(records, query, limit=3000):
    ql = query.lower()
    hits, total = [], 0
    if not ql:
        return hits, total
    for i, r in enumerate(records):
        txt = search_text(r)
        if not txt:
            continue
        n = txt.lower().count(ql)
        if n:
            hits.append({"idx": i, "n": n})
            total += n
            if len(hits) >= limit:
                break
    return hits, total


# ---------------- 块读取(展开全文 / 编辑 / 详情) ----------------
def whole_text(rec):
    """整条记录的可读文本(注入内容展开时用); 没有消息正文的 system 记录取顶层 content。"""
    t = search_text(rec)
    if not t and isinstance(rec, dict) and isinstance(rec.get("content"), str):
        t = rec["content"]
    return t


def get_block(rec, b, field):
    """记录里某一块的完整文本。field: text / thinking / content(工具结果) / input(工具参数);
    b = -2 取整条记录的文本。"""
    if not isinstance(rec, dict):
        raise ValueError("记录无效")
    if b == -2:
        return whole_text(rec) or json.dumps(rec, ensure_ascii=False, indent=2)
    if b == -1:
        if rec.get("type") == "summary":
            return rec.get("summary", "") or ""
        c = content_of(rec)
        if isinstance(c, str):
            return c
        raise ValueError("该记录内容不是纯文本")
    c = content_of(rec)
    if not isinstance(c, list) or not (0 <= b < len(c)) or not isinstance(c[b], dict):
        raise ValueError("块下标越界")
    p = c[b]
    if field == "text":
        return p.get("text", "") or ""
    if field == "thinking":
        return p.get("thinking") or p.get("text") or ""
    if field == "cipher":
        return cipher_of(p)
    if field == "content":
        return result_text(p)
    if field == "input":
        inp = p.get("input", p.get("arguments"))
        return inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False, indent=2)
    raise ValueError("未知字段 %s" % field)


def patched_record(rec, b, field, value):
    """返回改好某一块文本的新记录(深拷贝), 原记录不动。"""
    import copy
    if not isinstance(value, str):
        raise ValueError("value 必须是字符串")
    nr = copy.deepcopy(rec)
    if b == -1:
        if nr.get("type") == "summary":
            nr["summary"] = value
            return nr
        m = nr.get("message")
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            m["content"] = value
            return nr
        raise ValueError("该记录内容不是纯文本")
    c = content_of(nr)
    if not isinstance(c, list) or not (0 <= b < len(c)) or not isinstance(c[b], dict):
        raise ValueError("块下标越界")
    p = c[b]
    if field == "text":
        p["text"] = value
    elif field == "thinking":
        if "thinking" in p or p.get("type") != "reasoning":
            p["thinking"] = value
        else:
            p["text"] = value
    elif field == "content":
        key = "content" if "content" in p or "output" not in p else "output"
        old = p.get(key)
        if isinstance(old, list):
            others = [x for x in old if not (isinstance(x, dict) and x.get("type") == "text")]
            p[key] = [{"type": "text", "text": value}] + others
        else:
            p[key] = value
    else:
        raise ValueError("该字段不可编辑: %s" % field)
    return nr


def step_detail(records, call, res, ctx):
    out = {}
    if call:
        rec = records[call["idx"]]
        c = content_of(rec)
        p = c[call["b"]] if isinstance(c, list) and 0 <= call["b"] < len(c) else {}
        name = p.get("name") or ""
        inp = p.get("input", p.get("arguments"))
        info = tool_info(name, inp, ctx, full=True)
        if info["tk"] in ("shell", "search"):
            cmd = _codex_exec_command(inp) if (name.lower() == "exec" and isinstance(inp, str)) else _shell_command(inp)
            if cmd and cmd.strip():
                out["cmd"] = cmd[:DETAIL_OUT_MAX]
        pretty = inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False, indent=2)
        if pretty and pretty not in ("null", "{}"):
            out["params"] = pretty[:PARAMS_MAX]
            out["ptotal"] = len(pretty)
        if info["tk"] in ("edit", "write"):
            lines, trunc = diff_lines(inp, ctx.get("cwd"))
            if lines:
                out["diff"] = lines
                out["dtrunc"] = trunc
    if res:
        rec = records[res["idx"]]
        items = dict(result_items(rec))
        p = items.get(res["b"])
        if p is not None:
            text = result_text(p)
            out["out"] = text[:DETAIL_OUT_MAX]
            out["ototal"] = len(text)
            out["olines"] = (text.count("\n") + 1) if text.strip() else 0
    return out


def image_bytes(rec, b):
    c = content_of(rec)
    if not isinstance(c, list) or not (0 <= b < len(c)):
        raise ValueError("块下标越界")
    p = c[b]
    src = p.get("source") if isinstance(p, dict) else None
    if not isinstance(src, dict) or src.get("type") != "base64" or not isinstance(src.get("data"), str):
        raise ValueError("不是内嵌图片")
    return base64.b64decode(src["data"]), src.get("media_type") or "image/png"


# ---------------- 导出为可读 Markdown ----------------
def _fmt_time(ms):
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def transcript_markdown(records, index, ctx, agent_name, include_steps=True, include_thinking=False):
    st = index["stats"]
    lines = ["# %s" % (st.get("title") or "会话"), ""]
    meta = []
    if ctx.get("cwd"):
        meta.append("工作目录 `%s`" % ctx["cwd"])
    if st.get("first"):
        meta.append("%s → %s" % (_fmt_time(st["first"]), _fmt_time(st["last"])))
    meta.append("%d 次提问 · %d 次工具调用" % (st["questions"], st["tools"]))
    lines += ["> " + " · ".join(meta), ""]
    views = turn_views(records, index, range(len(index["bounds"])), ctx)
    for tv in views:
        body = []
        for ev in tv["lead"]:
            body.append("*%s*" % ev["label"])
        if tv["q"]:
            q = tv["q"]
            full_q = q["text"] if q["total"] <= len(q["text"]) else \
                "\n\n".join(get_block(records[q["idx"]], x["b"], "text") for x in q["blocks"])
            body += ["### 你" + ((" · " + _fmt_time(q["ts"])) if q.get("ts") else ""), "", full_q.strip(), ""]
        steps = [s for s in tv["steps"] if s["kind"] == "tool"]
        if include_steps and steps:
            body.append("<details><summary>执行过程 · %d 步</summary>\n" % len(steps))
            for s in steps:
                title = ("%s(%s)" % (s.get("verb"), s.get("target")) if s.get("target") else s.get("verb", ""))
                mark = {"error": " ✗", "interrupted": " ⊘"}.get(s["st"], "")
                body.append("- `%s`%s" % (title.replace("`", "'"), mark))
            body += ["", "</details>", ""]
        if include_thinking:
            for s in tv["steps"]:
                if s["kind"] == "thinking" and not s.get("red"):
                    full_t = get_block(records[s["idx"]], s["b"], "thinking")
                    body += ["> 思考：" + full_t.strip().replace("\n", "\n> "), ""]
        if tv["final"]:
            f = tv["final"]
            text = f["text"] if f["total"] <= len(f["text"]) else \
                "\n\n".join(get_block(records[x["idx"]], x["b"], "text") for x in f["items"])
            body += ["### " + agent_name + ((" · " + _fmt_time(f["ts"])) if f.get("ts") else ""), "",
                     text.strip(), ""]
        for ev in tv["trail"]:
            body.append("*%s*" % ev["label"])
        if body:
            lines += body + ["---", ""]
    return "\n".join(lines)


# ---------------- 会话列表: 标题 / 摘要 / 时间(读文件头尾) ----------------
_META_CACHE = {}


def _read_head_tail(path, head_bytes=131072, tail_bytes=131072):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size <= head_bytes + tail_bytes:
            data = f.read()
            return data.decode("utf-8", "replace").splitlines(), []
        head = f.read(head_bytes).decode("utf-8", "replace").splitlines()[:-1]
        f.seek(size - tail_bytes)
        tail = f.read().decode("utf-8", "replace").splitlines()[1:]
    return head, tail


def scan_jsonl_meta(path):
    """读 jsonl 头尾若干 KB, 提取会话标题、最后一条消息、时间、工作目录(带缓存)。"""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_mtime, st.st_size)
    hit = _META_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    meta = {}
    try:
        head, tail = _read_head_tail(path)
    except OSError:
        return meta

    def parse(lines):
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln or ln[0] != "{":
                continue
            try:
                o = json.loads(ln)
            except ValueError:
                continue
            if isinstance(o, dict):
                out.append(o)
        return out

    hrecs, trecs = parse(head), parse(tail)
    allrecs = hrecs + trecs
    first_q = None
    for r in hrecs:
        if r.get("type") == "cc_meta":
            meta["origin"] = r.get("origin")
            meta["cwd"] = r.get("cwd") or meta.get("cwd")
            continue
        if not meta.get("sessionId") and isinstance(r.get("sessionId"), str):
            meta["sessionId"] = r["sessionId"]
        if not meta.get("cwd") and isinstance(r.get("cwd"), str):
            meta["cwd"] = r["cwd"]
        if not meta.get("created"):
            meta["created"] = ts_ms(r.get("timestamp"))
        if first_q is None:
            cat, info = classify(r)
            if cat == Q and info and info.get("text"):
                first_q = info["text"]
                if not meta.get("created") and info.get("cts"):
                    meta["created"] = info["cts"]
    custom = ai = last_text = last_ts = None
    for r in reversed(allrecs):
        t = r.get("type")
        if custom is None and t == "custom-title" and isinstance(r.get("customTitle"), str) and r["customTitle"].strip():
            custom = r["customTitle"].strip()
        if ai is None and t == "ai-title" and isinstance(r.get("aiTitle"), str) and r["aiTitle"].strip():
            ai = r["aiTitle"].strip()
        if last_ts is None:
            last_ts = ts_ms(r.get("timestamp"))
        if last_text is None:
            cat, info = classify(r)
            if cat == Q and info and info.get("text"):
                last_text = info["text"]
            elif cat == AGENT:
                tx = "\n".join(x for _, x in text_blocks(content_of(r)) if x.strip())
                if tx.strip():
                    last_text = tx
        if custom and ai and last_text and last_ts:
            break
    title = custom or ai or (one_line(first_q, 80) if first_q else None)
    meta["title"] = title
    if last_text:
        s = snippet(last_text, 120)
        if s and s != snippet(title or "", 120):
            meta["summary"] = s
    meta["last"] = last_ts or meta.get("created")
    _META_CACHE[path] = (key, meta)
    return meta
