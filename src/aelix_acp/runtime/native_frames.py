"""Consume native JSONL using bounded memory, including large image histories."""

import json
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
TOOL_CANDIDATE = re.compile(rb'^\s*\{\s*"(?:tool_call_id|toolCallId|type)"\s*:')
SYMBOL = re.compile(rb'["{}\[\]:,\s]')
STRING_SYMBOL = re.compile(rb'["\\]')


class ToolEnvelope:
    """Keep bounded scalars at the root and inside the immediate result object."""

    def __init__(self):
        self.depth = 0
        self.in_result = False
        self.key = None
        self.last = None
        self.quoted = self.escaped = False
        self.token = bytearray()
        self.root = {}
        self.result = {}

    def _extend(self, chunk, start, end):
        if self.depth == 1 or (self.depth == 2 and self.in_result):
            available = CONTROL_PREFIX + 1 - len(self.token)
            if available > 0:
                self.token.extend(chunk[start : min(start + available, end)])

    def _finish(self):
        if len(self.token) <= CONTROL_PREFIX and self.token:
            try:
                self.last = json.loads(self.token)
            except (ValueError, UnicodeError):
                self.last = None
            if self.key is not None:
                fields = (
                    self.root if self.depth == 1 else self.result if self.in_result and self.depth == 2 else None
                )
                if fields is not None and self.key in (
                    "type",
                    "toolCallId",
                    "tool_call_id",
                    "isError",
                    "is_error",
                ):
                    fields[self.key] = self.last
                self.key = None
        else:
            self.last = None
        self.token.clear()

    def feed(self, chunk):
        position = 0
        while position < len(chunk):
            if self.escaped:
                self._extend(chunk, position, position + 1)
                self.escaped = False
                position += 1
                continue
            match = (STRING_SYMBOL if self.quoted else SYMBOL).search(chunk, position)
            if match is None:
                self._extend(chunk, position, len(chunk))
                return
            end = match.start()
            symbol = chunk[end]
            self._extend(chunk, position, end)
            position = end + 1
            if self.quoted:
                self._extend(chunk, end, position)
                if symbol == ord("\\"):
                    self.escaped = True
                else:
                    self.quoted = False
                    self._finish()
            elif symbol == ord('"'):
                self.quoted = True
                self._extend(chunk, end, position)
            else:
                if self.token:
                    self._finish()
                if symbol == ord(":"):
                    self.key = self.last if isinstance(self.last, str) else None
                elif symbol in b"{[":
                    if self.depth == 1:
                        self.in_result = symbol == ord("{") and self.key == "result"
                    self.depth += 1
                    self.key = self.last = None
                elif symbol in b"}]":
                    if self.depth == 2:
                        self.in_result = False
                    self.depth -= 1
                    self.key = self.last = None
                elif symbol == ord(","):
                    self.key = self.last = None

    def event(self):
        call_id = self.root.get("toolCallId") or self.root.get("tool_call_id")
        if self.depth == 0 and self.root.get("type") == "tool_execution_end" and isinstance(call_id, str):
            return {
                "type": "tool_execution_end",
                "toolCallId": call_id,
                "isError": any(
                    fields.get(key) is True
                    for fields in (self.root, self.result)
                    for key in ("isError", "is_error")
                ),
            }
        return {}


async def read_frames(reader):
    """Yield a bounded line or a prefix and projected envelope for a large frame."""

    line = bytearray()
    limit = None
    tool = None
    ignored = oversized = False
    while chunk := await reader.read(65536):
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = len(chunk) if newline < 0 else newline + 1
            if oversized:
                if tool is not None:
                    tool.feed(chunk[start:end])
            elif not ignored:
                line.extend(chunk[start:end])
                if limit is None and (len(line) >= CONTROL_PREFIX or newline >= 0):
                    ignored = IGNORED.match(line) is not None
                    response = RESPONSE.match(line)
                    limit = HISTORY_LIMIT if response and response[1] == b"get_messages" else FRAME_LIMIT
                if limit is not None and len(line) > limit:
                    if not ignored and TOOL_CANDIDATE.match(line) and not RESPONSE.match(line):
                        tool = ToolEnvelope()
                        tool.feed(line)
                    del line[CONTROL_PREFIX:]
                    oversized = True
            if newline >= 0:
                if not ignored:
                    yield line, (tool.event() if tool is not None else {}) if oversized else None
                line = bytearray()
                limit = tool = None
                ignored = oversized = False
            start = end
