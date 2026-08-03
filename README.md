# Claude Code 会话记录编辑器

一个零依赖的本地工具,用来查看和编辑 Claude Code 的会话记录文件(`~/.claude/projects/**/*.jsonl`)。
后端起本地 HTTP 服务(仅监听 `127.0.0.1`),前端是内嵌网页;数据全部驻留后端内存、按页拉取,几十 MB 的大会话也能秒开。

## 运行

```bash
# 不带参数:自动扫描 ~/.claude/projects 下所有会话,在网页里选择
python session_editor.py

# 直接打开指定文件
python session_editor.py --file /path/to/session.jsonl

# 其它选项
python session_editor.py --port 8790 --no-browser --dir <额外扫描目录>
```

> Windows 上 Python 路径含空格时:`& "C:\Program Files\Python311\python.exe" session_editor.py`

启动后浏览器打开 `http://127.0.0.1:8790/`。仅依赖 Python 标准库(建议 3.11+)。

## 功能

| 类别 | 说明 |
|------|------|
| 查看/浏览 | 把 JSONL 解析成可读对话:角色 / 文本 / thinking / 工具调用 / 工具结果,分块显示 |
| 后端分页 | 数据留在后端,浏览器每页只取约 50 条(几十 MB 秒开);可调 25/50/100/200 每页 |
| 全文搜索 | 跨全文搜关键词,列出命中片段,点击跨页跳转并高亮定位 |
| 角色/类型过滤 | 只看 user / assistant / tool_use / tool_result / thinking / system |
| 修改文本 | 就地编辑任意 text / thinking / 字符串 / 工具结果文本(防抖提交) |
| 删除消息 | 删除并自动把子节点 `parentUuid` 重链到父节点,保持链表完整 |
| 插入/移动/复制 | 上下插入新消息、上移下移、复制某条 |
| 截断 | 从某条起删除其后所有消息 |
| 撤销/重做 | 编辑、删除、截断、插入、移动均可逐步回退(`Ctrl+Z` / `Ctrl+Y`) |
| Fork 会话 | 从任意消息分叉出一个新 `sessionId` 的独立会话文件(写入同目录,可直接 `--resume`),原会话不动 |
| 备份/回滚 | 每次保存自动生成 `.bak.<时间戳>`;可列出所有备份并一键回滚 |
| 导出 | 导出为 Markdown 或纯文本 |

## 安全

- 保存前先把原文件复制成 `<文件名>.bak.<时间戳>`,再用临时文件 + 原子替换写入。
- 保留每条记录的全部原始字段,只改动被编辑的部分。
- 仅监听本地回环地址,不对外暴露。

## 会话文件格式速记

每行一个 JSON 对象,通过 `uuid` / `parentUuid` 组成链表;`sessionId` 标识会话,文件名通常即 `<sessionId>.jsonl`。
删除节点需把其子节点重链到父节点,否则 `--resume` 会断链——本工具已自动处理。

## 快捷键

- `Ctrl+S` 保存 · `Ctrl+Z` 撤销 · `Ctrl+Y` / `Ctrl+Shift+Z` 重做
