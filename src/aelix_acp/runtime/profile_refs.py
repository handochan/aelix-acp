"""D16 profile identities and the display projection of native profiles."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

SCOPES = ("bundled", "user", "project")
REVISION = re.compile(r"sha256:[0-9a-f]{64}")
POLICY_FIELDS = {
    "builtinTools": "builtin_tools", "excludeTools": "exclude_tools",
    "inheritSkills": "inherit_skills", "extensions": "extensions",
    "inheritExtensions": "inherit_extensions", "contextFiles": "context_files",
    "thinking": "thinking", "role": "role", "outputCap": "output_cap",
    "timeoutMs": "timeout_ms", "agents": "agents",
}


MAX_PREVIEW_BYTES = 24 * 1024
MAX_REDACTION_INPUT = 16384
PATH_START = r"(?:file://|[a-z]:[\\/]|\\\\|/(?!/))"
REDACTION = re.compile(
    r"""(?ix)(?P<key>(?<!\w)["']?(?:api[_ -]?key|password|passwd|secret|"""
    r"""(?:access|refresh|auth)[_ -]?token|authorization)["']?[ \t]*[:=][ \t]*)"""
    r"""|(?P<bearer>\bbearer[ \t]+)"""
    r"|(?P<secret>\b(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}"
    r"|\bhttps?://[^\s/]+:[^\s/@]+@)"
    r"""|(?P<quoted_path>["'](?=""" + PATH_START + r"))"
    r"""|(?P<path>(?<![\w:/\\.])""" + PATH_START + r"""[^\s<>\x22\x27]+)"""
)
AUTH_SCHEME = re.compile(r"(?:bearer|basic|token)[ \t]+", re.IGNORECASE)
SCALAR_END = re.compile(r"""[^\s,;\]}]+""")
BLOCK = re.compile(r"[|>](?:[+-][1-9]?|[1-9][+-]?)?[ \t]*(?:#[^\n]*)?\n")


class DisplayTooLarge(ValueError):
    pass


def quoted_end(value, start):
    quote = value[start]
    index = start + 1
    while index < len(value):
        char = value[index]
        if char == "\\":
            if value[index + 1:index + 2] == "\n":
                return None
            index += 2  # Escaped quotes are part of the value.
        elif char == "\n":
            return None
        elif char == quote:
            if quote == "'" and value[index + 1:index + 2] == "'":
                index += 2  # YAML escapes single quotes by doubling them.
            else:
                return index + 1
        else:
            index += 1
    return None


def block_end(value, start, after_header):
    line_start = value.rfind("\n", 0, start) + 1
    indent = len(value[line_start:]) - len(value[line_start:].lstrip(" \t"))
    end = after_header
    while end < len(value):
        newline = value.find("\n", end)
        stop = len(value) if newline < 0 else newline + 1
        line = value[end:stop]
        if line.strip() and len(line) - len(line.lstrip(" \t")) <= indent:
            break
        end = stop
    return end


def credential_end(value, match):
    start = match.end()
    if match.lastgroup == "key" and "authorization" in match.group().casefold():
        if scheme := AUTH_SCHEME.match(value, start):
            start = scheme.end()
    if start == len(value):
        return start
    char = value[start]
    if char in "\"'":
        return quoted_end(value, start)
    if char in "|>":
        header = BLOCK.match(value, start)
        if header is None:
            return None
        end = block_end(value, match.start(), header.end())
        return end if end > header.end() or end == len(value) else None
    if match.lastgroup == "key":
        newline = value.find("\n", start)
        return len(value) if newline < 0 else block_end(value, match.start(), newline + 1)
    scalar = SCALAR_END.match(value, start)
    return scalar.end() if scalar else start


def redact(value):
    # Work on complete spans before applying any display-length cutoff.
    if len(value) > MAX_REDACTION_INPUT:
        return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    pieces = []
    position = 0
    while match := REDACTION.search(value, position):
        kind = match.lastgroup
        end = match.end()
        if kind in {"key", "bearer"}:
            end = credential_end(value, match)
        elif kind == "quoted_path":
            end = quoted_end(value, match.start())
        if end is None:
            return ""  # The extent of this value cannot be established.
        pieces.append(value[position:match.start()])
        pieces.append("[redacted path]" if kind in {"path", "quoted_path"} else "[redacted credential]")
        if value[end - 1:end] == "\n":
            pieces.append("\n")
        position = end
    return "".join(pieces) + value[position:]


def catalog_description(value):
    if not isinstance(value, str):
        raise ValueError("Invalid profile description")
    return display_text(redact(value))[:1024]


def display_text(value, *, multiline=False):
    if not isinstance(value, str):
        raise ValueError("Invalid profile text")
    return "".join(
        character if (multiline and character == "\n") or not unicodedata.category(character).startswith("C")
        else " " for character in value
    )


def valid_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()
            and value not in {".", "..", "--none"} and not any(c in value for c in "/\\:")
            and display_text(value) == value and redact(value) == value)


