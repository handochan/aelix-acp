"""Session resource identifiers and their native namespace."""
from __future__ import annotations

import hmac
import re
import secrets

from .profile_refs import display_text, redact

KEY_PREFIX = "selection:"
# Native uses reversible hex for read-only row keys that are not valid UTF-8.
RESERVED_PREFIXES = (KEY_PREFIX, "readonly:")
OPAQUE_KEY = re.compile(r"selection:[0-9a-f]{64}")
MAX_KEY_LENGTH = 8192


def ordinary_name(name):
    return isinstance(name, str) and len(name) <= MAX_KEY_LENGTH and safe_label(name)


def safe_label(name):
    return (bool(name.strip())
            and name not in (".", "..") and not name.startswith(RESERVED_PREFIXES)
            and "/" not in name and "\\" not in name and not re.match(r"^[A-Za-z]:", name)
            and display_text(name) == name and redact(name) == name)


def public_text_fields(row, *fields):
    return {**row, **{field: display_text(redact(row[field]), multiline=True)
                     for field in fields if isinstance(row.get(field), str)}}


def public_name(name):
    return ordinary_name(name) or (isinstance(name, str) and OPAQUE_KEY.fullmatch(name) is not None)


class ResourceIdentifiers:
    def __init__(self):
        self.secret = secrets.token_bytes(32)

    def key(self, kind, name):
        if ordinary_name(name):
            return name
        payload = (kind + "\0" + name).encode("utf-8", errors="surrogatepass")
        return KEY_PREFIX + hmac.digest(self.secret, payload, "sha256").hex()

    def index(self, kind, names):
        index = {}
        for name in names:
            key = self.key(kind, name)
            if index.setdefault(key, name) != name:
                raise ValueError("Ambiguous resource identifier")
        return index

    def names(self, kind, names):
        if names is None:
            return None
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("Invalid resource names")
        return list(self.index(kind, names))
