"""D16 projection of native project selections."""
from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import stat

from .profile_refs import file_stamp
from .profile_reselection import Operation

EFFECTS = {"tool": "next_request", "skill": "next_request", "mcp": "restart"}
LIST = "selections/list"
SET = "selections/set"
CREATE_TITLE = "This creates .aelix/settings.json: the folder falls under project trust."
CREATE_HINT = "next launch may ask; -p may need --approve"
LOCK_REASONS = {"cli": "launch-locked", "profile": "profile-locked",
                "shadowed": "shadowed", "unavailable": "unavailable"}


class SelectionWrite(Operation):
    def __init__(self, resources):
        super().__init__(resources)
        self.reply = None
        self.confirm_id = None
        self.consent_cause = None
        self.confirmation_sent = False
        self.target = None

    def awaiting_native(self):
        return self.reply is not None and not self.reply.done()

    def claim_confirmation(self, event):
        identifier = event.get("id")
        if (self.awaiting_native() and self.confirm_id is None
                and isinstance(identifier, str) and identifier
                and event.get("title") == CREATE_TITLE and event.get("message") == CREATE_HINT
                and self.target.path.name == "settings.json"):
            self.confirm_id = identifier
            return True
        return False

    def before_write(self):
        self.require_current()
        self.target.require_version(self.target.before)
        super().before_write()


def file_label(kind):
    return ".aelix/" + ("mcp.json" if kind == "mcp" else "settings.json")


def project_target(session, kind, scopes):
    project = scopes["project"]["path"]
    try:
        target = Path(session.cwd) / file_label(kind)
        if not isinstance(project, str) or Path(project) != target:
            return None
        return TargetFence(target, scopes["global"]["path"])
    except UnsafeTarget:
        return None


class UnsafeTarget(ValueError):
    def __init__(self):
        super().__init__("Project selection file identity could not be verified")


