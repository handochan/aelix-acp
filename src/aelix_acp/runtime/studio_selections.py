"""D16 projection of native project selections."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import stat
from threading import Thread

from .profile_refs import file_stamp
from . import profile_reselection
from .resource_identifiers import safe_label

EFFECTS = {"tool": "next_request", "skill": "next_request", "mcp": "restart"}
LIST = "selections/list"
SET = "selections/set"
CREATE_TITLE = "This creates .aelix/settings.json: the folder falls under project trust."
CREATE_HINT = "next launch may ask; -p may need --approve"
LOCK_REASONS = {"cli": "launch-locked", "profile": "profile-locked",
                "shadowed": "shadowed", "unavailable": "unavailable"}
TARGET_PROBE_WAIT = 5


class TargetProbeBusy(TimeoutError):
    pass


class SelectionTargetIO:
    """Bounded off-loop work for read-only target probes, independent of executor shutdown."""

    def __init__(self):
        self.slots = asyncio.BoundedSemaphore(4)

    async def run(self, probe, *args, operation=None, before_start=None):
        if operation is not None:
            operation.require_current()
        try:
            async with asyncio.timeout(TARGET_PROBE_WAIT):
                await self.slots.acquire()
        except TimeoutError:
            raise TargetProbeBusy() from None
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        def complete(value, error):
            self.slots.release()
            if not result.done():
                if error is None:
                    result.set_result(value)
                else:
                    result.set_exception(error)

        def work():
            value, error = None, None
            try:
                value = probe(*args)
            except BaseException as exc:
                error = exc
            # EOF can cancel the request and close the loop while the
            # filesystem still owns this call. No probe performs a write.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(complete, value, error)

        try:
            if operation is not None:
                operation.require_current()
            if before_start is not None:
                before_start()
            Thread(target=work, name="aelix-selection-target", daemon=True).start()
        except BaseException:
            self.slots.release()
            raise
        return await result


class SelectionWrite(profile_reselection.Operation):
    def __init__(self, resources):
        super().__init__(resources)
        self.reply = None
        self.confirm_id = None
        self.consent_cause = None
        self.confirmation_sent = False
        self.target = None
        self.kind = None
        self.native_keys = {}

    def awaiting_native(self):
        return self.reply is not None and not self.reply.done()

    def may_confirm_creation(self):
        return (self.binding is not None and self.session.controls.permission_mode == "default"
                and profile_reselection.host_verified_manual_approval(self))

    def claim_confirmation(self, event):
        identifier = event.get("id")
        if (self.awaiting_native() and self.confirm_id is None
                and isinstance(identifier, str) and identifier
                and event.get("title") == CREATE_TITLE and event.get("message") == CREATE_HINT
                and self.target.path.name == "settings.json"):
            self.confirm_id = identifier
            return True
        return False

    def missing_key_hint(self, message):
        prefix = f"Unknown {self.kind} row: "
        keys = []
        for public, native in self.native_keys.items():
            # Pin control_selection._quote truncates the native ID at 120 characters.
            quoted = native if len(native) <= 120 else native[:120] + "…"
            candidate = prefix + quoted
            if message == candidate or message.startswith(candidate + "; "):
                keys.append(public)
        return keys[0] if len(keys) == 1 else None


def file_label(kind):
    return ".aelix/" + ("mcp.json" if kind == "mcp" else "settings.json")


def project_target(session, kind, scopes):
    status = scopes["project"]
    # In particular, native does not inspect an untrusted project's files.
    if status["writable"] is not True:
        return None
    project = status["path"]
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
            if ((project is not None and global_version is not None and project[:2] == global_version[:2])
                    or directory_identity(path.parent.parent) != cwd
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
    if message.startswith(("Unknown tool row: ", "Unknown skill row: ", "Unknown mcp row: ")):
        return "selection-missing"
    if (message.startswith(("Not saved: Cannot save to ", "Not saved: Cannot save: "))
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
    if writable and not safe_target:
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


def check_snapshot(kind, data, identifiers):
    if data["kind"] != kind or data["effect"] != EFFECTS[kind]:
        raise ValueError("Invalid selection snapshot")
    for field in ("items", "unreadable", "problems", "notices"):
        if not isinstance(data[field], list):
            raise ValueError("Invalid selection snapshot")
    if data["unreadable"]:
        raise ValueError("Unreadable selection snapshot")
    rows = {}
    for row in data["items"]:
        if not isinstance(row["key"], str) or not isinstance(row["label"], str) or type(row["saved"]) is not bool:
            raise ValueError("Invalid selection row")
        key = identifiers.key(kind, row["key"])
        if key in rows:
            raise ValueError("Invalid selection row")
        rows[key] = row
    return rows


def public_row(kind, key, row):
    result = {field: row[field] for field in ("role", "saved", "decidedBy",
                                              "applied", "inForce", "live", "pending")}
    label = row["label"]
    result.update(key=key, label=label if safe_label(label) else "Selection item")
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
        result["lock"] = {"kind": lock["kind"] if safe_label(lock["kind"]) else "unknown", "value": lock["value"],
                          "reason": LOCK_REASONS.get(lock["kind"], "locked")}
    if kind == "tool":
        # ToolKind enumerates the registered tools and the excluded ones.
        # The latter carry the pin's "excluded" lock tag. Placeholders never load.
        loaded = result["role"] == "item" and not (lock and lock.get("tag") == "excluded")
        if type(result["live"]) is not bool or (result["live"] and not loaded):
            raise ValueError("Invalid native tool snapshot")
        result["loaded"] = loaded
    else:
        result["loaded"] = result["applied"]
    result["problem"] = "native-problem" if row["problem"] is not None else None
    return result


def project_readback(kind, native, target, identifiers):
    if (native["scope"] != "project" or native["kind"] != kind or native["effect"] != EFFECTS[kind]
            or not isinstance(native["written"], list)
            or len(native["written"]) > 1
            or any(not isinstance(path, str) or Path(path) != target for path in native["written"])):
        raise ValueError("Invalid selection write response")
    if (not isinstance(native["changed"], list) or not isinstance(native["mismatches"], list)
            or any(not isinstance(change["label"], str) or type(change["saved"]) is not bool
                   for change in native["changed"])
            or any(not isinstance(message, str) for message in native["mismatches"])):
        raise ValueError("Invalid selection write response")
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
        for field in ("turnedOn", "turnedOff"):
            apply[field] = identifiers.names(kind, apply[field])
    return {
        "accepted": True, "scope": "project", "kind": kind, "effect": EFFECTS[kind],
        "written": [file_label(kind)] if native["written"] else [],
        "apply": apply,
    }


def verified_diff(before, after, changes, native_mismatches):
    """The pin returns display labels, so identify changed rows from keyed snapshots."""
    changed, keyed, labels = [], {}, {}
    for edit in changes:
        key = edit["key"]
        labels.setdefault(before[key]["label"], []).append(key)
        row = after.get(key)
        if row is None:
            keyed[key] = "missing"
            continue
        saved = row["saved"]
        if saved != before[key]["saved"]:
            changed.append({"key": key, "saved": saved})
        if saved != edit["on"]:
            keyed[key] = "saved-value-differs"

    unkeyed = []
    for message in native_mismatches:
        label, sep, _ = message.rpartition(": saved, but ")
        keys = labels.get(label, []) if sep else []
        if len(keys) == 1:
            keyed.setdefault(keys[0], "saved-value-differs")
        elif not any(key in keyed for key in keys):
            unkeyed.append({"key": None, "reason": "native-mismatch"})
    mismatches = [{"key": edit["key"], "reason": keyed[edit["key"]]} for edit in changes if edit["key"] in keyed]
    return changed, mismatches + unkeyed
