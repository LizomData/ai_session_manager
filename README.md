# AI Session Manager

一个零依赖的本地工具,用来**查看、编辑、互导** Claude Code / Cursor / Codex 的对话会话。
后端起本地 HTTP 服务(仅监听 `127.0.0.1`),前端是内嵌网页;数据全部驻留后端内存、按页拉取,几十 MB / 上万条的大会话也能秒开。仅依赖 Python 标准库(建议 3.11+)。

## 运行

```bash
# 不带参数:自动扫描并列出所有来源的会话,自动打开浏览器
python session_editor.py

# 直接打开指定文件(jsonl / store.db / rollout 均可)
python session_editor.py --file /path/to/session.jsonl

# 其它选项
python session_editor.py --port 8790 --no-browser --dir <额外扫描目录>
```

> Windows 上 Python 路径含空格时:`& "C:\Program Files\Python311\python.exe" session_editor.py`

启动后浏览器打开 `http://127.0.0.1:8790/`。**执行目录**决定「存到本地」的保存位置(`saved_sessions/`)。

## 支持的来源

顶栏可按来源筛选,也可用文字筛选(按文件名 / 项目目录 / cwd)快速定位:

| 来源 | 位置 | 读 | 写 |
|------|------|----|----|
| Claude Code | `~/.claude/projects/**/*.jsonl` | ✅ | ✅ 可编辑保存 |
| Cursor agent-transcript | `~/.cursor/projects/**/*.jsonl` | ✅ | ✅ 可编辑保存 |
| Cursor 会话 store.db | `~/.cursor/chats/**/store.db` | ✅(比 jsonl 更全) | ❌ 只读 |
| Codex rollout | `~/.codex/sessions/**/rollout-*.jsonl` | ✅ | ❌ 只读 |
| 本地保存 | `<工具目录>/saved_sessions/` | ✅ | ✅ |

## 功能

| 类别 | 说明 |
|------|------|
| 查看/浏览 | 把会话解析成可读对话:角色 / 文本 / thinking / 工具调用 / 工具结果,分块显示;自动适配 Claude / Cursor / Codex 格式 |
| 后端分页 | 数据留在后端,浏览器每页只取约 50 条;可调 25/50/100/200 每页 |
| 筛选 | 顶栏按来源过滤 + 文字筛选(名称/目录/cwd);正文按角色/类型过滤(user/assistant/tool_use/…) |
| 全文搜索 | 跨全文搜关键词,列出命中片段,点击跨页跳转并高亮定位 |
| 修改文本 | 就地编辑任意 text / thinking / 字符串 / 工具结果文本(防抖提交) |
| 删除消息 | 删除并自动把子节点 `parentUuid` 重链到父节点,保持链表完整 |
| 插入/移动/复制 | 上下插入新消息、上移下移、复制某条 |
| 截断 | 从某条起删除其后所有消息 |
| 撤销/重做 | 编辑、删除、截断、插入、移动均可逐步回退(`Ctrl+Z` / `Ctrl+Y`) |
| Fork 会话 | 从任意消息分叉出一个新 `sessionId` 的独立 Claude 会话文件;Fork 前会**扫描分叉点之后的文件改动**并提示(Fork 只分叉对话,不还原工作区文件) |
| 备份/回滚 | 每次保存自动生成 `.bak.<时间戳>`;可列出所有备份并一键回滚 |
| 存到本地 | 把当前会话(含改动)另存为 jsonl 到 `saved_sessions/`;首行写入**来源信息**(origin / 源文件 / cwd / 时间),重开后可再编辑或导出 |
| 导出到… | 弹窗选目标,把当前会话导出到其它 CLI(见下);导出前自动剔除斜杠命令 / 本地命令 caveat 等噪声 |
| 导出 MD/TXT | 导出为 Markdown 或纯文本 |

## 导出到其它 CLI

「导出到…」弹窗选目标(cwd 自动预填):

- **Claude Code** — 写成可 `--resume` 的会话到 `~/.claude/projects/<cwd编码>/<sessionId>.jsonl`(cwd 编码 `[^A-Za-z0-9]→'-'`,优先复用已存在的同名目录),建立 `parentUuid` 链。
- **Codex** — 写 rollout 到 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`,并**登记到 `state_5.sqlite` 的 `threads` 表**(这是 `codex resume` 真正读取的索引),同时安全追加 `history.jsonl`。逐轮结构复刻真实 rollout(`user_message` / `agent_message` 事件 + `response_item`)。

两者都把 thinking / 工具调用折叠为纯文本,保证对方能加载、能继续对话。

> 提示:导出超长会话到 Codex 后 resume 可能因上下文过长报错——工具结果等大块内容会占用大量 token。可考虑精简正文 / 截断 / 只导出最近若干轮。

## 只读来源说明

- **Cursor store.db**:内容受服务端签发的 token/签名完整性保护(线程节点每个气泡带 ~100 字符 token,reasoning 块带 provider signature)。实测:原样复制真实 `store.db` 可正常渲染,但改动一句文本(并沿内容寻址 DAG 级联重哈希)Cursor 即渲染为空。故只读;要编辑请先「存到本地」另存为 jsonl。
- **Codex rollout**:直接改写会破坏其事件结构,故只读;要编辑同样先「存到本地」。

## 安全

- 保存前先把原文件复制成 `<文件名>.bak.<时间戳>`,再用临时文件 + 原子替换写入。
- 保留每条记录的全部原始字段,只改动被编辑的部分。
- 仅监听本地回环地址,不对外暴露。
- `saved_sessions/`、`*.jsonl` 等会话数据已在 `.gitignore` 中排除,不会进仓库。

## 会话格式速记

- **Claude jsonl**:每行一个 JSON,通过 `uuid` / `parentUuid` 组成链表;`sessionId` 标识会话,文件名通常即 `<sessionId>.jsonl`。删除节点会把子节点重链到父节点,否则 `--resume` 断链——已自动处理。
- **Cursor store.db**:SQLite 单表 `blobs(id,data)`,`id = sha256(data)` 内容寻址;JSON 消息 blob + protobuf 节点组成 Merkle DAG,引用消息最多的节点即会话头。
- **Codex rollout**:首行 `session_meta`,其后 `response_item`(模型上下文)与 `event_msg`(UI transcript,`user_message` / `agent_message`);索引在 `~/.codex/state_5.sqlite` 的 `threads` 表。

## 快捷键

- `Ctrl+S` 保存 · `Ctrl+Z` 撤销 · `Ctrl+Y` / `Ctrl+Shift+Z` 重做

## 代码结构

- `session_editor.py` — HTTP 服务 + 内嵌网页 + 所有编辑/搜索/导出接口
- `cursor_store.py` — 读 Cursor store.db(内容寻址 blob + protobuf 解码)
- `codex_store.py` — 读/写 Codex rollout + 登记 threads 索引
- `claude_store.py` — 写可 resume 的 Claude 会话
