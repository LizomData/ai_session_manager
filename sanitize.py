# -*- coding: utf-8 -*-
"""
「存到本地」的清理与去隐私化。

清理: 只留对话记录; 去掉 Claude 的结构化副本、加密思考的密文、图片; 可截断工具输出。
脱敏: 删账号与遥测字段; 直接删掉(不留占位符)路径里的用户名和托管账号、项目目录、邮箱、IP、MAC、主机名、
设备序列号、本机 git 姓名、密钥与令牌等; 可重新生成所有 ID(会话 / 消息 / 工具调用 ID、正文里的 UUID、
本机生成的随机名, 同一个 ID 处处换成同一个新值); 可把所有时间戳整体平移一个随机时长。
输入记录不改动, 返回新记录列表和统计报告。
"""
import functools
import json
import os
import random
import re
import socket
import string
import subprocess
import uuid as uuidlib
from collections import Counter
from datetime import datetime, timedelta, timezone

import claude_store
import reader_model as rm

OPTION_KEYS = ("dialog_only", "drop_copies", "drop_cipher", "drop_images", "max_tool_chars", "strip_account",
               "user_paths", "project_dir", "contacts", "secrets", "regen_ids", "shift_time", "hide_source")

# 只留对话时保留的非消息记录(会话标题)
TITLE_TYPES = {"custom-title", "ai-title", "summary"}
# 带账号 / 花费信息的记录, 删账号字段时整条去掉
ACCOUNT_TYPES = {"cost-state", "artifact-autoreact-ledger", "artifact-comment-monitor", "frame-link"}
ACCOUNT_ATTACHMENTS = {"session_context", "credential_org"}
# 记录顶层的账号 / 遥测字段
TOP_FIELDS = ("requestId", "promptId", "refusedMessageId", "_id", "slug", "permissionDecision", "requestedModel",
              "advisorModel", "perTurnEffort", "effort", "apiBlockIndex", "attributionMcpServer", "attributionMcpTool",
              "attributionSkill", "wireIngestContext", "entrypoint", "userType", "version", "gitBranch")
MSG_FIELDS = ("id", "usage", "container", "context_management")
CIPHER_KEYS = ("signature", "encrypted_content")
SKIP_SCAN_KEYS = {"signature", "data", "encrypted_content", "base64"}
# 取值整个就是 ID 的字段(另外键名以 uuid / uuids 结尾的也算, 工具调用块的 id 也算)
ID_KEYS = {"promptId", "messageId", "snapshotMessageId", "refusedMessageId", "requestId", "sessionId", "deliveryId",
           "delivery_id", "tool_use_id", "toolUseID", "toolUseId", "parentToolUseID", "agentId", "_id"}
TOOL_BLOCK_TYPES = {"tool_use", "server_tool_use", "mcp_tool_use"}
IMAGE_PLACEHOLDER = "[图片已移除]"
# 用户目录下常见的文件夹: 删过用户名的路径(C:\Users\Documents\…)再处理时不会被当成用户名
HOME_FOLDERS = {"public", "default", "all users", "default user", "shared", "documents", "desktop", "downloads",
                "pictures", "music", "videos", "appdata", "onedrive", "favorites", "links", "searches", "contacts",
                "saved games", "3d objects"}
FILE_LIKE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
EMAIL_KEEP = {"noreply@anthropic.com", "git@github.com", "git@gitlab.com"}

# 用户名连同后面的分隔符一起删: C:\Users\Alice\a.txt → C:\Users\a.txt
WIN_USER = re.compile(r"(?i)([A-Za-z]:[\\/]{1,2}Users[\\/]{1,2})([^\\/\s\"'<>|:*?`,;；，。、：（）]+)([\\/]{1,2})?")
# 盘符后的 /Users/ 归 Windows 规则管: 否则 C:/Users/Alice/x.py 删掉用户名后, x.py 会被当成用户名再删一次
UNIX_USER = re.compile(r"(?<![A-Za-z]:)(/(?:home|Users)/)([A-Za-z0-9._-]+)(/)?")
ENC_USER = re.compile(r"(?i)((?:^|[^A-Za-z0-9])[A-Za-z]--Users-)([A-Za-z0-9._]+)(-)?")
EMAIL_TEXT = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
EMAIL = re.compile(r"(<\s*)?(" + EMAIL_TEXT + r")(\s*>)?")      # 带尖括号的连括号一起删
IPV4 = re.compile(r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1?\d?\d))((?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?![\d.])"
                  r"(:\d{1,5}(?!\d))?")                          # 连同端口一起删
