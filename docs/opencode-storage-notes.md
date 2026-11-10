# OpenCode 存储核查（2026-10-17）

只读核查本机 `~/.local/share/opencode/opencode.db` 的表结构与会话内容；没有修改原生数据或转储消息正文。桌面版版本为 2.0.32。

## 位置

OpenCode（桌面版与 CLI）把会话存进**单一 SQLite 库**：

- 默认：`~/.local/share/opencode/opencode.db`（WAL 模式，含 `-wal` / `-shm`）
- XDG：`$XDG_DATA_HOME/opencode/opencode.db`；可用 `AGENTHUB_OPENCODE_HOME` 直接指向目录
- 桌面版 Electron 缓存（`~/Library/Application Support/ai.opencode.desktop/`）**不含会话**，真正数据在上面那个库里

## 表结构

库中同时存在新旧两套表：

| 代 | 会话表 | 消息表 | 说明 |
|---|---|---|---|
| v2（当前） | `session_v2` | `session_message` | 桌面版 2.x 使用，内容块内联在消息 `data` 里 |
| v1（旧） | `session` | `message` + `part` | 更早版本使用，正文拆在 `part` 表 |

本机的 v2 是 v1 的**超集**（v2 包含全部 v1 会话并多出当前会话），故读取器以 `session_v2` / `session_message` 为准；库中没有 `session_v2` 时视为无可读会话。

### `session_v2`（会话元数据）

关键列：`id`、`parent_id`、`title`、`directory`（工作目录）、`model`（JSON 文本 `{"id":…,"providerID":…}`）、`time_created` / `time_updated`（epoch 毫秒）、`time_archived`、`revert`、`permission`。

- `parent_id` 非空表示这是 `task` 工具派生的**子代理会话**，据此建任务树（`origin=subagent`）。
- `time_archived` 只表示归档，原记录仍在库中，读取器不据此隐藏（与 DSH 归档同一处理）。

### `session_message`（消息）

列：`id`（`msg_…`）、`session_id`、`type`、`seq`、`time_created`、`data`（JSON）。`type ∈ {user, assistant, system, idle}`，按 `seq` 排序即原生顺序。

- `user`：正文在 `data.text`（`data.metadata.displayText` 是同一内容的展示兜底）。
- `system`：正文在 `data.text`（如「可用工具已变更」通知），读取器映射为「协作事件」卡片。
- `assistant`：正文在 `data.content[]`，内容块内联：
  - `{type:"reasoning", text}` → 思考
  - `{type:"text", text}` → 正文
  - `{type:"tool", name|tool, id|callID, state}` → 工具调用；`state = {status, input, content|error}`，结果取 `content[]` 的文本块，其次 `error` / `output`

**流式碎片**：正在生成或中断的会话里，`reasoning` / `text` 会以逐 token 的小块相邻写入（一个消息可达数万块）。读取器把**相邻同类**块先合并成一段再输出，因此同一段思考不会炸成成千上万条记录。

## 时间与可见性

- `session_message.data.time.created` 与 `session_v2.time_*` 都是 epoch 毫秒。
- 会话删除由原生外键级联（行被直接移除），没有墓碑；库中仍存在的记录即视为可见。
