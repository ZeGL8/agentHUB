"""项目启动入口：uv run server.py。"""

import argparse

from agenthub.server import DEFAULT_PORT, main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="启动 agentHUB 本地页面")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        main(args.port)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"启动失败：{exc}\n")