ADB_SERIAL = re.compile(r"(\badb(?:\.exe)?)(\s+(?:-s|--serial)\s+)([^\s\"'`]+)")   # 整个 -s 参数删掉
ENV_SERIAL = re.compile(r"(\bANDROID_SERIAL=)([^\s\"'`]+)")
SECRET_TOKENS = [
    ("PRIVATE KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")),
    ("sk-", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}")),
    ("gh", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("github_pat_", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("AKIA", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AIza", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("xox", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("eyJ", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]
BEARER = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._\-~+/=]{16,})")
ASSIGN_WORDS = ("password", "passwd", "pwd", "secret", "token", "api_key", "apikey", "api-key", "access_key",
                "accesskey", "secret_key")
ASSIGN = re.compile(r"(?i)(\b(?:password|passwd|pwd|client_secret|secret|access_token|refresh_token|token|"
                    r"api[_-]?key|access[_-]?key|secret[_-]?key)\b[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_\-+/=.~]{8,})")
COOKIE = re.compile(r"(?i)(\b(?:set-)?cookie[\"']?[ \t]*:[ \t]*[\"']?)([A-Za-z0-9_.%-]+=[^\r\n\"'；，。]*)")  # 请求头: 值到行尾
COOKIE_ARG = re.compile(r"(?i)(--cookie(?:=|[ \t]+)([\"'])?)((?(2)[^\"'\r\n]+|[^\s\"']+))")  # curl --cookie
URL_SECRET = re.compile(r"(?i)([?&](?:x-amz-signature|x-amz-credential|x-amz-security-token|signature|sig|sign|token|"
                        r"access_token|auth|auth_?key|ticket|code|key|secret|session|sessionid|sid|skey)=)"
                        r"([A-Za-z0-9%._~+/=*!|-]{8,})")
SESSION_WORDS = ("sessid", "sessionid", "session_key", "sid", "csrf", "xsrf", "auth_token", "authtoken", "ticket", "skey")
# 网页会话令牌: Cookie 里常见的小写键名区分大小写, 免得把 JSON 里的 sessionId(会话 ID, 由「重新生成 ID」处理)也删掉
SESSION_ASSIGN = re.compile(r"(\b(?:(?i:phpsessid|jsessionid|aspsessionid\w*|csrf_?token|xsrf_?token|auth_?token|p_skey)|"
                            r"sessionid|SESSIONID|session_key|sid|SID|ticket|skey)\b[\"']?[ \t]*[:=][ \t]*[\"']?)"
                            r"([A-Za-z0-9%_\-+/=.~]{8,})")
ACCOUNT_WORDS = ("uid", "openid", "open_id", "unionid", "union_id", "userid", "user_id", "accountid", "account_id",
                 "playerid", "player_id", "roleid", "role_id", "steamid", "steam_id", "uin")
ACCOUNT_ASSIGN = re.compile(r"(?i)(\b(?:uid|openid|open_id|unionid|union_id|user_?id|account_?id|player_?id|role_?id|"
                            r"steam_?id|uin)\b[\"']?[ \t]*[:=][ \t]*[\"']?)([A-Za-z0-9_\-]{4,})")
FINGERPRINT = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){19}(?:(?:[0-9A-Fa-f]{2}:){12})?[0-9A-Fa-f]{2}"
                         r"(?![0-9A-Fa-f:])")                     # 证书 SHA1 / SHA256 指纹

# 个人与设备标识: Windows 默认主机名(后缀要带数字, 免得误伤 desktop-capture 这类词)、MAC、IPv6、账户 SID、卷序列号、
# Wi-Fi 名、Apple 开发者身份
HOST_DEFAULT = re.compile(r"(?i)(?<![A-Za-z0-9_-])(?:DESKTOP-([A-Z0-9]{7})|LAPTOP-([A-Z0-9]{8})|WIN-([A-Z0-9]{11}))"
                          r"(?![A-Za-z0-9_-])")
