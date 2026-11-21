"""Consume native JSONL using bounded memory, including large image histories."""

import re

CONTROL_PREFIX = 1024
FRAME_LIMIT = 16 * 1024**2
HISTORY_LIMIT = 64 * 1024**2
# Native dataclass AgentEndEvent serializes its messages first, while some
# workers emit a type-first object. Neither form carries control state.
IGNORED = re.compile(rb'^\s*\{\s*(?:"messages"\s*:\s*\[|"type"\s*:\s*"(?:agent_end|turn_end)"\s*[,}])')
RESPONSE = re.compile(
    rb'^\s*\{\s*"type"\s*:\s*"response",\s*"command"\s*:\s*"([a-z_]+)",'
    rb'\s*"success"\s*:\s*(?:true|false),\s*"id"\s*:\s*"([0-9a-f]{32})"'
)
TOOL_ID = re.compile(
    rb'^\s*\{\s*(?:"type"\s*:\s*"tool_execution_end",\s*)?"tool_?[cC]all_?[iI]d"\s*:\s*("(?:[^"\\]|\\.)*")'
)
TOOL_END = re.compile(rb'"type"\s*:\s*"tool_execution_end"\s*[,}]')
TOOL_FAILED = re.compile(rb'"is_?[eE]rror"\s*:\s*true\s*[,}]')


async def read_frames(reader):
    """Yield a bounded line or a prefix identifying an oversized frame.

    Native emits type and correlation fields before large content. Agent/turn
    end only repeat history, so drain them without buffering or parsing JSON.
    """
    line = bytearray()
    tail = b""
    limit = None
    ignored = oversized = False
    while chunk := await reader.read(65536):
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = len(chunk) if newline < 0 else newline + 1
            if not ignored and not oversized:
                line.extend(chunk[start:end])
                if limit is None and (len(line) >= CONTROL_PREFIX or newline >= 0):
                    ignored = IGNORED.match(line) is not None
                    response = RESPONSE.match(line)
                    limit = HISTORY_LIMIT if response and response[1] == b"get_messages" else FRAME_LIMIT
                if limit is not None and len(line) > limit:
                    del line[CONTROL_PREFIX:]
                    oversized = True
            if oversized:
                tail = (tail + chunk[max(start, end - CONTROL_PREFIX) : end])[-CONTROL_PREFIX:]
            if newline >= 0:
                if not ignored:
                    yield line, tail if oversized else None
                line = bytearray()
                tail = b""
                limit = None
                ignored = oversized = False
            start = end