def valid_ref(value):
    return (isinstance(value, dict) and set(value) == {"name", "scope", "revision"}
            and valid_name(value["name"]) and value["scope"] in SCOPES
            and isinstance(value["revision"], str) and REVISION.fullmatch(value["revision"]) is not None)


def source_digest(scope, source_uri):
    return hashlib.sha256(f"studio-profile/1\0{scope}\0{source_uri}\0".encode("utf-8"))


def profile_ref(name, scope, source_uri, chunks):
    digest = source_digest(scope, source_uri)
    if isinstance(chunks, bytes):
        chunks = (chunks,)
    for chunk in chunks:
        digest.update(chunk)
    return {"name": name, "scope": scope, "revision": "sha256:" + digest.hexdigest()}


def source_label(scope, path):
    filename = path.name
    if (scope not in SCOPES or not filename.endswith(".md") or filename.startswith(".")
            or display_text(filename) != filename or any(c in filename for c in "/\\:")):
        raise ValueError("Invalid profile source")
    if redact(filename) != filename:
        raise ValueError("Invalid profile source")
    return f"{scope}:agents/{filename}"


def file_stamp(value):
    return (value.st_dev, value.st_ino, value.st_ctime_ns, value.st_mtime_ns, value.st_size)


def file_chunks(stream, max_bytes):
    remaining = max_bytes
    while True:
        chunk = stream.read(65536 if remaining is None else min(65536, remaining + 1))
        if not chunk:
            return
        if remaining is not None:
            remaining -= len(chunk)
            if remaining < 0:
                raise DisplayTooLarge()
        yield chunk


def file_identity(native, *, max_bytes=None):
    name, scope = native["name"], native["scope"]
    path = Path(native["file_path"] if "file_path" in native else native["filePath"])
    if not valid_name(name) or not path.is_absolute():
        raise ValueError("Invalid native profile")
    source = source_label(scope, path)
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.suffix.lower() != ".md":
        raise ValueError("Invalid profile file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(resolved, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Invalid profile file")
        if max_bytes is not None and before.st_size > max_bytes:
            raise DisplayTooLarge()
        ref = profile_ref(name, scope, resolved.as_uri(), file_chunks(stream, max_bytes))
        after = os.fstat(stream.fileno())
    stamp = file_stamp(after)
    if file_stamp(before) != stamp or path.resolve(strict=True) != resolved:
        raise ValueError("Profile changed during inspection")
    # Windows can report creation time through stat() and change time through
    # fstat(). Compare two handle snapshots, including the current path target.
    with os.fdopen(os.open(resolved, flags), "rb") as current:
        if file_stamp(os.fstat(current.fileno())) != stamp:
            raise ValueError("Profile changed during inspection")
    return ref, source, stamp


def path_labels(paths, source):
    if not isinstance(paths, list):
        raise ValueError("Invalid profile paths")
    parent = Path(source).parent
    labels = []
    for path in paths:
        candidate = Path(path)
        if candidate.is_relative_to(parent):
            labels.append(display_text(candidate.relative_to(parent).as_posix()))
        else:
            digest = hashlib.sha256(candidate.as_posix().encode("utf-8")).hexdigest()
            labels.append(f"external:{display_text(candidate.name)} (sha256:{digest})")
    return labels