MAC = re.compile(r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
IPV6 = re.compile(r"(?<![0-9A-Za-z:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:%[0-9A-Za-z]+)?(?![0-9A-Za-z:.])")
WIN_SID = re.compile(r"(?<![A-Za-z0-9-])S-1-5-21(?:-\d+){3,4}(?![\d-])")
VOLUME_SERIAL = re.compile(r"((?:Volume Serial Number is|卷的序列号是)[ \t]*)[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}")
SSID = re.compile(r"((?<![A-Za-z])SSID[\"']?(?:[ \t]+\d+)?[ \t]*[:=][ \t]*)([^\r\n,;]*[^\s,;])")
APPLE_IDENTITY = re.compile(r"((?:Apple Development|Apple Distribution|iPhone Developer|iPhone Distribution|"
                            r"Mac Developer|Developer ID Application|Developer ID Installer):[ \t]*)"
                            r"([^\r\n\"\\()]{1,80}?[ \t]*\([A-Z0-9]{10}\))")
TEAM_ID = re.compile(r"((?i:\b(?:DEVELOPMENT_TEAM|TeamIdentifier|team[_ -]?id))[\"']?[ \t]*[:=][ \t]*[\"']?)([A-Z0-9]{10})"
                     r"(?![A-Za-z0-9])")
TEAM_PLIST = re.compile(r"(<key>(?:TeamIdentifier|com\.apple\.developer\.team-identifier)</key>\s*(?:<array>\s*)?"
                        r"<string>)[A-Z0-9]{10}(?=</string>)")
# 本机身份: 名字太泛的不删, 免得把正文里的普通词删掉
GENERIC_NAMES = {"admin", "administrator", "user", "root", "test", "dev", "developer", "me", "unknown", "default",
                 "owner", "git", "github", "name", "your name", "yourname", "pc", "home", "localhost", "local"}
COMMON_DIRS = {"src", "test", "tests", "docs", "doc", "lib", "libs", "app", "apps", "web", "api", "bin", "build", "dist",
               "assets", "scripts", "tools", "core", "common", "utils", "main", "master", "data", "public", "static",
               "config", "example", "examples", "packages", "modules", "plugins", "server", "client", "include"}
# 代码托管账号: 仓库远程地址里的 owner、push 输出里推送到的账号、gh 登录的账号
REMOTE_OWNER = re.compile(r"(?i)(?:github|gitee|gitlab)\.com[/:]([A-Za-z0-9_.-]+)/")
PUSH_TO = re.compile(r"(?i)\bTo (?:https?://|ssh://)?(?:[\w.-]+@)?(?:github|gitee|gitlab)\.com[/:]([A-Za-z0-9_.-]+)/")
GH_LOGIN = re.compile(r"(?i)\bLogged in to [\w.-]+ (?:account|as) ([A-Za-z0-9_-]+)")
OWNER_STOP = {"orgs", "users", "repos", "settings", "login", "about", "features", "topics", "marketplace", "apps",
              "sponsors", "search", "explore", "notifications", "new", "organizations", "site"}

UUID_TEXT = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_FULL = re.compile(UUID_TEXT)
ID_PREFIX = r"(?:srvtoolu|toolu|tooluse|msg|req|call|fc|rs|resp|ws)_(?:vrtx_|bdrk_)?"
# 正文里的 ID: UUID, 以及 toolu_ / msg_ / req_ / call_ 这类服务端 ID(主体至少 16 位且带数字, 不会误伤 msg_buffer 之类)
TEXT_ID = re.compile(r"(?<![A-Za-z0-9])(?:(%s)|(%s)([A-Za-z0-9]{16,}))(?![A-Za-z0-9])" % (UUID_TEXT, ID_PREFIX))
# ID 字段的取值: 再放宽到短前缀 ID 和其余带数字的字母数字段, 用于 Cursor 的复合 ID(call-<uuid>-7\nfc_xxx-…-aws_ue1_0)
FIELD_ID = re.compile(r"(?<![A-Za-z0-9])(?:(%s)|(%s)(?![0-9a-fA-F]{8}-)([A-Za-z0-9]{4,})|([A-Za-z0-9]{5,}))"
                      r"(?![A-Za-z0-9])" % (UUID_TEXT, ID_PREFIX))
AGENT_ID_TEXT = re.compile(r"\bagentId[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9]{6,})")
# 本机生成的随机名: 后台任务 ID、临时输出文件名、shell 快照名、artifact 短 ID。先收集原值, 正文里整词出现就换
LOCAL_NAME_TEXT = [
    ("with ID", re.compile(r"\bwith ID:?\s*([A-Za-z0-9]{6,})\b")),
    ("<task-id>", re.compile(r"<task-id>([A-Za-z0-9]{6,})</task-id>")),
    (".output", re.compile(r"\btasks[\\/]+([A-Za-z0-9]{6,})\.output\b")),
    ("tool-results", re.compile(r"\btool-results[\\/]+(?:([a-z0-9]{6,12})|[\w.-]*?-(\d{13}))\.(?:txt|json)\b")),
    ("shell-snapshots", re.compile(r"\bshell-snapshots[\\/]+snapshot-[a-z]+-(\d{10,13})-([a-z0-9]{4,})\.sh\b")),
    ("claude.ai/", re.compile(r"claude\.ai/(?:code/)?artifacts?/([A-Za-z0-9]{10,})(?![A-Za-z0-9-])")),
]
TASK_ID_KEYS = {"task_id", "taskId", "bash_id", "shell_id"}
PLAN_FILE = re.compile(r"\bplans[\\/]+([a-z]+-[a-z]+-[a-z]+)(?:-agent-[A-Za-z0-9]+)?\.md\b")   # 计划文件名就是会话 slug
SLUG_FULL = re.compile(r"[a-z]{2,}-[a-z]{2,}-[a-z]{2,}")
WF_ID = re.compile(r"(?<![A-Za-z0-9_])wf_([a-z0-9]{4,}(?:-[a-z0-9]{2,})*)(?![A-Za-z0-9_-])")
ALNUM_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{6,}(?![A-Za-z0-9])")

# 时间平移: ISO 时间串、以时间命名的字段里的秒 / 毫秒时间戳、Cursor 提问里的 <timestamp>
ISO_TIME = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?")
CURSOR_TAG = re.compile(r"<timestamp>[^<]{6,80}</timestamp>")
TIME_KEY = re.compile(r"(?:[Tt]ime(?:stamp)?|[Dd]ate|_at|[a-z]At|[Cc]reated|[Uu]pdated|[Mm]odified)$|^ts$")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def normalize(opts):
    out = {}
    for k in OPTION_KEYS:
        v = (opts or {}).get(k)
        if k == "max_tool_chars":
            try:
                out[k] = max(0, int(v or 0))
            except (TypeError, ValueError):
                out[k] = 0
        else:
            out[k] = bool(v)
    return out


def active(opts):
    return any(v for k, v in normalize(opts).items())


def _label(r):
    t = r.get("type")
    if t == "attachment":
        a = r.get("attachment") if isinstance(r.get("attachment"), dict) else {}
        return "附件:%s" % a.get("type")
    if t == "system":
        return "system:%s" % r.get("subtype")
    if t:
        return str(t)
    return "role:%s" % (r.get("role") or "?")


def _is_account_record(r):
    if r.get("type") in ACCOUNT_TYPES:
        return True
    a = r.get("attachment")
    return r.get("type") == "attachment" and isinstance(a, dict) and a.get("type") in ACCOUNT_ATTACHMENTS


def _project_patterns(cwd):
    """工作目录的几种写法: E:\\a\\b、E:/a/b、/e/a/b、JSON 转义后的双反斜杠, 以及 Claude 项目目录名编码。
    连同后面的分隔符一起匹配, 删掉后路径变成相对路径。"""
    if not cwd:
        return []
    out = []
    norm = cwd.replace("/", "\\").rstrip("\\")
    m = re.match(r"^([A-Za-z]):\\(.+)$", norm)
    if m:
        parts = [re.escape(x) for x in m.group(2).split("\\") if x]
        if parts:
            sep = r"[\\/]{1,2}"
            head = r"(?:%s:%s|/%s/)" % (m.group(1), sep, m.group(1).lower())
            out.append(re.compile(r"(?i)" + head + sep.join(parts) + r"(?![A-Za-z0-9_.\-])(?:[\\/]{1,2})?"))
    elif cwd.startswith("/") and cwd.strip("/"):
        out.append(re.compile(re.escape(cwd.rstrip("/")) + r"(?![A-Za-z0-9_.\-])/?"))
    enc = claude_store.encode_cwd(cwd.rstrip("\\/"))
    if len(enc.strip("-")) > 3:
        out.append(re.compile(r"(?i)(?<![A-Za-z0-9-])" + re.escape(enc) + r"(?![A-Za-z0-9])-?"))
    return out


def _plausible_serial(v):
    return len(v) >= 4 and not any(c in v for c in "$%<>{}()[]*\\/")


def _has_any(words):
    """关键词预筛: 字符串(不分大小写)里含任一关键词才跑正则。返回小写词元组, 由 Redactor.sub 每个字符串只转一次小写来查。"""
    return tuple(w.lower() for w in words)


def _scan_key(k):
    """字典的键要不要处理: type、tool_use_id 这类标识符形式的键里不会有敏感内容, 跳过(省掉大量调用)。"""
    return isinstance(k, str) and len(k) > 3 and not k.isidentifier()


def _ok_name(n):
    if not n or n.lower() in GENERIC_NAMES:
        return False
    return len(n) >= 2 if any(ord(c) > 127 for c in n) else len(n) >= 3


def _ok_owner(o):
    return len(o) >= 2 and o.lower() not in OWNER_STOP and o.lower() not in GENERIC_NAMES


def _git(args, cwd=None):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=cwd, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


@functools.lru_cache(maxsize=32)
def local_identity(cwd=""):
    """本机的 git 姓名、主机名, 以及会话工作目录所在仓库的远程地址里的托管账号。只在本机读取, 用来从会话里删掉。"""
    where = cwd if cwd and os.path.isdir(cwd) else None
    names = {_git(["config", "--get", "user.name"], where).strip(), _git(["config", "--global", "--get", "user.name"]).strip()}
    hosts = {}
    for h in (socket.gethostname(), os.environ.get("COMPUTERNAME", ""), os.environ.get("HOSTNAME", "")):
        if len(h) >= 4 and h.lower() not in GENERIC_NAMES:
            hosts.setdefault(h.lower(), h)
    owners = set(REMOTE_OWNER.findall(_git(["remote", "-v"], where))) if where else set()
    return {"names": sorted(n for n in names if _ok_name(n)), "hosts": sorted(hosts.values()),
            "owners": sorted(o for o in owners if _ok_owner(o))}


class _Found:
    """先从整份会话里收集出来、之后要全文处理的值。"""

    def __init__(self):
        self.serials, self.owners, self.agent_ids, self.tokens, self.slugs = set(), set(), set(), set(), set()


def collect(records, opts):
    """收集设备序列号(`adb -s`、`ANDROID_SERIAL=`)、托管账号(push 输出、gh 登录)、agentId、本机随机名和会话 slug。"""
    f = _Found()
    serial, owner, ids = opts["contacts"], opts["user_paths"], opts["regen_ids"]

    def text(s, key):
        if serial and ("adb" in s or "ANDROID_SERIAL" in s):
            f.serials.update(m.group(3) for m in ADB_SERIAL.finditer(s) if _plausible_serial(m.group(3)))
            f.serials.update(m.group(2) for m in ENV_SERIAL.finditer(s) if _plausible_serial(m.group(2)))
        if owner:
            if "To " in s and ".com" in s:
                f.owners.update(PUSH_TO.findall(s))
            if "Logged in to" in s:
                f.owners.update(GH_LOGIN.findall(s))
        if ids:
            if key == "agentId":
                f.agent_ids.add(s)
            elif "agentId" in s:
                f.agent_ids.update(AGENT_ID_TEXT.findall(s))
            if key in TASK_ID_KEYS:
                f.tokens.add(s)
            elif key == "slug" and SLUG_FULL.fullmatch(s):
                f.slugs.add(s)
            for pre, rx in LOCAL_NAME_TEXT:
                if pre in s:
                    for m in rx.finditer(s):
                        f.tokens.update(g for g in m.groups() if g)
            if "plans" in s:
                f.slugs.update(PLAN_FILE.findall(s))

    def walk(o, key=None):
        if isinstance(o, str):
            text(o, key)
        elif isinstance(o, list):
            for x in o:
                walk(x, key)
        elif isinstance(o, dict):
            for k, v in o.items():
                if k not in SKIP_SCAN_KEYS:
                    walk(v, k)
    for r in records:
        walk(r)
    # 随机名要同时带字母和数字(13 位毫秒时间戳除外), 免得把普通单词、数字当成 ID
    f.tokens = {t for t in f.tokens if re.fullmatch(r"[A-Za-z0-9]{6,}", t) and not UUID_FULL.fullmatch(t)
                and ((t.isdigit() and len(t) == 13) or (re.search(r"\d", t) and re.search(r"[A-Za-z]", t)))}
    f.owners = {o for o in f.owners if _ok_owner(o)}
    return f


def _owner_rules(owners):
    """托管账号: 地址里的 owner 段连同后面的 / 一起删(github.com/alice/repo → github.com/repo);
    alice/repo 简写、@alice、alice.github.io 也删(名字太泛的不删简写, 免得误伤 src/ 之类的目录)。"""
    alts = "|".join(re.escape(o) for o in sorted(owners, key=len, reverse=True))
    hosted = re.compile(r"(?i)((?:github|gitee|gitlab)\.com(?::|/(?:repos/|users/|orgs/)?)|githubusercontent\.com/|"
                        r"\bLogged in to [\w.-]+ (?:account|as) )(?:%s)(?:/|(?![A-Za-z0-9_.-]))" % alts)
    distinct = [o for o in owners if len(o) >= 4 and o.lower() not in COMMON_DIRS]
    if not distinct:
        return hosted, None
    alts = "|".join(re.escape(o) for o in sorted(distinct, key=len, reverse=True))
    short = re.compile(r"(?i)(?<![A-Za-z0-9_.\-/\\@])(?:%s)/(?=[A-Za-z0-9_.-])|(?<![A-Za-z0-9_.-])@(?:%s)(?![A-Za-z0-9_-])"
                       r"|(?<![A-Za-z0-9_.-])(?:%s)\.(?=github\.io)" % (alts, alts, alts))
    return hosted, short


def _word_rx(words, ascii_bounds):
    """整词匹配一组值(不区分大小写); 非 ASCII 的值(中文姓名)直接按子串匹配。"""
    words = sorted(words, key=len, reverse=True)
    plain = [re.escape(w) for w in words if any(ord(c) > 127 for c in w)]
    ascii_ = [re.escape(w) for w in words if not any(ord(c) > 127 for c in w)]
    parts = plain + (["(?<!%s)(?:%s)(?!%s)" % (ascii_bounds, "|".join(ascii_), ascii_bounds)] if ascii_ else [])
    return re.compile("(?i)" + "|".join(parts))


class Redactor:
    """按选项逐个字符串删除敏感内容(不留占位符), 并按类别计数。"""

    def __init__(self, opts, cwd, found=None):
        self.count = Counter()
        self.users = Counter()
        self.identity = {}      # 认出的本机身份(姓名 / 主机名 / 托管账号), 报告里给用户看
        self.rules = []
        ident = local_identity(cwd or "") if (opts["contacts"] or opts["user_paths"]) else None
        if opts["secrets"]:
            for key, rx in SECRET_TOKENS:
                self._add("密钥/令牌", rx, lambda m: "", (lambda k: lambda s: k in s)(key))
            self._add("密钥/令牌", BEARER, lambda m: m.group(1), lambda s: "earer" in s or "EARER" in s)
            self._add("密钥/令牌", ASSIGN, self._assign, _has_any(ASSIGN_WORDS))
            self._add("Cookie", COOKIE, self._cookie, lambda s: "ookie" in s or "OOKIE" in s)
            self._add("Cookie", COOKIE_ARG, self._cookie, lambda s: "--cookie" in s)
            self._add("URL 签名/令牌参数", URL_SECRET, lambda m: m.group(1), lambda s: "=" in s and ("?" in s or "&" in s))
            self._add("会话令牌", SESSION_ASSIGN, self._assign, _has_any(SESSION_WORDS))
            self._add("账号参数", ACCOUNT_ASSIGN, self._account, _has_any(ACCOUNT_WORDS))
            self._add("证书指纹", FINGERPRINT, lambda m: "", lambda s: s.count(":") >= 19)
        if opts["project_dir"]:
            for rx in _project_patterns(cwd):
                self._add("项目目录", rx, lambda m: "", None)
        if opts["user_paths"]:
            self._add("用户名", WIN_USER, self._user, lambda s: "sers" in s)
            self._add("用户名", UNIX_USER, self._user, lambda s: "/home/" in s or "/Users/" in s)
            self._add("用户名", ENC_USER, self._user, lambda s: "--Users-" in s or "--users-" in s)
            owners = set(ident["owners"]) | (found.owners if found else set())
            if owners:
                self.identity["代码托管账号"] = sorted(owners, key=str.lower)
                hosted, short = _owner_rules(owners)
                self._add("代码托管账号", hosted, lambda m: m.group(1), lambda s: ".com" in s or "Logged in" in s)
                if short is not None:
                    self._add("代码托管账号", short, lambda m: "", None)
        if opts["contacts"]:
            self._add("邮箱", EMAIL, self._email, lambda s: "@" in s)
            self._add("IP", IPV4, self._ip, lambda s: "." in s)
            self._add("IPv6", IPV6, self._ipv6, lambda s: "::" in s or s.count(":") >= 7)
            self._add("MAC", MAC, self._mac, lambda s: s.count(":") >= 5 or s.count("-") >= 5)
            self._add("设备序列号", ADB_SERIAL, lambda m: m.group(1) if _plausible_serial(m.group(3)) else m.group(0),
                      lambda s: "adb" in s)
            self._add("设备序列号", ENV_SERIAL, lambda m: m.group(1) if _plausible_serial(m.group(2)) else m.group(0),
                      lambda s: "ANDROID_SERIAL" in s)
            if ident["names"]:
                self.identity["姓名"] = ident["names"]
                self._add("姓名", _word_rx(ident["names"], "[A-Za-z0-9]"), lambda m: "", None)
            if ident["hosts"]:
                self.identity["主机名"] = ident["hosts"]
                self._add("主机名", _word_rx(ident["hosts"], "[A-Za-z0-9_-]"), lambda m: "", None)
            self._add("主机名", HOST_DEFAULT, self._host_default,
                      lambda s: "ESKTOP-" in s or "esktop-" in s or "APTOP-" in s or "aptop-" in s or "WIN-" in s)
            self._add("Windows SID", WIN_SID, lambda m: "", lambda s: "S-1-5-21" in s)
            self._add("卷序列号", VOLUME_SERIAL, lambda m: m.group(1), lambda s: "Serial Number" in s or "序列号是" in s)
            self._add("Wi-Fi 名", SSID, lambda m: m.group(1), lambda s: "SSID" in s)
            self._add("Apple 开发者身份", APPLE_IDENTITY, lambda m: m.group(1),
                      lambda s: "Develop" in s or "Distribution" in s)
            self._add("Apple 开发者身份", TEAM_ID, self._team, lambda s: "eam" in s or "EAM" in s)
            self._add("Apple 开发者身份", TEAM_PLIST, lambda m: m.group(1), lambda s: "<key>" in s)

    def add_serials(self, serials):
        """其余地方出现的同一序列号(比如 `adb devices` 的输出)也删掉。"""
        if not serials:
            return
        alts = "|".join(re.escape(s) for s in sorted(serials, key=len, reverse=True))
        rx = re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % alts)
        vals = list(serials)
        self._add("设备序列号", rx, lambda m: "", lambda s: any(v in s for v in vals))

    def _add(self, name, rx, repl, pre):
        def fn(s):
            def rp(m):
                out = repl(m)
                if out != m.group(0):
                    self.count[name] += 1
                return out
            return rx.sub(rp, s)
        self.rules.append((pre, fn))

    def _user(self, m):
        name = m.group(2)
        # 常见文件夹、点开头的目录、路径末尾带扩展名的文件名都不是用户名
        if name.lower() in HOME_FOLDERS or name.startswith(".") or (not m.group(3) and FILE_LIKE.search(name)):
            return m.group(0)
        self.users[name] += 1
        return m.group(1)   # 用户名和它后面的分隔符一起去掉

    @staticmethod
    def _email(m):
        left, addr, right = m.group(1), m.group(2), m.group(3)
        if addr.lower() in EMAIL_KEEP:
            return m.group(0)
        if left and not right:     # 只有半边括号时保留那半边
            return left
        if right and not left:
            return right
        return ""

    @staticmethod
    def _ip(m):
        first = int(m.group(1))
        ip = m.group(1) + m.group(2)
        if ip == "0.0.0.0" or first in (0, 127, 255) or first < 10:
            return m.group(0)   # 回环 / 广播, 以及 1.2.3.4 这类多半是版本号的写法
        return ""

    @staticmethod
    def _assign(m):
        v = m.group(2)
        tail = m.string[m.end():m.end() + 1]
        if tail == "(" or not re.search(r"\d", v) or not re.search(r"[A-Za-z]", v):
            return m.group(0)   # 函数调用、纯单词之类不是口令
        return m.group(1)   # 只删值, 保留键名

    @staticmethod
    def _account(m):
        # uid / openid 这类账号参数: 值里要有数字, 函数调用不算
        if m.string[m.end():m.end() + 1] == "(" or not re.search(r"\d", m.group(2)):
            return m.group(0)
        return m.group(1)

    @staticmethod
    def _cookie(m):
        # Cookie 值是 name=value 形式; curl --cookie 后面跟的是 cookie 文件名时不删
        return m.group(1) if "=" in m.group(m.lastindex) else m.group(0)

    @staticmethod
    def _mac(m):
        v = re.sub(r"[:-]", "", m.group(0)).lower()
        return m.group(0) if v in ("000000000000", "ffffffffffff") else ""

    @staticmethod
    def _ipv6(m):
        s = m.group(0).split("%")[0]
        groups = [g for g in s.split(":") if g]
        if not re.search(r"\d", s):
            return m.group(0)       # abc::def 这类代码里的名字
        if "::" in s:
            ok = len(groups) >= 2 and any(len(g) >= 3 or re.search(r"[a-fA-F]", g) for g in groups)
        else:
            ok = s.count(":") == 7
        return "" if ok else m.group(0)     # ::1、12:34:56 这类保留

    @staticmethod
    def _host_default(m):
        suffix = m.group(1) or m.group(2) or m.group(3)
        return "" if re.search(r"\d", suffix) else m.group(0)

    @staticmethod
    def _team(m):
        return m.group(1) if re.search(r"\d", m.group(2)) else m.group(0)

    @property
    def enabled(self):
        return bool(self.rules)

    def sub(self, s):
        if not s:
            return s
        low = None
        for pre, fn in self.rules:
            if type(pre) is tuple:          # 关键词预筛
                if low is None:
                    low = s.lower()
                if not any(w in low for w in pre):
                    continue
            elif pre is not None and not pre(s):
                continue
            out = fn(s)
            if out != s:
                s, low = out, None
        return s

    def walk(self, o, key=None):
        if isinstance(o, str):
            return o if key in SKIP_SCAN_KEYS else self.sub(o)
        if isinstance(o, list):
            return [self.walk(x, key) for x in o]
        if isinstance(o, dict):
            return {(self.sub(k) if _scan_key(k) else k): self.walk(v, k) for k, v in o.items()}
        return o


