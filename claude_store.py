# -*- coding: utf-8 -*-
"""
Claude Code 会话写入模块。

Claude Code 把会话存为 ~/.claude/projects/<cwd编码>/<sessionId>.jsonl,
每行一条记录, 通过 uuid/parentUuid 组成链表。cwd 编码规则: 非字母数字字符 → '-'
(盘符大小写为历史遗留, 故优先复用已存在的同名目录, 忽略大小写)。

Claude jsonl 无内容寻址、无签名, 可以合成写入。为最大兼容(能 resume 且继续对话
不因缺 thinking 签名报错), 这里把每条消息折叠为纯文本 message。
"""
import json
import os
import re
import time
import uuid as uuidlib


def projects_root():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def encode_cwd(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def project_dir(cwd):
    root = projects_root()
    enc = encode_cwd(cwd)
    if os.path.isdir(root):
        for d in os.listdir(root):
            if d.lower() == enc.lower():
                return os.path.join(root, d)
    return os.path.join(root, enc)


def _iso_z(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % (int(t * 1000) % 1000)


def _role_of(rec):
    return rec.get("role") or (rec.get("message") or {}).get("role") or rec.get("type")


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


def write_claude_session(records, cwd, title="", version="2.1.177", git_branch="HEAD"):
    """把归一化会话写成可 resume 的 Claude Code 会话。返回 {path, session_id, count, project_dir}。"""
    if not cwd:
        raise ValueError("导出到 Claude 需要 cwd(决定项目目录, 否则无法 resume)")
    sid = str(uuidlib.uuid4())
    now = time.time()
    out = []
    parent = None
    for rec in records:
        if not isinstance(rec, dict) or rec.get("type") in ("turn_ended", "summary"):
            continue
        role = _role_of(rec)
        crole = "assistant" if role == "assistant" else "user"
        text = _text_of((rec.get("message") or {}).get("content"))
        if not text.strip():
            continue
        u = str(uuidlib.uuid4())
        r = {
            "parentUuid": parent, "isSidechain": False, "userType": "external",
            "type": crole, "message": {"role": crole, "content": [{"type": "text", "text": text}]},
            "uuid": u, "timestamp": _iso_z(now), "sessionId": sid, "cwd": cwd,
        }
        if version:
            r["version"] = version
        if git_branch:
            r["gitBranch"] = git_branch
        out.append(r)
        parent = u

    pdir = project_dir(cwd)
    os.makedirs(pdir, exist_ok=True)
    path = os.path.join(pdir, sid + ".jsonl")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False))
            f.write("\n")
    return {"path": os.path.abspath(path), "session_id": sid, "count": len(out),
            "project_dir": os.path.abspath(pdir)}