def preview(native, ref, source):
    raw_prompt = native["body"]
    prompt = display_text(redact(raw_prompt), multiline=True)
    policy = {key: native[field] for key, field in POLICY_FIELDS.items()}
    policy["extensions"] = path_labels(policy["extensions"], native["file_path"])
    result = {
        "ref": ref, "source": source, "description": catalog_description(native["description"]),
        "promptMode": native["system_prompt"], "promptPreview": prompt[:512],
        "promptTruncated": len(raw_prompt) > 512 or len(prompt) > 512, "provider": native["provider"],
        "model": native["model"], "tools": native["tools"],
        "skills": path_labels(native["skills"], native["file_path"]),
        "approvalMode": native["approval_mode"], "policy": policy,
    }
    if result["promptTruncated"]:
        result["omittedPromptDigest"] = hashlib.sha256(raw_prompt[512:].encode("utf-8")).hexdigest()
    if not valid_preview(result, ref):
        raise ValueError("Invalid native profile preview")
    if not prompt and raw_prompt:
        result["safeForConsent"] = False
    return safe_preview(result)


def text_list(value):
    return isinstance(value, list) and all(isinstance(s, str) and display_text(s) == s for s in value)


def optional_text(value):
    return value is None or (isinstance(value, str) and display_text(value) == value)


def valid_preview(value, ref):
    if not isinstance(value, dict) or value.get("ref") != ref:
        return False
    try:
        source = value["source"]
        label = source_label(ref["scope"], Path(source.removeprefix(ref["scope"] + ":agents/")))
        policy = value["policy"]
        digest = value.get("omittedPromptDigest")
        return (
            source == label and optional_text(value["description"]) and isinstance(value["description"], str)
            and value["promptMode"] in ("append", "replace")
            and isinstance(value["promptPreview"], str) and len(value["promptPreview"]) <= 512
            and display_text(value["promptPreview"], multiline=True) == value["promptPreview"]
            and type(value["promptTruncated"]) is bool
            and (digest is None or (value["promptTruncated"] and isinstance(digest, str)
                                    and REVISION.fullmatch("sha256:" + digest)))
            and optional_text(value["provider"]) and optional_text(value["model"])
            and (value["tools"] is None or text_list(value["tools"])) and text_list(value["skills"])
            and value["approvalMode"] in ("inherit", "ask", "auto", "deny")
            and isinstance(policy, dict) and set(POLICY_FIELDS) <= policy.keys()
            and all(type(policy[key]) is bool for key in
                    ("builtinTools", "inheritSkills", "inheritExtensions", "contextFiles"))
            and text_list(policy["excludeTools"]) and text_list(policy["extensions"])
            and optional_text(policy["thinking"]) and policy["role"] in ("leaf", "orchestrator")
            and type(policy["outputCap"]) is int
            and (policy["timeoutMs"] is None or type(policy["timeoutMs"]) is int)
            and (policy["agents"] is None or text_list(policy["agents"]))
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def public_preview(value):
    fields = ("ref", "source", "description", "promptMode", "promptPreview", "promptTruncated",
              "provider", "model", "tools", "skills", "approvalMode")
    return {**{field: value[field] for field in fields},
            "policy": {field: value["policy"][field] for field in POLICY_FIELDS}}


def safe_preview(value):
    """Whitelist, redact and bound the same representation for cards and SQLite."""
    result = public_preview(value)
    if value.get("omittedPromptDigest"):
        result["omittedPromptDigest"] = value["omittedPromptDigest"]
    safe = value.get("safeForConsent", True) is True

    def authority(item):
        nonlocal safe
        if isinstance(item, list):
            if len(item) > 128:
                raise DisplayTooLarge()
            return [authority(entry) for entry in item]
        if isinstance(item, str):
            filtered = redact(item)[:512]
            safe = safe and filtered == item
            return filtered
        return item

    for field in ("provider", "model", "tools", "skills"):
        result[field] = authority(result[field])
    for field, item in result["policy"].items():
        result["policy"][field] = authority(item)
    result["description"] = catalog_description(result["description"])
    old_prompt = result["promptPreview"]
    result["promptPreview"] = display_text(redact(old_prompt), multiline=True)[:512]
    if old_prompt and not result["promptPreview"]:
        safe = False
    if not safe:
        result["safeForConsent"] = False
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_PREVIEW_BYTES:
        raise DisplayTooLarge()
    return result
