# -*- coding: utf-8 -*-
"""读取 DSH legacy/v3 zstd JSONL；原始会话始终只读。"""

import glob
import io
import json
import os
import re
from typing import List, Optional

import zstandard

from ..model import SessionRecord, Message, ts_to_ms, clip, split_think
from .base import tool_home

ROOT = os.path.join(tool_home('.dsh', 'AGENTHUB_DSH_HOME'), 'sessions')


def _iter_lines(path: str):
    """流式解压逐行解析；损坏记录必须向调用方报告。"""
    with open(path, 'rb') as f, zstandard.ZstdDecompressor().stream_reader(f, read_across_frames=True) as r:
        with io.TextIOWrapper(r, encoding='utf-8') as stream:
            for number, raw in enumerate(stream, 1):
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f'{path}:{number}: invalid DSH JSON') from exc
                if not isinstance(event, dict):
                    raise ValueError(f'{path}:{number}: DSH event must be an object')
                yield event


def _string(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _version(path: str) -> int:
    """会话文件的原生版本号：v4（桌面版）> v3 > legacy。用于同 ID 多文件时的排序。"""
    name = os.path.basename(path)
    if 'session.v4.' in name:
        return 4
    if 'session.v3.' in name:
        return 3
    return 1


# 与其他工具的系统注入同类，不是任何人说的话：运行时上下文快照 / 工作区指令 / 收件箱拼接记录。
# v4 桌面版把这些注入也都写成 user/message，来源是 runtime-context / skill-catalog / tool-jobs /
# model-selection / compact-checkpoint，与 v3 的 plugin / agent-instructions 一并过滤。
NOISE_SOURCES = {'plugin', 'agent-instructions', 'runtime-context', 'skill-catalog',
                 'tool-jobs', 'model-selection', 'compact-checkpoint'}
NOISE_EVENTS = {'agent/inbox/spliced', 'subagent/descriptor'}
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
GOAL_OPS = {'create': '设定', 'update': '更新', 'complete': '完成', 'clear': '清除'}


def _describe(kind: str, d: dict):
    """协作事件 → (一句话说明, 关联会话 ID)。未知事件保留原始 JSON，不丢信息。"""
    if kind == 'subagent/catalog':
        return f"创建子任务：{d.get('label') or d.get('childId', '')}", d.get('childId', '')
    if kind == 'goal/change':
        goal = d.get('goal') or {}
        objective = goal.get('objective', '') if isinstance(goal, dict) else _string(goal)
        op = GOAL_OPS.get(d.get('operation'), d.get('operation') or '变化')
        return f"目标{op}：{objective}", ''
    if kind == 'tool-workflow/run-start':
        return f"工作流开始：{d.get('name', '')}", ''
    if kind == 'tool-workflow/run-end':
        return f"工作流结束：{d.get('stopReason', '')}", ''
    if kind == 'tool-workflow/agent-start':
        phase = f"（{d['phase']}）" if d.get('phase') else ''
        return f"工作流派发子任务：{d.get('label', '')}{phase}", d.get('childId', '')
    if kind == 'tool-workflow/agent-end':
        return f"工作流子任务结束：{d.get('outcome', '')}", ''
    return _string(d), ''


class DshReader:
    tool = 'dsh'

    def scan(self) -> List[SessionRecord]:
        # 先选每个目录的原生最新版本（v4 > v3 > legacy），避免新版损坏时静默回退旧版。
        files = {}
        for name in ('session.jsonl.zstd', 'session.v3.jsonl.zstd', 'session.v4.jsonl.zstd'):
            for path in sorted(glob.glob(os.path.join(ROOT, '*', '*', name))):
                files[os.path.dirname(path)] = path
        records, self.errors = {}, []
        for path in files.values():
            try:
                record = self._scan_file(path)
            except Exception as e:  # 新版文件坏了只报告，不回退到同目录旧版，也不连累其他会话
                self.errors.append(f"{path}: {type(e).__name__}: {e}")
                continue
            if record is None:  # 空会话（无任何对话/协作内容），跳过
                continue
            previous = records.get(record.session_id)
            rank = lambda r: (_version(r.file_path), r.updated_at, r.file_path)
            if previous is None or rank(record) > rank(previous):
                records[record.session_id] = record
        return list(records.values())

    def _scan_file(self, path: str) -> Optional[SessionRecord]:
        header, title, first_user, model, label = {}, '', '', '', ''
        first_ts = last_ts = n_user = n_asst = anonymous_calls = n_events = 0
        calls = set()
        for event in _iter_lines(path):
            kind, data = event.get('type'), event.get('data') or {}
            ts = ts_to_ms(event.get('time') or event.get('createdAt'))
            if ts:
                first_ts = min(first_ts or ts, ts)
                last_ts = max(last_ts, ts)
            if kind == 'session':
                header = event
            elif kind == 'session/title':
                # 自动生成的标题偶尔带着模型的 <think>（甚至未闭合），去掉后为空就沿用其他来源
                title = split_think(data.get('title') or '')[1] or title
            elif kind in ('request/context', 'model/selection'):
                model = data.get('model') or model
            elif kind == 'subagent/descriptor':
                label = data.get('label') or data.get('description') or label
            elif kind == 'user/message':
                if (data.get('source') or {}).get('kind') == 'user':
                    txt = self._content_text(data.get('content'))
                    if txt:
                        n_user += 1
                        first_user = first_user or txt
            elif kind in ('assistant/message', 'tool/call'):
                if kind == 'assistant/message':
                    n_asst += 1
                    blocks = (data.get('message') or {}).get('content') or []
                else:
                    blocks = [dict(data, type='tool-call', id=data.get('callId'))]
                for block in blocks:
                    if isinstance(block, dict) and block.get('type') == 'tool-call':
                        if block.get('id'):
                            calls.add(block['id'])
                        else:
                            anonymous_calls += 1
            elif kind == 'system/message':
                n_events += 1
            elif kind.startswith(('agent/', 'subagent/', 'goal/', 'tool-workflow/')) \
                    and kind not in NOISE_EVENTS:
                # 与 read_messages 会渲染成消息的协作事件保持一致，用来判断会话是否真的“有内容”
                n_events += 1
        if not header.get('id'):
            raise ValueError(f'{path}: missing DSH session header/id')
        if not (n_user or n_asst or calls or anonymous_calls or n_events):
            # 空会话：只写了会话头/权限/沙箱等初始配置事件，没有任何对话或协作内容。
            # 桌面版这类会话在自身列表里也标记为 blank，不建索引。
            return None
        parent = header.get('parentSession', '')
        # subagent 是委派出去的子任务；其他带 parentSession 的是从父会话派生（fork/seed）出来的。
        # 子任务的自动标题取自提示词开头（如 "You are reading a Python"），委派描述（label）更能说明它做什么
        origin = header.get('origin') or ('fork' if parent else '')
        return SessionRecord(
            tool=self.tool, session_id=header['id'], title=((label or title) if origin == 'subagent' else (title or label))
            or clip(first_user, 60) or '(无标题)',
            cwd=header.get('cwd', ''), file_path=path, started_at=first_ts, updated_at=last_ts,
            n_messages=n_user + n_asst, n_tool_calls=len(calls) + anonymous_calls,
            size_bytes=os.path.getsize(path),
            extra={'parent_session_id': parent, 'origin': origin,
                   'delegation_depth': header.get('delegationDepth', 0),
                   'label': label, 'model': model, 'deleted': False},
        )

    def read_messages(self, path: str) -> List[Message]:
        messages, calls = [], set()
        turn, starts = '', 0
        for number, event in enumerate(_iter_lines(path)):
            kind, data = event.get('type', ''), event.get('data') or {}
            if kind == 'turn/start':
                # 同一轮次号可能被重新开始（重试），用启动序号区分，避免两次尝试被当成一轮
                starts += 1
                turn = f"{data.get('turn', '')}#{starts}"
            native = data.get('message') or data
            native_id = str(native.get('id') or event.get('seq', number))
            source = kind
            if 'step' in data:
                source += ':step:' + str(data['step'])
            meta = dict(timestamp=ts_to_ms(event.get('time')) / 1000,
                        turn_id=turn or str(data.get('turn', '')), source=source)
            if kind == 'user/message':
                src = data.get('source') if isinstance(data.get('source'), dict) else {}
                source_kind = src.get('kind', '')
                if source_kind in NOISE_SOURCES:
                    continue
                text = self._content_text(data.get('content'))
                if text:
                    human = source_kind == 'user'
                    # 子任务回报 / 主任务来信等：关联到发送方会话，前端可跳转
                    link = '' if human else (src.get('senderSessionId') or
                                             next(iter(UUID_RE.findall(text[:300])), ''))
                    messages.append(Message(role='user' if human else 'system', text=text,
                                            message_id=native_id, link=link,
                                            kind='text' if human else 'event',
                                            **dict(meta, source=source + ':' + source_kind)))
            elif kind in ('assistant/message', 'tool/call', 'tool/result', 'system/message'):
                blocks = native.get('content') or []
                if kind == 'tool/call':
                    blocks = [dict(data, type='tool-call', id=data.get('callId'))]
                if kind == 'tool/result' and not blocks and 'output' in data:
                    blocks = [dict(type='tool-result', content=data['output'], toolCallId=data.get('callId'))]
                for index, block in enumerate(blocks):
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get('type')
                    msg = dict(role='assistant', message_id=f'{native_id}:{index}', **meta)
                    if block_type in ('text', 'reasoning'):
                        messages.append(Message(kind='event' if kind == 'system/message' else
                                                ('thinking' if block_type == 'reasoning' else 'text'),
                                                text=block.get('text', ''), **msg))
                    elif block_type == 'tool-call':
                        call_id = str(block.get('id') or '')
                        if call_id and call_id in calls:
                            continue
                        if call_id:
                            calls.add(call_id)
                        messages.append(Message(kind='tool_call', call_id=call_id,
                                                tool_name=block.get('name', ''),
                                                tool_args=_string(block.get('arguments', '')), **msg))
                    elif block_type == 'tool-result':
                        content = block.get('content', '')
                        output = self._content_text(content) if isinstance(content, (str, list)) else _string(content)
                        messages.append(Message(kind='tool_result', call_id=str(block.get('toolCallId') or ''),
                                                tool_output=output, **msg))
            elif kind in NOISE_EVENTS:
                continue
            elif kind.startswith(('agent/', 'subagent/', 'goal/', 'tool-workflow/')):
                text, link = _describe(kind, data)
                messages.append(Message(role='system', kind='event', text=text, link=link,
                                        message_id=native_id, **meta))
        return messages

    @staticmethod
    def _content_text(content) -> str:
        if isinstance(content, str):
            return content
        return '\n'.join(b.get('text', '') for b in content or []
                         if isinstance(b, dict) and b.get('type') == 'text')
