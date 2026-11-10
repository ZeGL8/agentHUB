# agentHUB

**本地多 Agent 工具的统一会话索引与无缝交接器。**

解决的核心痛点：在 Claude Code / Codex / DSH / Hermes / OpenCode / WorkBuddy 等多个 agent 工具之间转移工作上下文——先扫出所有工具的本地对话记录，再把任一会话导出成标准 Markdown 交接文档，投喂给任意其他 agent 接续工作。

## 支持的工具

默认从当前用户主目录下读取，路径可用环境变量覆盖（macOS / Linux / Windows 通用）：

| 工具 | 默认位置 | 覆盖变量 | 格式 |
|---|---|---|---|
| claude | `~/.claude/projects/` | `AGENTHUB_CLAUDE_HOME` 或 `CLAUDE_CONFIG_DIR` | JSONL（主会话；subagents 子会话暂不单独索引） |
| codex | `~/.codex/sessions/` + `archived_sessions/`，以及 `thread_history_1.sqlite` | `AGENTHUB_CODEX_HOME` 或 `CODEX_HOME` | JSONL rollout；rollout 已被清理的线程从新版的 `thread_history_1.sqlite` 读取（元数据取自 `state_5.sqlite`），子代理线程挂在父线程下 |
| dsh | `~/.dsh/sessions/` | `AGENTHUB_DSH_HOME` | zstd 压缩 JSONL，桌面版 `session.v4` 优先，其次 `session.v3` 与 legacy（用 `zstandard` 库解压，无需系统命令） |
| hermes | `~/.hermes/state.db` | `AGENTHUB_HERMES_HOME` | SQLite (WAL) |
| opencode | `~/.local/share/opencode/opencode.db` | `AGENTHUB_OPENCODE_HOME` 或 `XDG_DATA_HOME` | SQLite (WAL)，桌面版 2.x 的 `session_v2` + `session_message` 表 |
| workbuddy | `~/.workbuddy/` | `AGENTHUB_WORKBUDDY_HOME` | SQLite 元数据 + 本地 traces 重建对话 |

某个工具没装、目录不存在时直接跳过，不影响其他工具。任何会话文件或工具读取失败都会明确报错，本轮不替换原索引，避免把缺失或旧数据当成完整扫描结果。JSONL 末行没有换行符时视为对方工具正在写入，先忽略该行（中间行损坏仍报错）。扫描未完成时页面顶部显示「⚠ N 个扫描问题」（可点开看原因）；服务启动时扫描失败也会先用上次的索引启动，不会因为一个坏文件整个页面打不开。

各工具中已删除的会话不显示：Codex 以其 `state_5.sqlite` 线程表为准，WorkBuddy 以 `deleted_at` 为准，Claude Code 以 `.desktop-released.json` 标记为准。OpenCode / Hermes 的会话删除由原生外键级联（记录被直接移除），仍存在的记录即视为可见。

### DSH 的任务结构

DSH 与其他工具不同：主任务会把工作委派给**子代理**（独立会话文件），也可以从旧会话**派生**新会话。agentHUB 据此：

- 左侧列表默认「按任务树」：子任务折叠在父任务下（可切「全部平铺」）；子代理标题用委派描述（如「审计限流与超时分区」）
- 会话头部「↑ 父任务」「子任务 N ▾」互相跳转
- 全程视图里的协作事件（创建子任务、子任务回报、子任务结束、主任务来信、目标变化、工作流派发）以紫色卡片显示，可「打开子任务 →」
- 运行时上下文、工作区指令、收件箱拼接等注入内容不显示（与其他工具的系统注入同样处理）
- 同一轮次号被重新开始时按两次尝试分别取最终回复

### OpenCode 的任务结构

OpenCode（桌面版 / CLI）把会话存进单一 SQLite 库；子代理（`task` 工具派生的 `@explore` / `@general` 等）是带 `parent_id` 的独立会话。agentHUB 据此复用与 DSH 相同的任务树：

- 子会话折叠在父会话下，标题即原生标题（含 `(@… subagent)`）
- 会话头部「↑ 父任务」「子任务 N ▾」互相跳转
- 助手消息内的思考、正文、工具调用按原生顺序还原；流式写入时逐 token 的相邻思考/正文碎片会先合并成一段，避免同一段思考被拆成成千上万条
- `system` 消息（如工具变更通知）以「协作事件」卡片显示

