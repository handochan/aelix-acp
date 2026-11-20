"""Studio's launch snapshot and ACP's per-session extension overrides."""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib

from .extensions import require_consent, run_management_task, same_extension
from ..wire.errors import ProtocolError


META = "aelix/extensionOverrides"
LIST = "extensions/overrides/list"
SET = "extensions/overrides/set"
VERSION = 1
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,255}")
MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", re.ASCII)
MAX_INPUT = 1024 * 1024


def valid_id(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _module_reference(value):
    return (
        isinstance(value, str) and MODULE.fullmatch(value) is not None
        and (os.name != "nt" or value[1] != ":" or not value[0].isalpha())
    )


def managed_preferences(preferences):
    overrides = preferences.get("extensionOverrides", {})
    if not isinstance(overrides, dict) or any(
        not valid_id(key) or value not in ("on", "off") for key, value in overrides.items()
    ):
        raise ProtocolError("Invalid saved managed extension overrides")
    return overrides


def _overlap(first, second):
    if same_extension(first, second):
        return True
    if Path(first).is_absolute() and Path(second).is_absolute():
        left, right = Path(first).resolve(), Path(second).resolve()
        return left.is_relative_to(right) or right.is_relative_to(left)
    return False


@dataclass(frozen=True)
class ManagedExtension:
    id: str
    reference: str
    default_enabled: bool
    entry_points: tuple[str, ...]
    package: str | None = None
    plugin_id: str | None = None

    def enabled(self, overrides):
        return overrides.get(self.id, "on" if self.default_enabled else "off") == "on"

    def matches(self, reference):
        return (
            reference == self.plugin_id or same_extension(self.reference, reference)
            or reference in self.entry_points or reference + ":setup" in self.entry_points
            or (self.package is not None and same_extension(self.package, reference))
        )

    def overlaps(self, reference):
        return (
            self.matches(reference) or _overlap(self.reference, reference)
            or (self.package is not None and _overlap(self.package, reference))
        )

    def matches_pack(self, item):
        path = item.get("path")
        return isinstance(path, str) and same_extension(self.package, path)

    def state(self, resources, selections, enabled):
        registered = any(
            row.get("name") == (self.plugin_id or self.reference)
            or (self.package is not None and isinstance(row.get("source"), str)
                and same_extension(self.package, row["source"]))
            for row in resources["extensions"]
        )
        if not enabled:
            return ("error", "unexpectedly_loaded") if registered else ("disabled", None)
        if any(isinstance(row.get("source"), str) and self.overlaps(row["source"])
               for row in resources["extensionErrors"]):
            return "error", "load_failed"
        if not registered:
            return "error", "not_registered"
        if self.package is None:
            return "active", None
        matches = [row for row in selections if self.matches_pack(row)]
        if len(matches) != 1:
            return "error", "activation_unverified"
        if matches[0].get("problem"):
            return "error", "activation_failed"
        # At the pinned native revision this suffix is emitted only while the
        # pack's on_command activation is pending, even though it is registered.
        if (matches[0].get("description") or "").endswith(" · loads at its first command"):
            return "deferred", None
        return "active", None


@dataclass(frozen=True)
class ManagedChange:
    extension_id: str
    overrides: dict[str, str]


@dataclass(frozen=True)
class ManagedExtensions:
    generation: str
    entries: tuple[ManagedExtension, ...]

    def alias(self, reference):
        matches = [item for item in self.entries if item.matches(reference)]
        if len(matches) > 1:
            raise ProtocolError("Ambiguous managed extension reference", -32602,
                                {"kind": "managed_extension_ambiguous"})
        return matches[0] if matches else None

    def target(self, reference):
        by_id = next((item for item in self.entries if reference == item.id), None)
        return by_id or self.alias(reference)

    def check_external(self, references):
        for reference in references:
            if any(item.overlaps(reference) for item in self.entries):
                raise ProtocolError(
                    "A managed extension also has an explicit load reference", -32004,
                    {"kind": "managed_extension_conflict"},
                )

    def migrate_session_references(self, references, overrides):
        remaining = []
        overrides = dict(overrides)
        for reference in references:
            entry = self.alias(reference)
            if entry is None:
                self.check_external([reference])
                remaining.append(reference)
            else:
                overrides.setdefault(entry.id, "on")
        return remaining, overrides

    def arguments(self, overrides):
        return [item.reference for item in self.entries if item.enabled(overrides)]

    async def status(self, session):
        has_packs = any(item.package and item.enabled(session.extension_overrides) for item in self.entries)
        resources = await session.rpc("aelix_control", method="resources/list", params={}) if self.entries else {}
        selections = []
        if has_packs and "extension" in session.controls.capabilities.get("selectionKinds", []):
            result = await session.rpc("aelix_control", method="selections/list", params={"kind": "extension"})
            selections = result["items"]
        rows = []
        overrides = session.extension_overrides
        installed = {item.id for item in self.entries}
        for item in self.entries:
            enabled = item.enabled(overrides)
            state, reason = item.state(resources, selections, enabled)
            rows.append({"extensionId": item.id, "defaultEnabled": item.default_enabled,
                         "override": overrides.get(item.id, "inherit"), "desiredEnabled": enabled,
                         "state": state, "reason": reason})
        for extension_id in sorted(overrides.keys() - installed):
            rows.append({"extensionId": extension_id, "defaultEnabled": None, "override": overrides[extension_id],
                         "desiredEnabled": False, "state": "unavailable", "reason": "not_installed"})
        return {"generation": self.generation, "extensions": rows}

    async def verify_start(self, session, change):
        snapshot = await self.status(session)
        for row in snapshot["extensions"]:
            if row["reason"] == "unexpectedly_loaded" or (
                change is not None and row["extensionId"] == change.extension_id and row["state"] == "error"
            ):
                raise ProtocolError(
                    "The worker did not apply the managed extension selection", -32004,
                    {"kind": "managed_extension_not_applied", "extensionId": row["extensionId"],
                     "reason": row["reason"]},
                )
        return snapshot

    async def set(self, server, session, extension_id, value):
        operation = self._set(server, session, extension_id, value)
        if session.management_task is not None:
            return await operation  # Slash commands already have a management task.
        return await run_management_task(session, operation)

    async def _set(self, server, session, extension_id, value):
        if not valid_id(extension_id) or value not in ("inherit", "on", "off"):
            raise ProtocolError("Expected extensionId and inherit, on, or off")
        entry = next((item for item in self.entries if item.id == extension_id), None)
        old = session.extension_overrides
        if entry is None and (value != "inherit" or extension_id not in old):
            raise ProtocolError("Managed extension is unavailable", -32004,
                                {"kind": "managed_extension_unavailable", "extensionId": extension_id})
        override = old.get(extension_id, "inherit")
        if override == value:
            return await self.status(session)
        await require_consent(server, session, "Change managed extension",
                              {"extensionId": extension_id, "value": value})
        overrides = dict(old)
        if value == "inherit":
            overrides.pop(extension_id, None)
        else:
            overrides[extension_id] = value
        return await session.apply_extension_override(ManagedChange(extension_id, overrides))


def _read_entries(value):
    if not isinstance(value, list):
        raise ValueError("extensions must be an array")
    entries = []
    ids, plugins = set(), set()
    for item in value:
        common = {"id", "defaultEnabled", "reference"}
        if not isinstance(item, dict) or set(item) not in (common, common | {"entryPoints"}):
            raise ValueError("invalid managed extension entry")
        extension_id, default_enabled, reference = (item[key] for key in ("id", "defaultEnabled", "reference"))
        if not valid_id(extension_id) or extension_id in ids or type(default_enabled) is not bool:
            raise ValueError("invalid or duplicate managed extension ID or default")
        if not isinstance(reference, str) or not reference or "\x00" in reference:
            raise ValueError("invalid managed extension reference")
        if "entryPoints" in item:
            entry_points = item["entryPoints"]
            if not isinstance(entry_points, list) or not entry_points or not all(map(_module_reference, entry_points)):
                raise ValueError("pack entryPoints must be explicit module references")
            wrapper = Path(reference)
            if not wrapper.is_absolute():
                raise ValueError("pack reference must be an absolute wrapper directory")
            wrapper = wrapper.resolve(strict=True)
            children = list(wrapper.iterdir())
            if len(children) != 1 or not children[0].is_dir():
                raise ValueError("pack wrapper must contain exactly one package directory")
            package = children[0].resolve(strict=True)
            manifest = tomllib.loads((package / "aelix-plugin.toml").read_text(encoding="utf-8"))
            plugin = manifest.get("plugin", {})
            plugin_id = plugin.get("id") if isinstance(plugin, dict) else None
            if not valid_id(plugin_id) or plugin_id in plugins:
                raise ValueError("invalid or duplicate managed plugin ID")
            plugins.add(plugin_id)
            factory = plugin.get("entry")
            factory = factory.get("python") if isinstance(factory, dict) else None
            if _module_reference(factory) and factory not in entry_points:
                entry_points = [*entry_points, factory]
            entry = ManagedExtension(extension_id, str(wrapper), default_enabled, tuple(entry_points),
                                     str(package), plugin_id)
        else:
            if not _module_reference(reference):
                raise ValueError("module reference must have the form module:factory")
            entry = ManagedExtension(extension_id, reference, default_enabled, (reference,))
        if any(existing.overlaps(entry.reference) or entry.overlaps(existing.reference)
               or set(entry.entry_points).intersection(existing.entry_points) for existing in entries):
            raise ValueError("overlapping managed extension load references")
        ids.add(extension_id)
        entries.append(entry)
    return tuple(entries)


def read_launch(path):
    """Read one launcher-owned snapshot at process start, before accepting ACP."""
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON key")
        return result

    try:
        file = Path(path)
        if not file.is_absolute() or not file.is_file():
            raise ValueError("expected an absolute path to a snapshot file")
        with file.open("rb") as stream:
            raw = stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("snapshot exceeds 1 MiB")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(value, dict) or set(value) != {"version", "generation", "extensions"}:
            raise ValueError("expected version, generation, and extensions")
        if type(value["version"]) is not int or value["version"] != VERSION or not valid_id(value["generation"]):
            raise ValueError("unsupported version or invalid generation")
        return ManagedExtensions(value["generation"], _read_entries(value["extensions"]))
    except (ValueError, OSError, RuntimeError) as error:
        raise argparse.ArgumentTypeError(f"invalid managed extensions snapshot: {error}") from None