class _Stats:
    def __init__(self):
        self.dropped = Counter()
        self.fields = 0
        self.copies = 0
        self.cipher = [0, 0]
        self.images = [0, 0]
        self.truncated = 0


def _cut(s, n, st):
    if n and isinstance(s, str) and len(s) > n:
        st.truncated += 1
        return s[:n] + "\n…[已截断 %d 字符]" % (len(s) - n)
    return s


def _block(p, opts, st):
    if not isinstance(p, dict):
        return p
    t = p.get("type")
    if opts["drop_images"] and t == "image":
        st.images[0] += 1
        st.images[1] += len(json.dumps(p.get("source", ""), ensure_ascii=False))
        return {"type": "text", "text": IMAGE_PLACEHOLDER}
    if opts["drop_cipher"] and t in ("thinking", "redacted_thinking", "reasoning"):
        q = dict(p)
        n = 0
        for k in CIPHER_KEYS + (("data",) if t == "redacted_thinking" else ()):
            if k in q:
                v = q.pop(k)
                n += len(v) if isinstance(v, str) else 1
        if n:
            st.cipher[0] += 1
            st.cipher[1] += n
            if not (q.get("thinking") or q.get("text")):
                return {"type": "thinking", "thinking": "", "redacted": True}
        return q
    if t == "tool_result":
        q = dict(p)
        c = q.get("content")
        if isinstance(c, list):
            c = [_block(x, opts, st) for x in c]
            if opts["max_tool_chars"]:
                c = [dict(x, text=_cut(x.get("text"), opts["max_tool_chars"], st))
                     if isinstance(x, dict) and x.get("type") == "text" else x for x in c]
        elif isinstance(c, str):
            c = _cut(c, opts["max_tool_chars"], st)
        q["content"] = c
        return q
    return p


