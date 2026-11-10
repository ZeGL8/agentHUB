# DSH 存储核查（2026-09-26）

只读核查本机 `~/.dsh/storages`、会话事件结构与已安装的
`/opt/homebrew/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/` 源码；没有修改原生数据或转储消息正文。

- 同目录的 `session.v4.jsonl.zstd`（桌面版）优先于 `session.v3.jsonl.zstd`，再优先于 `session.jsonl.zstd`。新版不可读时扫描报错，不回退旧版。跨目录最终按原生 header.id 去重，版本高者优先。
- `origin == "subagent"` 才使用 `parentSession` 建树。带 `parentSession` 的 seeded/fork 会话不自动归入子代理。
- 工具调用的 assistant 内容块 `id` 与独立 tool/call 的 `callId` 是同一身份，只保留一次。工具结果使用 `toolCallId` 关联。
- user/message 的非 user 来源以及 agent/subagent/goal/tool-workflow 事件予以保留；来源携带原生 step，消息携带 turn。

## 桌面版 v4 变更（2026-10-17）

换用桌面版 DeepSeek Harness 后，会话文件更名为 `sessions/<proj>/<sessionDir>/session.v4.jsonl.zstd`，header 的 `version` 为 4。事件结构与 v3 一致（`type/seq/time/data`），故读取器仅需把 v4 纳入版本选择即可恢复全部记录。

v4 新增了几类**注入内容**，与 v3 的 `plugin` / `agent-instructions` 一样写在 `user/message` 里，来源是：`runtime-context`（运行时上下文快照）、`skill-catalog`（技能目录）、`tool-jobs`（后台任务回执）、`model-selection`（模型切换说明）、`compact-checkpoint`（压缩检查点）。这些不是任何人说的话，读取器一并过滤；`user-approval` / `subagent-settled` / `agent-message` 仍作为协作事件保留。

## 删除识别边界

`dsh-workspace/lib/index.js:228–239` 明确说明 `archivedSessionIds` 是可恢复的归档集合，归档会保留 `sessionIds` 中的位置。不能把归档当删除。

同文件 `:392–400` 明确说明删除 workspace 只删除注册，保留目录及所有会话日志；`:148–153` 的 `detachSession` 仅解除工作区归属。`:441–445` 明确说明会话是否存在与 workspace accounting（包括没有 accounting）无关。`:461–470` 的 `sessionKnown` 依次检查 live、header index 和新鲜的 persistence listing。

因此 workspace.sessionIds 不是全部有效会话的权威白名单；其补集不能用来判定删除。已安装版本和本机存储中未发现可靠的会话删除墓碑。reader 只读取仍存在的原生文件，不推断已移除工作区、脱离工作区、归档或父会话缺失的记录已经删除；无法识别外部删除后刻意保留下来的无标记副本。没有父任务删除证据时不级联隐藏后代。
