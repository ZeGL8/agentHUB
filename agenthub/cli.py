# -*- coding: utf-8 -*-
"""agentHUB 命令行入口。

用法：
  agenthub scan                        # 扫描全部工具 → index/
  agenthub list [--tool claude] [--cwd 关键词]
  agenthub export --tool dsh --id <会话ID前缀>
  agenthub export --tool claude --path <会话文件路径>
  agenthub stats
"""

import argparse
import sys

from . import SUPPORTED_TOOLS, __version__
from .indexer import scan_all, write_index, load_index
from .exporter import export_by_id, export_by_path


def cmd_scan(args):
    print(f"agentHUB v{__version__} — 扫描本地 agent 会话存储…\n")
    tools = args.tools.split(",") if args.tools else None
    try:
        previous = load_index()
    except FileNotFoundError:
        previous = []
    try:
        records, problems = scan_all(tools, previous)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return 1
    if tools:  # 只扫了部分工具：其他工具沿用原索引，不能被冲掉
        records += [r for r in previous if r.tool not in tools]
    print(f"\n共 {len(records)} 个会话，{len(problems)} 个问题，正在生成索引…")
    mp = write_index(records, problems=problems)
    print(f"✅ 索引完成：{mp}")
    print(f"   机器可读：{mp.replace('index.md', 'index.json')}")
    return 0


def cmd_list(args):
    try:
        records = load_index()
    except FileNotFoundError as e:
        print(e)
        return 1
    if args.tool:
        records = [r for r in records if r.tool == args.tool]
    if args.cwd:
        records = [r for r in records if args.cwd.lower() in (r.cwd or "").lower()]
    if args.title:
        records = [r for r in records if args.title.lower() in (r.title or "").lower()]
    records.sort(key=lambda r: r.updated_at or 0, reverse=True)
    if not records:
        print("（无匹配会话）")
        return 0
    print(f"{'工具':<10} {'最近活动':<17} {'消息':>5} {'标题':<38} 会话ID")
    print("-" * 100)
    for r in records[: args.limit]:
        print(f"{r.tool:<10} {r.iso('updated') or '-':<17} {r.n_messages:>5} "
              f"{(r.title[:36] + '…') if len(r.title) > 36 else r.title:<38} "
              f"{r.session_id[:12]}")
    if len(records) > args.limit:
        print(f"…共 {len(records)} 条，仅显示前 {args.limit} 条（--limit 调整）")
    return 0


def cmd_export(args):
    if not args.tool:
        print("必须指定 --tool（" + "/".join(SUPPORTED_TOOLS) + "）")
        return 1
    mode = "full" if args.full else "final"
    try:
        if args.path:
            fp = export_by_path(args.tool, args.path, args.out, mode)
        elif args.id:
            fp = export_by_id(args.tool, args.id, args.out, mode=mode)
        else:
            print("必须指定 --id <会话ID> 或 --path <会话文件路径>")
            return 1
        print(f"✅ 已导出（{'完整过程' if mode == 'full' else '仅最终输出'}）：{fp}")
        return 0
    except (KeyError, FileNotFoundError) as e:
        print(f"❌ {e}")
        return 1


def cmd_stats(args):
    try:
        records = load_index()
    except FileNotFoundError as e:
        print(e)
        return 1
    by_tool = {}
    for r in records:
        d = by_tool.setdefault(r.tool, {"n": 0, "msg": 0, "tool": 0})
        d["n"] += 1
        d["msg"] += r.n_messages
        d["tool"] += r.n_tool_calls
    print(f"{'工具':<12} {'会话':>6} {'消息':>8} {'工具调用':>10}")
    print("-" * 44)
    for t in sorted(by_tool):
        d = by_tool[t]
        print(f"{t:<12} {d['n']:>6} {d['msg']:>8} {d['tool']:>10}")
    print("-" * 44)
    print(f"{'合计':<12} {len(records):>6} "
          f"{sum(x['msg'] for x in by_tool.values()):>8} "
          f"{sum(x['tool'] for x in by_tool.values()):>10}")
    return 0


def cmd_serve(args):
    from .server import main as serve
    serve(args.port)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="agenthub",
                                 description="本地 agent 会话统一索引与导出器")
    p.add_argument("--version", action="version", version=f"agenthub {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scan", help="扫描全部工具并生成索引")
    s.add_argument("--tools", help="逗号分隔的工具列表，默认全部")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("list", help="列出索引中的会话")
    s.add_argument("--tool", help="按工具过滤")
    s.add_argument("--cwd", help="按工作目录关键词过滤")
    s.add_argument("--title", help="按标题关键词过滤")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("export", help="导出会话为 Markdown 交接文档（默认仅最终输出）")
    s.add_argument("--tool", help="工具名：" + "/".join(SUPPORTED_TOOLS))
    s.add_argument("--id", help="会话 ID（支持前缀匹配）")
    s.add_argument("--path", help="会话文件路径（直接导出，不查索引）")
    s.add_argument("--full", action="store_true",
                   help="包含工具调用与结果的完整过程（默认仅对话正文）")
    s.add_argument("--out", help="输出目录，默认 exports/")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("stats", help="索引统计")
    s.set_defaults(func=cmd_stats)

    from .server import DEFAULT_PORT
    s = sub.add_parser("serve", help="启动 Web UI（只监听 127.0.0.1）")
    s.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="端口，默认 8322（或环境变量 AGENTHUB_PORT）")
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
