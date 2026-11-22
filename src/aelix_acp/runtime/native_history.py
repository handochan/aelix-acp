"""Read the pinned native get_messages envelope one message at a time."""

import codecs
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
ARGUMENTS = (b'"input"', b'"arguments"')
OMIT_BLOCK_STRINGS = frozenset({
    b'"data"', b'"source"', b'"text_signature"', b'"thinking_signature"', b'"thought_signature"',
    b'"textSignature"', b'"thinkingSignature"', b'"thoughtSignature"', b'"thinking"',
})
MAX_KEY_BYTES = max(map(len, OMIT_BLOCK_STRINGS))
SCALAR = re.compile(rb'(?:null|true|false|NaN|-?Infinity|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)')
# The base64 alphabet lets the common long image runs bypass the byte scanner.
BASE64 = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
TOOL_ARGUMENTS_OMITTED = "[Tool arguments omitted from recovered history.]"
OMITTED_RESULT = object()
TOOL_RESULT_OMITTED = "[Tool result omitted from recovered history.]"
IMAGE_OMITTED = "[Image omitted from recovered history.]"


class InvalidHistory(ValueError):
    """An incomplete, unsupported or over-budget native history."""


class TextParts(tuple):
    """One transcript string decoded in bounded pieces."""


class DiscardedArguments:
    """Validate the shape of unused JSON arguments without retaining them."""

    def __init__(self, opening):
        self.stack = [(opening, "first")]
        self.atom = bytearray()
        self.atom_ended = False
        self.size = 1

    def span(self, part):
        self.size += len(part)
        stripped = part.strip(b" \t\r")
        if stripped:
            if (self.atom_ended or (self.atom and part[:1] in b" \t\r")
                    or len(self.atom) + len(stripped) > TEXT_CHUNK):
                raise InvalidHistory
            self.atom.extend(stripped)
        if self.atom and part and part[-1] in b" \t\r":
            self.atom_ended = True

    def string_bytes(self, part):
        self.size += len(part)

    def punctuation(self, symbol):
        opening, state = self.stack[-1]
        array = opening == ord("[")
        if self.atom:
            if not (state == "value" or (array and state == "first")) or not SCALAR.fullmatch(self.atom):
                raise InvalidHistory
            self.atom.clear()
            state = "separator"
        self.atom_ended = False
        self.size += 1
        value = state == "value" or (array and state == "first")
        if symbol == ord('"') and not array and state in ("first", "key"):
            self.stack[-1] = (opening, "colon")
        elif symbol == ord(":") and state == "colon":
            self.stack[-1] = (opening, "value")
        elif symbol in b'{["' and value:
            self.stack[-1] = (opening, "separator")
            if symbol != ord('"'):
                if len(self.stack) >= 1024:
                    raise InvalidHistory
                self.stack.append((symbol, "first"))
        elif symbol == ord(",") and state == "separator":
            self.stack[-1] = (opening, "value" if array else "key")
        elif symbol == (ord("]") if array else ord("}")) and state in ("first", "separator"):
            self.stack.pop()
        else:
            raise InvalidHistory