def _record(r, opts, st):
    r = dict(r)
    if opts["strip_account"]:
        for k in TOP_FIELDS:
            if k in r:
                del r[k]
                st.fields += 1
    if opts["drop_copies"]:
        for k in ("toolUseResult", "wireToolInputs"):
            if k in r:
                del r[k]
                st.copies += 1
    tur = r.get("toolUseResult")
    if isinstance(tur, dict) and (opts["drop_images"] or opts["max_tool_chars"]):
        tur = dict(tur)
        f = tur.get("file")
        if opts["drop_images"] and isinstance(f, dict) and "base64" in f:
            f = dict(f)
            st.images[0] += 1
            st.images[1] += len(f.pop("base64") or "")
            tur["file"] = f
        if opts["max_tool_chars"]:
            for k, v in list(tur.items()):
                tur[k] = _cut(v, opts["max_tool_chars"], st)
        r["toolUseResult"] = tur
    m = r.get("message")
    if isinstance(m, dict):
        m = dict(m)
        if opts["strip_account"]:
            for k in MSG_FIELDS:
                if k in m:
                    del m[k]
                    st.fields += 1
        if isinstance(m.get("content"), list):
            m["content"] = [_block(p, opts, st) for p in m["content"]]
        r["message"] = m
    return r


_rng = random.SystemRandom()
HEX = "0123456789abcdef"
B62 = string.ascii_letters + string.digits