agentHUB 自己的数据（索引、导出文件、模型配置）放在 `~/.agenthub/`，可用 `AGENTHUB_HOME` 改到别处。

## 安装与使用

在项目目录启动只需：

```bash
uv run server.py
```

浏览器打开 http://127.0.0.1:8322；停止时在该终端按 Ctrl+C。启动会自动同步依赖并重扫本地记录。换端口可用 `uv run server.py --port 9000`。跨机器复制项目时不要复制 `.venv/`，让 uv 在目标机器重新创建环境。

需要 Python ≥ 3.9。推荐用 [uv](https://docs.astral.sh/uv/)：

```bash
cd agentHUB          # 本仓库目录
uv sync

uv run server.py                      # Web UI → http://127.0.0.1:8322（--port 或 AGENTHUB_PORT 改端口）

uv run agenthub scan                  # 扫描全部工具，生成统一索引
uv run agenthub list --limit 20       # 查看索引（--tool / --cwd / --title 过滤）
uv run agenthub export --tool claude --id 1f1771b5          # 导出交接文档（ID 支持前缀匹配）
uv run agenthub export --tool dsh --id da2e7bef --full      # 含思考与工具调用的完整过程
uv run agenthub stats

uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python tests/test_core.py      # 原有回归检查
```

也可以全局安装成命令：`uv tool install .`（或 `pip install .`），之后直接运行 `agenthub serve`。

## 可视化页面（Web UI）

功能：
- **布局**：顶栏只放身份与全局操作；左栏「找会话」（搜索 → 会话 / 按项目目录 → 工具筛选 → 列表）；右栏「读会话」（视图标签 + 右侧小工具）。勾选框与「源码 / 复制」在悬停或多选时出现，触屏上常显
- **开始页**：各工具会话数（点击即筛选）+ 最近的会话 + 快捷键提示
- **搜索**：左栏按标题 / 工作目录 / 会话 ID 实时搜索（快捷键 `/`）；会话内搜索点右侧 ⌕ 打开，Esc 关闭
- **按项目目录**：按工作目录跨工具聚合（点目录即过滤该目录全部会话）
- **会话详情流**：用户 / 助手消息分色卡片，四种视图——「对话」用户消息 + 每轮 AI 最终回答；「只看用户」只列用户输入（配合全选 → 复制，可一次拿到全部提示词）；「只看 AI」只列每轮最终回答；「全程」含过程说明、思考（折叠）、工具调用与结果。导出交接文档跟随当前视图
- **格式化渲染**：Markdown、代码高亮、LaTeX 公式（`$…$`、`$$…$$`、`\(…\)`、`\[…\]`）；每条消息可切「源码」查看原文
- **单条复制**：每条消息、每个代码块都可单独复制原文；超长消息默认折叠
- **多选**：勾选消息 / 工具记录（Shift 连选、全选已加载、Esc 取消），可复制选中内容或「导出节选」为交接文档；选择跨视图与分页保留
- **导出弹层**：源码 / 渲染预览切换，复制全文或下载
- **会话多选（可跨工具）**：左侧列表勾选多个会话 → 「合并导出」为一份交接文档，或「AI 总结」

## AI 总结

右上角「⚙ 设置」填写任意 **OpenAI 兼容**接口（`/v1/models` + `/v1/chat/completions`）：接口地址、API Key（可留空）、模型（可获取列表并搜索），可一键测试连接。配置保存在 `~/.agenthub/config.json`（权限 600），Key 不会回传到页面。

三个入口，结果都在同一个弹层里流式显示，可复制 / 下载，不落盘：

| 入口 | 总结范围 |
|---|---|
| 会话头部「AI 总结」 | 当前整个会话（用户消息 + 每轮最终回复） |
| 消息多选栏「AI 总结」 | 选中的消息 / 思考 / 工具记录 |
| 左侧会话多选栏「AI 总结」 | 多个会话（可跨 Claude Code、Codex 等不同工具）合并归纳 |

模板：交接摘要（目标 / 已完成 / 结论 / 待办 / 文件）、要点提炼、关键决策与待办、自定义要求。内容超过模型上下文时自动分段提取要点再合并（上下文窗口可在设置中填写，未填写按 32K 保守估算；预算包含提示和输出）。模型输出里的 `<think>` 思考内容不显示也不复制。

> 注意：总结会把所选对话内容发送到你配置的模型接口，请只配置你信任的服务。
- **可分享链接**：地址栏 `#工具/会话ID` 记住当前会话，刷新后自动打开
- **一键导出**：弹层预览完整 Markdown 交接文档 → 📋 复制全文（直接粘贴给其他 agent）或 💾 下载；「对话」视图导出仅最终输出，「全程」视图导出含工具调用的完整过程
- **重新扫描**：右上角 ↻ 触发全量重扫（无需命令行；后台也会定时自动扫描）

后端用 Python 标准库 http.server（唯一第三方依赖是解压 DSH 用的 `zstandard`）；前端为 index.html + app.js + app.css，渲染库（marked / DOMPurify / highlight.js / KaTeX）固定版本放在 `web/vendor/`，离线可用、不请求外网。对话内容一律经 DOMPurify 清洗，原始 HTML 转义显示，图片不自动加载。服务只监听 127.0.0.1，并校验 Host/Origin（防 DNS rebinding 与跨站请求读取本地对话）。

## 目录结构

```
agentHUB/
├── server.py           # uv run server.py 启动入口
├── agenthub/            # Python 包
│   ├── model.py         # 统一数据模型（SessionRecord / Message）与 think 拆分
│   ├── readers/         # 各工具读取器（scan + read_messages）；__init__.normalize 统一后处理
│   ├── indexer.py       # 索引生成（index.json + index.md）
│   ├── exporter.py      # Markdown 交接文档渲染
│   ├── server.py        # Web UI 后端（http.server）
│   ├── llm.py           # AI 总结：OpenAI 兼容接口调用、分段提要、流式输出
│   ├── web/             # 前端单页 + vendor 渲染库
│   └── cli.py           # 命令行入口
└── tests/              # 读取、删除过滤、AI 流式与服务回归检查

~/.agenthub/             # 运行数据（AGENTHUB_HOME 可改）
├── index/               # scan 产物：index.json（机器）/ index.md（人读）
├── exports/             # export 产物：<tool>__<id>__<标题>.md
└── config.json          # 模型接口配置（权限 600）
```

## 交接文档长什么样

导出的 Markdown 包含：

1. **元数据头**：来源工具、会话 ID、原工作目录、起止时间、消息数、模型、原始文件路径
2. **对话记录**：👤 用户消息全文 → 🔧 工具调用全文（含参数）→ 📤 工具结果全文 → 🤖 助手回复全文
3. **接手提示**：引导接手方先确认工作目录现状再继续

使用方式：把导出的 `.md` 直接发给任何 agent，配一句"请接手这段工作"即可。

## 已知限制

- **WorkBuddy**：新版 trace 按原生 `trace.sessionId` 归属；旧版按工作目录（正反斜杠归一化）和创建时间切分。小型归属缓存与正文缓存均按文件修改时间、大小失效；原生工具输出保留全文。旧版 trace 没有 ID 时，工作目录/时间只提供推断归属；同目录并发会话可能存在歧义。缺少 trace 会明确报错，无法从云端恢复已截断或未落盘的用户消息。列表最近活动时间来自原生数据库，可能比最后一条消息晚几秒。
- **Hermes**：读取当前 `state.db` 的原生可见会话；不把历史备份库重复纳入。
- **OpenCode**：读取桌面版 2.x 的 `session_v2` / `session_message` 表；旧版仅 `session` / `message` / `part` 三表（无 `session_v2`）的库暂不读取。整库是单个 SQLite 文件，故每个会话显示的「占用」是库文件大小。原生存储细节（含流式碎片、父子会话、归档语义）见 docs/opencode-storage-notes.md。
- **Claude Code**：`projects/<proj>/<sessionId>/subagents/*.jsonl`（子代理会话）暂不入索引。
- 索引是**快照**：每次启动重扫，Web 服务每 30 分钟自动重扫（`AGENTHUB_RESCAN_MINUTES` 可调），也可点右上角 ↻ 或运行 `scan`。
- 删除识别只基于各工具自己的删除记录；DSH 没有可靠的删除标记，已归档或脱离工作区的会话仍会显示（见 docs/dsh-storage-notes.md）。

## 路线图（后续）

- [ ] 增量/定时扫描（fswatch 或 launchd 守护）
- [ ] 反向导入：把交接文档"喂"回各工具的原生格式
- [ ] 更多工具接入（codewhale / qclaw / gemini / kimi …）
- [ ] 按工作目录聚合的跨工具时间线视图

