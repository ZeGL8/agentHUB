"""agentHUB — 本地 AI agent 工具的统一会话索引与导出器。

支持的工具：Claude Code / Codex / DSH (DeepSeek harness) / Hermes / OpenCode / WorkBuddy
"""

__version__ = "0.1.0"

import os

SUPPORTED_TOOLS = ["claude", "codex", "dsh", "hermes", "opencode", "workbuddy"]

# agentHUB 自己的数据目录（索引、导出、配置），可用 AGENTHUB_HOME 覆盖
DATA_DIR = os.path.expanduser(os.environ.get("AGENTHUB_HOME") or os.path.join("~", ".agenthub"))
