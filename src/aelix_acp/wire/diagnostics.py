"""Bounded stderr filtering for native workers that reopen a transcript."""

from pathlib import Path
import re

MAX_LINE_BYTES = 16 * 1024
RECOVERY = b"Recovered damaged session file "
RECOVERY_NOTICE = b"[aelix-acp] Native session recovered from damaged history.\n"
LOGGER = re.compile(r"^(?:DEBUG|INFO|WARNING|ERROR|CRITICAL):aelix[\w]*(?:\.[\w]+)*:")
PATH_LIKE = re.compile(
    r"[\\/]|%2f|%5c|(?<!\w)(?:[a-z]:|~)\S|"
    r"(?<![\w.])(?:\.[\w-]+|(?=[\w.-]*[^\W\d_])[\w-]+(?:\.[\w-]+)+)|"
    r"\b(?:file|path|directory|filename)\s*[:=]\s*\S",
    re.IGNORECASE,
)


class ReopenDiagnostics:
    """Forward complete path-free lines and redact native transcript recovery."""

    def __init__(self, session_file, session_id):
        name = Path(session_file).name
        # Match known path fragments too. A POSIX filename can contain a line
        # break, and its later pieces need not have a slash or an extension.
        self.private = tuple(part.casefold() for path in (str(session_file), name)
                             for part in path.splitlines() if part)
        self.prefix = f"[aelix:{session_id[:8]}] ".encode()
        self.line = bytearray()
        self.overflow = False
        self.reported_recovery = False

    def _finish_line(self):
        notice = None
        if RECOVERY in self.line:
            if not self.reported_recovery:
                self.reported_recovery = True
                notice = RECOVERY_NOTICE
        elif self.line and not self.overflow:
            raw = bytes(self.line).removesuffix(b"\r")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and text.isprintable() and not text[0].isspace():
                body = LOGGER.sub("", text, count=1)
                if not PATH_LIKE.search(body) and not any(part in text.casefold() for part in self.private):
                    notice = self.prefix + raw + b"\n"
        self.line.clear()
        self.overflow = False
        return notice

    def feed(self, chunk):
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            if not self.overflow:
                available = MAX_LINE_BYTES - len(self.line)
                self.line.extend(part[:available])
                self.overflow = len(part) > available
            if index < len(parts) - 1:
                if notice := self._finish_line():
                    yield notice

    def finish(self):
        return self._finish_line()