def _rand_like(s):
    """和 s 等长的随机串: 纯数字仍是纯数字, 十六进制仍是十六进制, 全小写 / 全大写的仍是小写 / 大写, 其余用字母数字。"""
    if s.isdigit():
        pool = string.digits
    elif all(c in HEX for c in s):
        pool = HEX
    elif all(c in HEX.upper() for c in s):
        pool = HEX.upper()
    elif s.islower():
        pool = string.ascii_lowercase + string.digits
    elif s.isupper():
        pool = string.ascii_uppercase + string.digits
    else:
        pool = B62
    return "".join(_rng.choice(pool) for _ in s)


def _rand_runs(s):
    """每段字母数字各换成等长随机串, 分隔符不变: ab12cd34-x9z → 7fq2m0kd-4bp。"""
    return re.sub(r"[A-Za-z0-9]+", lambda m: _rand_like(m.group(0)), s)


def _rand_slug(s):
    """会话 slug(humble-swimming-otter): 每个词换成等长的随机小写字母。"""
    return "-".join("".join(_rng.choice(string.ascii_lowercase) for _ in w) for w in s.split("-"))


class IdMapper:
    """把 ID 换成新的随机值。同一个原值处处换成同一个新值, 所以 parentUuid 链、工具调用和结果的配对、
    正文里提到的会话 ID、文件名都还对得上; 新值和原值没有关系。"""

    def __init__(self, found=None):
        self.map = {}
        self.used = set()
        self.count = 0
        found = found or _Found()
        # agentId、后台任务 ID、临时文件名这类没有固定格式的值: 先登记新值, 正文里整词出现就换
        self.tokens = {v for v in found.agent_ids | found.tokens if len(v) >= 6 and not UUID_FULL.fullmatch(v)
                       and re.fullmatch(r"[A-Za-z0-9]+", v)}
        for v in self.tokens:
            self.map[v] = _rand_like(v)
        self.slugs = sorted(found.slugs, key=len, reverse=True)
        for v in self.slugs:
            self.map[v] = _rand_slug(v)
        self.slug_rx = (re.compile(r"(?<![A-Za-z0-9_-])(?:%s)(?![A-Za-z0-9_])" % "|".join(map(re.escape, self.slugs)))
                        if self.slugs else None)

    def _get(self, old, make):
        self.count += 1
        self.used.add(old)
        new = self.map.get(old)
        if new is None:
            new = self.map[old] = make(old)
        return new

    def _uuid(self, v):
        new = self._get(v.lower(), lambda _: str(uuidlib.uuid4()))
        upper = any(c in "ABCDEF" for c in v) and not any(c in "abcdef" for c in v)
        return new.upper() if upper else new

    def _sub_text(self, m):
        if m.group(1):
            return self._uuid(m.group(1))
        prefix, body = m.group(2), m.group(3)
        if not any(c.isdigit() for c in body):
            return m.group(0)
        return self._get(prefix + body, lambda _: prefix + _rand_like(body))

    def _sub_field(self, m):
        if m.group(1):
            return self._uuid(m.group(1))
        if m.group(3):
            prefix, body = m.group(2), m.group(3)
            return self._get(prefix + body, lambda _: prefix + _rand_like(body))
        run = m.group(4)
        return self._get(run, _rand_like) if any(c.isdigit() for c in run) else run

    def _sub_wf(self, m):
        body = m.group(1)
        if not any(c.isdigit() for c in body):
            return m.group(0)       # wf_name 这类代码里的变量名
        return self._get("wf_" + body, lambda _: "wf_" + _rand_runs(body))

    def _sub_token(self, m):
        t = m.group(0)
        return self._get(t, _rand_like) if t in self.tokens else t

    def text(self, s):
        if len(s) >= 19 and ("-" in s or "_" in s):
            s = TEXT_ID.sub(self._sub_text, s)
        if "wf_" in s:
            s = WF_ID.sub(self._sub_wf, s)
        if self.tokens and len(s) >= 6 and not self.tokens.isdisjoint(ALNUM_TOKEN.findall(s)):
            s = ALNUM_TOKEN.sub(self._sub_token, s)
        if self.slug_rx is not None and any(v in s for v in self.slugs):
            s = self.slug_rx.sub(lambda m: self._get(m.group(0), _rand_slug), s)
        return s

    def field(self, s):
        return FIELD_ID.sub(self._sub_field, s)

    def walk(self, o, key=None, in_tool=False):
        if isinstance(o, str):
            if key in SKIP_SCAN_KEYS:
                return o
            if key is not None and (key in ID_KEYS or key.lower().endswith(("uuid", "uuids")) or (in_tool and key == "id")):
                return self.field(o)
            return self.text(o)
        if isinstance(o, list):
            return [self.walk(x, key) for x in o]
        if isinstance(o, dict):
            tool = isinstance(o.get("type"), str) and o["type"] in TOOL_BLOCK_TYPES
            return {(self.text(k) if _scan_key(k) else k): self.walk(v, k, tool) for k, v in o.items()}
        return o