def is_alias(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def directory_identity(path, *, optional=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    if is_alias(info) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeTarget()
    return info.st_dev, info.st_ino


def target_stamp(path, *, project=False):
    """Verify with two handles to keep stat and fstat timestamps comparable on Windows."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if is_alias(info) or not stat.S_ISREG(info.st_mode):
        raise UnsafeTarget()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    stamps = []
    for _ in range(2):
        with os.fdopen(os.open(path, flags), "rb") as file:
            current = os.fstat(file.fileno())
            if not stat.S_ISREG(current.st_mode) or (project and current.st_nlink != 1):
                raise UnsafeTarget()
            stamps.append(file_stamp(current))
    if stamps[0] != stamps[1] or path.resolve(strict=True) != path:
        raise UnsafeTarget()
    return stamps[0]


class TargetFence:
    """ACP's observations under the session write lease. Native owns the filesystem lock."""

    def __init__(self, path, global_path):
        if not isinstance(global_path, str) or not Path(global_path).is_absolute():
            raise UnsafeTarget()
        self.path = path
        self.global_path = Path(global_path)
        self.before = self.version()

    def version(self):
        try:
            path = self.path
            cwd = directory_identity(path.parent.parent)
            parent = directory_identity(path.parent, optional=True)
            global_target = self.global_path.resolve()
            if path.resolve() != path or global_target == path:
                raise UnsafeTarget()
            project = target_stamp(path, project=True)
            global_version = target_stamp(global_target)
            if (directory_identity(path.parent.parent) != cwd
                    or directory_identity(path.parent, optional=True) != parent
                    or self.global_path.resolve() != global_target):
                raise UnsafeTarget()
            return cwd, parent, project, (global_target, global_version)
        except (OSError, ValueError, RuntimeError):
            raise UnsafeTarget() from None

    @property
    def exists(self):
        return self.before[2] is not None

    def require_version(self, expected):
        if self.version() != expected:
            raise UnsafeTarget()

    def verify_save(self, written):
        after = self.version()
        if not written:
            if after != self.before:
                raise UnsafeTarget()
            return after
        cwd, parent, file, global_version = self.before
        if (after[0] != cwd or (parent is not None and after[1] != parent)
                or after[2] is None or (file is not None and after[2][:2] == file[:2])
                or after[3] != global_version):
            raise UnsafeTarget()
        return after


def native_refusal(message):
    """Recognize native validation that occurs before a save can start."""
    if not isinstance(message, str):
        return None
    if message.startswith("Not saved: creating ") and message.endswith(" was not confirmed"):
        return "consent-required"
    if message == "Command cancelled":
        return "cancelled"
    if message in ("Runtime mutations require an idle session",
                   "A classic RPC command is still running; retry when it has answered"):
        return "busy"
    if message.startswith("Cannot save to "):
        return "scope-readonly"
    if (message.startswith(("Unknown tool row: ", "Unknown skill row: ", "Unknown mcp row: ",
                            "Not saved: Cannot save to ", "Not saved: Cannot save: "))
            or (message.startswith("Not saved: ") and "; nothing written" in message)):
        return "write-failed"
    return None


def exposed_scope(kind, scope, status, safe_target):
    exists = status["exists"]
    writable = status["writable"]
    if type(exists) is not bool or type(writable) is not bool:
        raise ValueError("Invalid selection scope")
    if scope == "global":
        return {"exists": exists, "writable": False, "reason": "management-helper"}
    reason = status["reason"]
    if not safe_target:
        reason, writable = "scope-readonly", False
    elif writable:
        reason = None
    elif reason == "this folder is not trusted":
        reason = "untrusted-project"
    elif kind == "mcp" and isinstance(reason, str) and reason.startswith("aelix reads one mcp.json:"):
        reason = "project-file-required"
    elif kind == "mcp" and isinstance(reason, str) and "$AELIX_MCP_CONFIG" in reason:
        reason = "launch-locked"
    else:
        reason = "scope-readonly"
    return {"exists": exists, "writable": writable, "reason": reason}


def check_snapshot(kind, data):
    if data["kind"] != kind or data["effect"] != EFFECTS[kind]:
        raise ValueError("Invalid selection snapshot")
    for field in ("items", "unreadable", "problems", "notices"):
        if not isinstance(data[field], list):
            raise ValueError("Invalid selection snapshot")
    if data["unreadable"]:
        raise ValueError("Unreadable selection snapshot")
    rows = {}
    for row in data["items"]:
        key = row["key"]
        if not isinstance(key, str) or key in rows or not isinstance(row["label"], str):
            raise ValueError("Invalid selection row")
        rows[key] = row
    return rows


def public_row(kind, row, tools):
    result = {field: row[field] for field in ("key", "label", "role", "saved", "decidedBy",
                                              "applied", "inForce", "live", "pending")}
    if (result["role"] not in ("item", "placeholder", "info")
            or result["decidedBy"] not in ("global", "project", None)
            or any(type(result[field]) is not bool for field in ("saved", "applied", "inForce", "pending"))
            or (result["live"] is not None and type(result["live"]) is not bool)):
        raise ValueError("Invalid selection row")
    lock = row["lock"]
    result["lock"] = None
    if lock is not None:
        if not isinstance(lock["kind"], str) or type(lock["value"]) is not bool:
            raise ValueError("Invalid selection lock")
        result["lock"] = {"kind": lock["kind"], "value": lock["value"],
                          "reason": LOCK_REASONS.get(lock["kind"], "locked")}
    result["loaded"] = (result["role"] == "item" and result["key"] in tools
                        if kind == "tool" else result["applied"])
    result["problem"] = "native-problem" if row["problem"] is not None else None
    return result


def project_readback(kind, native, rows, target, changes):
    if (native["scope"] != "project" or native["kind"] != kind or native["effect"] != EFFECTS[kind]
            or not isinstance(native["written"], list)
            or len(native["written"]) > 1
            or any(not isinstance(path, str) or Path(path) != target for path in native["written"])):
        raise ValueError("Invalid selection write response")
    labels = Counter(row["label"] for row in rows.values())
    keys = {rows[edit["key"]]["label"]: edit["key"] for edit in changes
            if labels[rows[edit["key"]]["label"]] == 1}
    if not isinstance(native["changed"], list) or not isinstance(native["mismatches"], list):
        raise ValueError("Invalid selection write response")
    changed, mismatches = {}, []
    for change in native["changed"]:
        label, saved = change["label"], change["saved"]
        if not isinstance(label, str) or type(saved) is not bool:
            raise ValueError("Invalid selection write response")
        if label in keys:
            changed[keys[label]] = saved
        elif labels[label] > 1:
            mismatches.append({"key": None, "reason": "native-mismatch"})
    for message in native["mismatches"]:
        label, sep, _ = message.rpartition(": saved, but ")
        key = keys.get(label) if sep else None
        mismatches.append({"key": key, "reason": "saved-value-differs" if key else "native-mismatch"})
    apply = native["apply"]
    if kind == "mcp":
        if apply is not None:
            raise ValueError("Invalid MCP apply response")
    else:
        apply = {key: apply[key] for key in ("applied", "deferred", "turnedOn", "turnedOff", "warning")}
        if (type(apply["applied"]) is not bool or type(apply["deferred"]) is not bool
                or (apply["applied"] and apply["deferred"])
                or any(not isinstance(apply[key], list) or any(not isinstance(name, str) for name in apply[key])
                       for key in ("turnedOn", "turnedOff"))
                or (apply["warning"] is not None and not isinstance(apply["warning"], str))):
            raise ValueError("Invalid selection apply response")
        if apply["warning"] is not None:
            apply["warning"] = "apply-warning" if apply["applied"] else "apply-failed"
    return {
        "accepted": True, "scope": "project", "kind": kind, "effect": EFFECTS[kind],
        "written": [file_label(kind)] if native["written"] else [],
        "changed": [{"key": key, "saved": changed[key]} for edit in changes if (key := edit["key"]) in changed],
        "mismatches": mismatches, "apply": apply,
    }
