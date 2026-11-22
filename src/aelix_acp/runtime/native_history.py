"""Read the pinned native get_messages envelope one message at a time."""

import json
import re

from ..wire.errors import ProtocolError

# Keep the previous private-host budget for history ACP can actually replay.
# Image strings do not contribute to the budget.
HISTORY_LIMIT = 64 * 1024**2
MAX_MESSAGES = 65_536
MAX_DEPTH = 64
MAX_RECORD_BYTES = 4 * 1024**2
MAX_RECORD_TOKENS = 32_768
MAX_HISTORY_TOKENS = 1_048_576
MAX_UPDATES = 65_536
TEXT_CHUNK = 8192
# A tool result is one replacement update. Bound its indivisible write.
TOOL_CONTENT_BYTES = 1024**2
SCAN_BYTES = 8 * 1024
OPEN = re.compile(rb'\s*,\s*"data"\s*:\s*\{\s*"messages"\s*:\s*\[')
CLOSE = re.compile(rb'\s*\]\s*\}\s*\}\s*\n')
PUNCTUATION = re.compile(rb'["{}\[\]:,]')
STRING_EDGE = re.compile(rb'["\\\x00-\x1f]')
NON_SPACE = re.compile(rb'[^ \t\r]')
IMAGE_OMITTED = "[Image omitted from recovered history.]"


class InvalidHistory(ValueError):
    """An incomplete, unsupported or over-budget native history."""


class TextParts(tuple):
    """One transcript string decoded in bounded pieces."""


class HistoryResponse:
    """Retain a bounded projection of each message in a native response.

    Native places the image discriminator after its data. Discard string
    values of data/source on direct content blocks (including legacy image
    data URLs), regardless of key order. Keys at other depths are ordinary
    transcript data.
    """

    def __init__(self, prefix, offset):
        match = OPEN.match(prefix, offset)
        if match is None:
            raise InvalidHistory
        self.start = match.end()
        self.messages = []
        self.remaining = HISTORY_LIMIT
        self.tokens = 0
        self.record_tokens = 0
        self.record = bytearray()
        self.stack = []
        self.content_array = False
        self.content_index = 0
        self.text_fields = {}
        self.text_buffer = None
        self.text_parts = []
        self.key = None
        self.string_start = None
        self.last_string = None
        self.discard = False
        self.escape_digits = 0
        self.escaped = False
        self.array_state = "initial"
        self.tail = bytearray()

    def _charge(self, size):
        self.remaining -= size
        if self.remaining < 0:
            raise InvalidHistory

    def _keep(self, part):
        self._charge(len(part))
        if len(self.record) + len(part) > MAX_RECORD_BYTES:
            raise InvalidHistory
        self.record.extend(part)

    def _keep_string(self, part):
        if self.text_buffer is not None:
            self._charge(len(part))
            self.text_buffer.extend(part)
        elif not self.discard:
            self._keep(part)

    def _flush_text(self, *, final=False):
        raw = self.text_buffer
        length = len(raw)
        try:
            try:
                literal = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                if final or exc.reason != "unexpected end of data":
                    raise
                length = exc.start
                literal = raw[:length].decode("utf-8")
            text = json.loads('"' + literal + '"')
            if not final and text and 0xD800 <= ord(text[-1]) <= 0xDBFF:
                # Keep the escaped high surrogate with its possible pair.
                text = text[:-1]
                length -= 6
            if text:
                text.encode("utf-8")
                self.text_parts.append(text)
        except (ValueError, UnicodeError):
            raise InvalidHistory from None
        del raw[:length]

    def _outside_record(self, chunk, pos):
        if self.array_state == "end":
            self.tail.extend(chunk[pos:])
            if len(self.tail) > SCAN_BYTES:
                raise InvalidHistory
            return len(chunk)
        match = NON_SPACE.search(chunk, pos)
        if match is None:
            return len(chunk)
        pos = match.start()
        symbol = chunk[pos]
        if symbol == ord("]") and self.array_state in ("initial", "separator"):
            self.array_state = "end"
            return pos
        if symbol == ord(",") and self.array_state == "separator":
            self.array_state = "message"
            return pos + 1
        if symbol == ord("{") and self.array_state in ("initial", "message"):
            if len(self.messages) >= MAX_MESSAGES:
                raise InvalidHistory
            self.stack.append(symbol)
            self.record_tokens = 0
            self._keep(b"{")
            return pos + 1
        raise InvalidHistory

    def _string(self, chunk, pos):
        if self.escaped or self.escape_digits:
            symbol = chunk[pos]
            if self.escaped:
                if symbol not in b'"\\/bfnrtu':
                    raise InvalidHistory
                self.escape_digits = 4 if symbol == ord("u") else 0
                self.escaped = False
            else:
                if symbol not in b"0123456789abcdefABCDEF":
                    raise InvalidHistory
                self.escape_digits -= 1
            self._keep_string(chunk[pos:pos + 1])
            return pos + 1
        edge = STRING_EDGE.search(chunk, pos)
        end = edge.end() if edge is not None else len(chunk)
        if edge is None:
            self._keep_string(chunk[pos:end])
            return end
        symbol = chunk[end - 1]
        if symbol == ord('"') and self.text_buffer is not None:
            self._keep_string(chunk[pos:end - 1])
            self._flush_text(final=True)
            field = self.content_index if self.content_array else None
            self.text_fields[field] = (
                TextParts(self.text_parts) if len(self.text_parts) > 1 else next(iter(self.text_parts), "")
            )
            self.text_buffer = None
            self.text_parts.clear()
        else:
            self._keep_string(chunk[pos:end])
        if symbol == ord("\\"):
            self.escaped = True
        elif symbol == ord('"'):
            size = len(self.record) - self.string_start
            self.last_string = (
                bytes(self.record[self.string_start:]) if not self.discard and size <= 9 else None
            )
            self.string_start = None
            self.discard = False
        else:
            raise InvalidHistory
        return end

    def _punctuation(self, symbol):
        self.tokens += 1
        self.record_tokens += 1
        if self.tokens > MAX_HISTORY_TOKENS or self.record_tokens > MAX_RECORD_TOKENS:
            raise InvalidHistory
        if symbol == ord('"'):
            self.string_start = len(self.record) - 1
            direct_block = self.content_array and len(self.stack) == 3
            text = (direct_block and self.key == b'"text"') or (len(self.stack) == 1 and self.key == b'"content"')
            self.discard = text or (direct_block and self.key in (b'"data"', b'"source"'))
            if self.discard:
                self._keep(b'"')
            if text:
                self.text_buffer = bytearray()
            self.key = None
        elif symbol == ord(":"):
            self.key = self.last_string
        elif symbol in b"{[":
            if len(self.stack) == 1:
                self.content_array = symbol == ord("[") and self.key == b'"content"'
                self.content_index = 0
            self.stack.append(symbol)
            if len(self.stack) > MAX_DEPTH:
                raise InvalidHistory
            self.key = None
        elif symbol in b"}]":
            if self.stack.pop() != (ord("{") if symbol == ord("}") else ord("[")):
                raise InvalidHistory
            if len(self.stack) == 1:
                self.content_array = False
            self.key = None
            if not self.stack:
                try:
                    message = json.loads(self.record)
                    for index, text in self.text_fields.items():
                        if index is None:
                            message["content"] = text
                        else:
                            message["content"][index]["text"] = text
                except (ValueError, UnicodeError, KeyError, TypeError, IndexError):
                    raise InvalidHistory from None
                self.messages.append(message)
                self.record.clear()
                self.text_fields.clear()
                self.array_state = "separator"
        elif symbol == ord(","):
            if self.content_array and len(self.stack) == 2:
                self.content_index += 1
            self.key = None
        self.last_string = None

    def feed(self, chunk):
        pos = 0
        while pos < len(chunk):
            if self.string_start is not None:
                pos = self._string(chunk, pos)
                if (self.text_buffer is not None and len(self.text_buffer) >= TEXT_CHUNK
                        and not self.escaped and not self.escape_digits):
                    self._flush_text()
            elif not self.stack:
                pos = self._outside_record(chunk, pos)
            else:
                edge = PUNCTUATION.search(chunk, pos)
                end = edge.end() if edge is not None else len(chunk)
                self._keep(chunk[pos:end])
                if edge is not None:
                    self._punctuation(chunk[end - 1])
                pos = end

    def finish(self):
        if self.array_state != "end" or CLOSE.fullmatch(self.tail) is None:
            raise InvalidHistory
        return self.messages