class TimeShifter:
    """所有时间统一往前挪一个随机时长(30~400 天, 再加一天内的随机秒数): 先后顺序和间隔不变, 看不出真实日期和几点。
    挪的是记录里的 ISO 时间、以时间命名的字段里的秒 / 毫秒时间戳、Cursor 提问里的 <timestamp>(顺带改成 UTC,
    不暴露时区); 工具输出正文里的日期没法可靠识别, 不动。"""

    def __init__(self):
        self.delta = -timedelta(days=_rng.randint(30, 400), seconds=_rng.randint(0, 86399))
        self.count = 0

    def iso(self, s):
        m = ISO_TIME.fullmatch(s)
        if not m:
            return s
        y, mo, d, hh, mi, ss, frac, tz = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss), int((frac or ".")[1:7].ljust(6, "0")))
            dt += self.delta
        except (ValueError, OverflowError):
            return s
        self.count += 1
        out = dt.strftime("%Y-%m-%dT%H:%M:%S")
        if frac:
            out += "." + ("%06d" % dt.microsecond).ljust(len(frac) - 1, "0")[:len(frac) - 1]
        return out + (tz or "")

    def _tag(self, m):
        ms = rm.cursor_ts(m.group(0))
        if ms is None:
            return m.group(0)
        dt = datetime.fromtimestamp(ms / 1000, timezone.utc) + self.delta
        self.count += 1
        return "<timestamp>%s, %s %d, %d, %d:%02d %s (UTC+0)</timestamp>" % (
            WEEKDAYS[dt.weekday()], MONTHS[dt.month - 1], dt.day, dt.year, dt.hour % 12 or 12, dt.minute,
            "AM" if dt.hour < 12 else "PM")

    def walk(self, o, key=None):
        if isinstance(o, str):
            if key in SKIP_SCAN_KEYS:
                return o
            if len(o) >= 19 and o[4:5] == "-" and o[10:11] == "T":
                shifted = self.iso(o)
                if shifted is not o:
                    return shifted
            return CURSOR_TAG.sub(self._tag, o) if "<timestamp>" in o else o
        if isinstance(o, bool):
            return o
        if isinstance(o, (int, float)):
            if key and TIME_KEY.search(key):
                sec = self.delta.total_seconds()
                if 1e12 <= o < 1e13:
                    self.count += 1
                    return type(o)(o + sec * 1000)
                if 1e9 <= o < 1e10:
                    self.count += 1
                    return type(o)(o + sec)
            return o
        if isinstance(o, list):
            return [self.walk(x, key) for x in o]
        if isinstance(o, dict):
            return {k: self.walk(v, k) for k, v in o.items()}
        return o


