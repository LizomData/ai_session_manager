# AI Session Manager

一个零依赖的本地工具,用来**查看、编辑、互导** Claude Code / Cursor / Codex 的对话会话。
后端起本地 HTTP 服务(仅监听 `127.0.0.1`),前端是 `web/` 下的静态页面;数据全部驻留后端内存、按页拉取,几十 MB / 上万条的大会话也能秒开。仅依赖 Python 标准库(建议 3.11+)。

界面版式参考 [cc-switch](https://github.com/farion1231/cc-switch) 的会话管理:左侧会话列表,右侧按「提问 → 执行过程 → 最终回复」整理的阅读视图和对话目录。

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

也可以直接用链接打开某个会话和视图:`http://127.0.0.1:8790/?file=<会话文件路径>&view=records`(`view` 可选 `all` / `chat` / `changes` / `records`)。

## 支持的来源

侧栏可按来源筛选,也可用文字搜索(标题 / 目录 / 最后一条消息 / 会话 ID)。子代理的 transcript(`agent-*.jsonl`、`subagents/` 目录)不单独列出,需要时点侧栏顶部的「按路径打开」输入文件路径。

| 来源 | 位置 | 读 | 写 |
| ---- | ---- | -- | -- |
| Claude Code | `~/.claude/projects/**/*.jsonl` | ✅ | ✅ 可编辑保存 |
| Cursor agent-transcript | `~/.cursor/projects/**/*.jsonl` | ✅ | ✅ 可编辑保存 |
| Cursor 会话 store.db | `~/.cursor/chats/**/store.db` | ✅(比 jsonl 更全) | ❌ 只读 |
| Codex rollout | `~/.codex/sessions/**/rollout-*.jsonl` | ✅ | ❌ 只读 |
| 本地保存 | `<工具目录>/saved_sessions/` | ✅ | ✅ |

## 功能

### 会话列表(侧栏)

- 按**项目**(工作目录)或按**时间**(今天 / 昨天 / 本周 / 更早)分组,项目组可折叠,记住展开状态。
- 每行显示标题、最后一条消息和相对时间;标题依次取 `custom-title` → `ai-title` → 第一条提问 → 目录名。
- 悬停行尾 `⋯`:复制会话 ID、源文件路径、恢复命令(`claude --resume …` / `codex resume …`)。

### 阅读视图

每一轮按「提问 → 执行过程 → 最终回复」组织:

- **提问**靠右气泡;**Agent 输出**靠左,带头像、时间和模型名。
- **执行过程**默认折叠成一行摘要:`执行过程 · 14 步 · 9 个命令 · 改了 2 个文件 · 1 个失败 · ⏱ 2m14s`。失败步骤在折叠时也常显;没有最终回复或被中断的轮自动展开。
- 步骤标题按来源习惯显示:Claude / Cursor 为 `Bash(git status)`、`Read(src/a.ts:1-40)`,Codex 为 `Ran git status`;右侧显示状态、exit code、耗时、输出行数或 `+3 −1`。展开后可看完整命令、参数、改动 diff 和输出(默认 12 行,可显示全部)。
- 过程中的说明、思考(显示字数,点开看全文)、斜杠命令、上下文压缩、中断等事件都有对应样式;注入的上下文(系统提示、环境信息、命令输出)默认隐藏,可在「显示选项」里打开。
- 文本按 Markdown 渲染(代码块带语言和复制按钮、表格、任务列表、链接;本地路径链接点击即复制)。超过 3000 字的内容折叠到 1500 字。
- 右侧**对话目录**列出每轮的提问和回复摘要,点击跳转,滚动时跟随高亮;有失败步骤的轮带红点。
- **会话内查找**(`Ctrl+F`):跨全文统计命中数,`Enter` / `Shift+Enter` 逐个跳转,自动展开命中所在的步骤并高亮。
- 视图切换:**全部** / **对话**(只看提问和最终回复)/ **改动**(只看改文件的步骤)/ **记录**(逐条原始记录)。
- 页面数据按轮分页(每页约 12 轮),大块内容只下发预览,展开时再取全文。

### 编辑

| 操作 | 说明 |
| ---- | ---- |
| 就地编辑 | 提问、回复、说明、思考、工具输出都可点「编辑」修改原文,`Ctrl+Enter` 应用,`Esc` 取消 |
| 记录操作 | 每条内容的 `⋯` 菜单:上 / 下方插入消息、复制此条、上移 / 下移、从这里 Fork、在记录视图查看、截断其后、删除 |
| 删除 | 自动把子节点的 `parentUuid` 重链到父节点,保持链表完整 |
| 撤销 / 重做 | 所有修改都可逐步回退(`Ctrl+Z` / `Ctrl+Y`) |
| 保存 | 写回原文件,写前自动生成 `.bak.<时间戳>`;「备份与回滚」可回到任一备份 |
| Fork 会话 | 从任意记录分叉出新 `sessionId` 的独立会话文件;Fork 前会**扫描分叉点之后的文件改动**并提示(Fork 只分叉对话,不还原工作区文件) |
| 存到本地 | 把当前会话(含改动)另存为 jsonl 到 `saved_sessions/`;首行写入**来源信息**(origin / 源文件 / cwd / 时间),重开后可再编辑或导出 |
| 导出到… | 弹窗选目标,把当前会话导出到其它 CLI(见下);导出前自动剔除斜杠命令 / 本地命令 caveat 等噪声 |
| 导出 MD / TXT | Markdown 按轮整理(提问、执行过程摘要、最终回复);TXT 为逐条记录的完整文本 |

界面支持浅色 / 深色(跟随系统,也可在侧栏顶部切换),窄屏时侧栏改为抽屉。

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
- `Ctrl+F` 会话内查找(`Enter` 下一个 · `Shift+Enter` 上一个 · `Esc` 关闭)
- `j` / `k` 跳到下一轮 / 上一轮

## 代码结构

- `session_editor.py` — HTTP 服务:会话发现、打开、编辑 / 撤销、保存、搜索、导出等接口
- `reader_model.py` — 阅读视图模型:记录分类、按轮组织、对话目录索引、步骤标题与详情、可读 Markdown 导出、会话列表的标题 / 摘要
- `web/` — 前端页面(`index.html` / `app.css` / `app.js`,无构建步骤)
- `cursor_store.py` — 读 Cursor store.db(内容寻址 blob + protobuf 解码)
- `codex_store.py` — 读/写 Codex rollout + 登记 threads 索引
- `claude_store.py` — 写可 resume 的 Claude 会话