def unavailable():
    return ProtocolError("Native session history is unavailable", -32603, {"kind": "native_history_unavailable"})


def content_blocks(message):
    content = message.get("content")
    if isinstance(content, (str, TextParts)):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise InvalidHistory
    return content


def display_parts(block):
    kind = block.get("type")
    if kind == "image":
        return (IMAGE_OMITTED,)
    if kind == "text":
        text = block.get("text")
        if isinstance(text, TextParts):
            return text
        if text := string(text):
            return (text,)
    return ()


def string(value):
    # A lone JSON surrogate cannot be sent back to an ACP client as UTF-8.
    if not isinstance(value, str):
        raise InvalidHistory
    if not value.isascii():
        value.encode("utf-8")
    return value


def text_block(text):
    return {"type": "text", "text": text}


def tool_content(blocks):
    content = []
    remaining = TOOL_CONTENT_BYTES
    for block in blocks:
        for text in display_parts(block):
            # All blocks stay in one replacement update.
            remaining -= 64  # JSON structure around each text block.
            for offset in range(0, len(text), TEXT_CHUNK):
                quoted = json.dumps(text[offset:offset + TEXT_CHUNK], ensure_ascii=False)
                remaining -= len(quoted.encode("utf-8")) - 2
                if remaining < 0:
                    raise InvalidHistory
            content.append({"type": "content", "content": text_block(text)})
    return content


def tool_identity(value):
    value = string(value)
    if not value or len(value) > TEXT_CHUNK:
        raise InvalidHistory
    return value