def _size(records):
    return sum(len(json.dumps(r, ensure_ascii=False, separators=(",", ":"))) + 1 for r in records)


def run(records, opts, cwd=""):
    """→ (新记录列表, 报告 dict)"""
    opts = normalize(opts)
    skipped = []
    # 源文件已经删过用户名(本地保存时记在来源信息里): 再删会把下一层目录当成用户名误删, 这次跳过
    prior = next((r.get("sanitized") for r in records
                  if isinstance(r, dict) and r.get("type") == "cc_meta" and isinstance(r.get("sanitized"), dict)), None)
    if prior and prior.get("user_paths") and opts["user_paths"]:
        opts = dict(opts, user_paths=False)
        skipped.append("user_paths")
    st = _Stats()
    src = [r for r in records if isinstance(r, dict) and r.get("type") != "cc_meta"]
    cats = [rm.classify(r)[0] for r in src] if opts["dialog_only"] else None
    keep_cats = (rm.Q, rm.SLASH, rm.TOOLRES, rm.AGENT, rm.EVENT)

    kept, dropped_parent = [], {}
    for i, r in enumerate(src):
        drop = False
        if opts["dialog_only"] and r.get("type") not in TITLE_TYPES and cats[i] not in keep_cats:
            drop = True
        elif opts["strip_account"] and _is_account_record(r):
            drop = True
        if drop:
            st.dropped[_label(r)] += 1
            if isinstance(r.get("uuid"), str):
                dropped_parent[r["uuid"]] = r.get("parentUuid")
            continue
        kept.append(r)

    found = collect(src, opts) if (opts["contacts"] or opts["user_paths"] or opts["regen_ids"]) else _Found()
    red = Redactor(opts, cwd, found)
    if opts["contacts"]:
        red.add_serials(found.serials)
    ids = IdMapper(found) if opts["regen_ids"] else None
    shifter = TimeShifter() if opts["shift_time"] else None
    out = []
    for r in kept:
        nr = _record(r, opts, st)
        for k in ("parentUuid", "logicalParentUuid"):   # 被删记录的子记录接到最近的保留祖先
            if k in nr:
                p, hops = nr[k], 0
                while isinstance(p, str) and p in dropped_parent and hops < 100000:
                    p, hops = dropped_parent[p], hops + 1
                nr[k] = p
        if red.enabled:
            nr = red.walk(nr)
        if ids is not None:
            nr = ids.walk(nr)
        if shifter is not None:
            nr = shifter.walk(nr)
        out.append(nr)

    report = {
        "options": opts,
        "records": [len(src), len(out)],
        "dropped": dict(st.dropped.most_common()),
        "removed": {"fields": st.fields, "copies": st.copies, "cipher": st.cipher, "images": st.images,
                    "truncated": st.truncated},
        "deleted": dict(red.count.most_common()),
        "skipped": skipped,
        "users": [u for u, _ in red.users.most_common(20)],
        "identity": {k: v for k, v in red.identity.items() if red.count.get(k)},
        "size": [_size(src), _size(out)],
        "cwd": redact_text(cwd, opts, cwd),
        "ids": [len(ids.used), ids.count] if ids is not None else None,
        "timeShift": shifter.count if shifter is not None else None,
        "newSessionId": next((r.get("sessionId") for r in out if isinstance(r.get("sessionId"), str)), None)
        if ids is not None else None,
    }
    return out, report


def redact_text(text, opts, cwd=""):
    """单独脱敏一段文字(来源信息里的路径等)。"""
    red = Redactor(normalize(opts), cwd)
    return red.sub(text) if red.enabled and text else text


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    recs = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    _, rep = run(recs, {k: True for k in OPTION_KEYS if k != "max_tool_chars"}, cwd=os.getcwd())
    print(json.dumps({k: v for k, v in rep.items() if k != "options"}, ensure_ascii=False, indent=1))