class HistoryResponse:
    """Retain a bounded projection of each message in a native response.

    Native places the image discriminator after its data. Discard
    image payloads, provider signatures and other unused strings on direct
    content blocks regardless of key order. Drop unused tool arguments too.
    The pinned tool result has its call ID before its content.
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
        self.discard_utf8 = None
        self.discard_block_string = False
        self.argument = None
        self.argument_string_size = None
        self.oversized_arguments = set()
        self.is_tool_result = False
        self.oversized_result = False
        self.result_bytes = 0
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

    def _keep_string(self, part, *, final=False):
        if self.text_buffer is not None:
            if self.is_tool_result:
                self.result_bytes += len(part)
                if self.result_bytes > TOOL_CONTENT_BYTES and not self.oversized_result:
                    self.oversized_result = True
                    self.text_fields.clear()
                    self.text_parts.clear()
            else:
                self._charge(len(part))
            self.text_buffer.extend(part)
        elif self.discard_utf8 is not None:
            if self.argument is not None:
                self.argument.string_bytes(part)
            if self.argument_string_size is not None:
                self.argument_string_size += len(part)
            if final or not part.isascii() or self.discard_utf8.getstate()[0]:
                try:
                    self.discard_utf8.decode(part, final=final)
                except UnicodeError:
                    raise InvalidHistory from None
        else:
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
                if not self.oversized_result:
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
            self.record_tokens = self.result_bytes = 0
            self.is_tool_result = self.oversized_result = False
            self._keep(b"{")
            return pos + 1
        raise InvalidHistory

    def skip_base64_run(self, chunk):
        """Handle a base64-only slice with bounded work and no decoded copy."""
        if self.discard_block_string and not (self.escaped or self.escape_digits) and not chunk.translate(None, BASE64):
            self._keep_string(chunk)
            return True
        return False

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
        if self.discard_block_string and len(chunk) - pos >= 1024 and self.skip_base64_run(chunk[pos:]):
            return len(chunk)
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
            if not self.oversized_result:
                self.text_fields[field] = (
                    TextParts(self.text_parts) if len(self.text_parts) > 1 else next(iter(self.text_parts), "")
                )
            self.text_buffer = None
            self.text_parts.clear()
        elif self.discard_utf8 is not None and symbol == ord('"'):
            self._keep_string(chunk[pos:end - 1], final=True)
            if self.argument is not None:
                self.argument.size += 1
            if self.argument_string_size is not None:
                if self.argument_string_size > MAX_RECORD_BYTES:
                    self.oversized_arguments.add(self.content_index)
                self.argument_string_size = None
        else:
            self._keep_string(chunk[pos:end])
        if symbol == ord("\\"):
            self.escaped = True
        elif symbol == ord('"'):
            size = len(self.record) - self.string_start
            self.last_string = (
                bytes(self.record[self.string_start:]) if self.discard_utf8 is None and size <= MAX_KEY_BYTES else None
            )
            self.string_start = None
            self.discard_utf8 = None
            self.discard_block_string = False
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
            self.discard_block_string = direct_block and self.key in OMIT_BLOCK_STRINGS
            if direct_block and self.key in ARGUMENTS:
                self.argument_string_size = 0
            if self.discard_block_string or text or self.argument_string_size is not None:
                self._keep(b'"')
                self.discard_utf8 = codecs.getincrementaldecoder("utf-8")()
            if text:
                self.text_buffer = bytearray()
            self.key = None
        elif symbol == ord(":"):
            self.key = self.last_string
            # The pinned ToolResultMessage writes its call ID before content.
            if len(self.stack) == 1 and self.key in (b'"tool_call_id"', b'"toolCallId"'):
                self.is_tool_result = True
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
                    for index in self.oversized_arguments:
                        message["content"][index]["argumentsOmitted"] = True
                    if self.oversized_result:
                        message["content"] = OMITTED_RESULT
                    else:
                        self._charge(self.result_bytes)
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
                self.oversized_arguments.clear()
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
                if self.argument is not None:
                    self.argument.span(chunk[pos:edge.start() if edge is not None else end])
                    if edge is not None:
                        symbol = chunk[end - 1]
                        self.argument.punctuation(symbol)
                        if symbol == ord('"'):
                            self.string_start = len(self.record)
                            self.discard_utf8 = codecs.getincrementaldecoder("utf-8")()
                        elif not self.argument.stack:
                            if self.argument.size > MAX_RECORD_BYTES:
                                self.oversized_arguments.add(self.content_index)
                            self.argument = None
                elif (edge is not None and chunk[end - 1] in b"{["
                        and self.content_array and len(self.stack) == 3 and self.key in ARGUMENTS):
                    self._keep(chunk[pos:end - 1])
                    self._keep(b"null")
                    self.argument = DiscardedArguments(chunk[end - 1])
                    self.key = self.last_string = None
                else:
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


def omitted_tool_content(text):
    return [{"type": "content", "content": text_block(text)}]


def tool_content(message):
    if message.get("content") is OMITTED_RESULT:
        return omitted_tool_content(TOOL_RESULT_OMITTED)
    content = []
    remaining = TOOL_CONTENT_BYTES
    for block in content_blocks(message):
        for text in display_parts(block):
            # All blocks stay in one replacement update.
            remaining -= 64  # JSON structure around each text block.
            for offset in range(0, len(text), TEXT_CHUNK):
                quoted = json.dumps(text[offset:offset + TEXT_CHUNK], ensure_ascii=False)
                remaining -= len(quoted.encode("utf-8")) - 2
                if remaining < 0:
                    return omitted_tool_content(TOOL_RESULT_OMITTED)
            content.append({"type": "content", "content": text_block(text)})
    return content


def tool_identity(value):
    value = string(value)
    if not value or len(value) > TEXT_CHUNK:
        raise InvalidHistory
    return value
