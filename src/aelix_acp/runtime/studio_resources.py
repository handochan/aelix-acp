"""Session profile discovery, revision-bound selection and live tool overrides."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re

from . import profile_reselection as reselection
from . import studio_selections as selections
from .profile_refs import (DisplayTooLarge, catalog_description, file_identity, preview, public_preview, source_label,
                           valid_name, valid_ref)
from .resource_identifiers import ResourceIdentifiers, ordinary_name, public_name, public_text_fields
from ..wire.errors import BackendError, ProtocolError

MAX_PREVIEWS = 100
MAX_CATALOG_ROWS = 4096
MAX_CATALOG_SOURCE_BYTES = 32 * 1024 * 1024
UNAVAILABLE = object()
TOO_LARGE = object()
TOOL_LOCK = re.compile(r"Cannot turn on (.+): \1 is (?:locked off for this run: |removed for this run by ).+\Z")
SELECTION_MUTATIONS = frozenset({"agents/use", "tools/set_active", "resources/reload", "mcp/configure", "settings/set"})


def refusal(reason, **details):
    return {"accepted": False, "reason": reason, **details}


def tool_lock_key(message):
    match = TOOL_LOCK.fullmatch(message) if isinstance(message, str) else None
    return match[1] if match else None


def internal_error():
    return ProtocolError("Request refused.", -32603)


class StudioResources:
    def __init__(self, session):
        self.session = session
        self.selection_lock = asyncio.Lock()
        self.unbound_identifiers = ResourceIdentifiers()
        self.recorded = None
        self.previews = OrderedDict()
        self.uncertain = False
        self.starting_binding = None
        self.retired_binding = None
        self.profile_operation = None
        self.tool_operation = None
        self.selection_operation = None
        self.resource_writes = set()
        self.waiting_operation = None
        self.legacy_operation = None
        self.recovering_operation = None
        self.restore_operation = None

    def binding(self):
        relay = self.session.input_relay
        return self.starting_binding or (relay.binding_id if relay is not None else None)

    @property
    def identifiers(self):
        relay = self.session.input_relay
        return relay.identifiers if relay is not None else self.unbound_identifiers

    def public_inventory(self, data):
        result = dict(data)
        identifiers = self.identifiers
        for field in ("activeTools", "toolOverride"):
            if field in result:
                result[field] = identifiers.names("tool", result[field])
        for kind in ("tool", "skill"):
            field = kind + "s"
            if field in result:
                result[field] = [{**public_text_fields(row, "description"), "name": identifiers.key(kind, row["name"])}
                                 for row in result[field]]
        if "skippedTools" in result:
            result["skippedTools"] = [
                {**public_text_fields(row, "reason"), "tool": identifiers.key("tool", row["tool"])}
                for row in result["skippedTools"]
            ]
        if "extensionErrors" in result:
            result["extensionErrors"] = [public_text_fields(row, "message") for row in result["extensionErrors"]]
        for field in ("commands", "prompts"):
            if field in result:
                result[field] = [public_text_fields(row, "description") for row in result[field]]
        for field in ("mcp", "servers"):
            if field in result:
                result[field] = [public_text_fields(row, "name", "clientName", "description", "reason")
                                 for row in result[field]]
        return result

    def public_native_profile(self, result):
        profile = result.get("profile")
        if not isinstance(profile, dict):
            return result
        identifiers = self.identifiers
        return {**result, "profile": {**public_text_fields(profile, "description", "body"), **{
            field: identifiers.names("tool", profile[field]) for field in ("tools", "exclude_tools") if field in profile
        }}}

    async def legacy_tools(self, arguments, *, timeout, before_write=None):
        names = arguments.get("names")
        if not isinstance(names, list) or not all(map(public_name, names)):
            raise ProtocolError("Expected tool identifiers")
        identifiers = self.identifiers
        if any(not ordinary_name(name) for name in names):
            resources = await self.rpc("resources/list")
            available = identifiers.index("tool", (tool["name"] for tool in resources["tools"]))
            if any(name not in available for name in names):
                raise ProtocolError("Tool identifier is unavailable")
            names = [available[name] for name in names]
        try:
            fence = {"_before_native_write": before_write} if before_write is not None else {}
            return await self.session.rpc("aelix_control", method="tools/set_active",
                                          params={**arguments, "names": names}, _timeout=timeout, **fence)
        except BackendError as error:
            key = getattr(error, "studio_tool_lock", None) or tool_lock_key(str(error))
            data = {"kind": "tool_locked", "key": identifiers.key("tool", key)} if key in names else error.data
            raise ProtocolError("Native tool selection was refused.", error.code, data) from None

    def retire_approval(self, *, keep_recovery=False):
        self.retire_pre_dispatch_writes(keep_recovery=keep_recovery)
        operation = (self.profile_operation or self.tool_operation or self.selection_operation
                     or self.legacy_operation or self.restore_operation)
        if operation is not None:
            operation.retire()

    def retire_pre_dispatch_writes(self, *, keep_recovery=False):
        for operation in self.resource_writes:
            if not operation.dispatched and not (keep_recovery and operation is self.recovering_operation):
                operation.retire()

    @asynccontextmanager
    async def track_write(self, operation):
        self.resource_writes.add(operation)
        try:
            async with operation.cancel_on_retirement():
                yield
        finally:
            self.resource_writes.discard(operation)

    @asynccontextmanager
    async def slash_write(self):
        operation = reselection.Operation(self)
        async with self.track_write(operation), self.selection_lock:
            operation.require_current()
            yield operation.before_write

    @asynccontextmanager
    async def selection_write(self, operation):
        # Wait before reserving session admission. Stop, close and replacement
        # can still retire this operation while the outstanding read owns the lock.
        async with self.track_write(operation):
            self.waiting_operation = operation
            try:
                await self.selection_lock.acquire()
            finally:
                self.waiting_operation = None
            try:
                # A historical Stop does not prevent a new idle write.
                session = self.session
                if session.server.sessions.get(session.id) is not session:
                    raise reselection.Stopped()
                if session.compaction_recovery_needed:
                    operation.require_admission(allow_cancelled=True)
                else:
                    operation.require_current(allow_cancelled=True)
                yield
            finally:
                self.selection_lock.release()

    async def recover_selection_write(self, operation):
        session = self.session
        if not session.compaction_recovery_needed:
            return False
        operation.require_admission(allow_cancelled=True)
        # The request is now admitted. Clear only historical cancellation
        # and keep this generation through the replacement worker's startup.
        session.reset_cancellation()
        operation.generation = session.cancel_generation
        previews = self.previews.copy()
        self.recovering_operation = operation
        try:
            await session.recover_after_compaction(preserve_cancellation=True)
        finally:
            self.recovering_operation = None
        operation.require_admission()
        operation.worker = session.proc
        operation.require_current()
        # A checked preview remains scoped to this binding. Explicit use
        # still resolves its revision again on the replacement worker.
        self.previews.update(previews)
        return True

    def retire_replaced_approval(self, replacement):
        binding = self.binding()
        if binding is not None and binding != replacement:
            self.retired_binding = binding
            self.invalidate_pages()
            self.retire_approval()

    def mark_uncertain(self):
        self.uncertain = True
        self.invalidate_pages()

    def supports(self, *methods):
        supported = self.session.controls.capabilities.get("methods", [])
        return all(method in supported for method in methods)

    def supports_selection(self, kind, *methods):
        kinds = self.session.controls.capabilities.get("selectionKinds")
        return isinstance(kinds, list) and kind in kinds and self.supports(*methods)

    def guarded(self):
        version = self.session.controls.capabilities.get("profileRefGuard")
        return type(version) is int and version == 1 and self.supports("agents/show", "agents/use")

    async def rpc(self, method, *, operation=None, **params):
        if method == selections.LIST and self.session.stop_pending():
            raise reselection.Stopped()
        kwargs = {"_before_native_write": operation.before_write} if operation is not None else {}
        return await self.session.rpc("aelix_control", method=method, params=params, **kwargs)

    def invalidate_pages(self):
        if relay := self.session.input_relay:
            relay.invalidate_list_pages()

    def selection_rows(self, kind, data):
        return selections.check_snapshot(kind, data, self.identifiers)

    async def selections_catalog(self, kind):
        if not self.supports_selection(kind, selections.LIST):
            return refusal("unsupported")
        if self.session.changing or self.session.rpc_closed:
            return refusal("unreadable")
        relay = self.session.input_relay
        generation = relay.list_generation if relay is not None else None

        def require_snapshot_current():
            if (self.session.input_relay is not relay
                    or (relay is not None and (relay.list_generation != generation
                                               or self.retired_binding == relay.binding_id))):
                raise reselection.Stopped()

        try:
            data = await self.rpc(selections.LIST, kind=kind)
            rows = self.selection_rows(kind, data)
            scopes = data["scopes"]
            target = await self.session.server.selection_target_io.run(
                selections.project_target, self.session, kind, scopes, before_start=require_snapshot_current)
            public_scopes = {scope: selections.exposed_scope(kind, scope, scopes[scope], target is not None)
                             for scope in ("global", "project")}
            pending = data["policyPending"]
            if type(pending) is not bool:
                raise ValueError("Invalid selection policy status")
            items = [selections.public_row(kind, key, rows[key])
                     for key in sorted(rows, key=lambda key: key.encode("utf-8"))]
        except (BackendError, reselection.Stopped, selections.TargetProbeBusy, KeyError, TypeError, ValueError):
            return refusal("unreadable")
        identifiers = self.identifiers
        return {"kind": kind, "effect": selections.EFFECTS[kind], "scopes": public_scopes, "items": items,
                "policyPending": pending, "diagnostics": [
                    {"code": "native-problem", "key": None, "source": None} for _ in data["problems"]],
                "toolOverride": identifiers.names("tool", self.session.active_tools) if kind == "tool" else None,
                "activeTools": [row["key"] for row in items if row["live"]] if kind == "tool" else None}

    async def set_selections(self, value, fits, operation):
        kind, changes = value["kind"], value["changes"]
        if not self.supports_selection(kind, selections.LIST, selections.SET):
            return refusal("unsupported")
        self.selection_operation = operation
        self.invalidate_pages()
        try:
            return await self._set_selections(kind, changes, fits, operation)
        except selections.TargetProbeBusy:
            if operation.dispatched:
                raise
            return refusal("busy")
        finally:
            if operation.permission is not None:
                operation.permission.cancel()
            self.selection_operation = None
            self.invalidate_pages()

    async def _set_selections(self, kind, changes, fits, operation):
        target_io = self.session.server.selection_target_io
        try:
            data = await self.rpc(selections.LIST, kind=kind)
            rows = self.selection_rows(kind, data)
            scopes = data["scopes"]
            target = await target_io.run(selections.project_target, self.session, kind, scopes, operation=operation)
            project = selections.exposed_scope(kind, "project", scopes["project"], target is not None)
            for edit in changes:
                key = edit["key"]
                row = rows.get(key)
                if row is None:
                    return refusal("selection-missing", scope="project", written=[],
                                   mismatches=[{"key": key, "reason": "missing"}])
                if row["lock"] is not None or row["role"] != "item" or row["refusal"] is not None:
                    return refusal("selection-locked", scope="project", written=[],
                                   mismatches=[{"key": key, "reason": "locked"}])
        except (BackendError, KeyError, TypeError, ValueError):
            return refusal("unreadable")
        operation.require_current()
        if not project["writable"] or project["exists"] != target.exists:
            return refusal("scope-readonly", scope="project", written=[])
        if not project["exists"] and (not operation.may_confirm_creation() or not self.supports("ui/enable")):
            return refusal("consent-required", cause="unanswered", scope="project", written=[])
        operation.target, operation.kind = target, kind
        operation.native_keys = {edit["key"]: rows[edit["key"]]["key"] for edit in changes}
        native_changes = [{"key": operation.native_keys[edit["key"]], "on": edit["on"]} for edit in changes]
        try:
            # Keep the last filesystem check under the mutation and selection
            # locks. The synchronous dispatch hook still fences Stop/close
            # after this thread returns and immediately before the pipe write.
            await target_io.run(target.require_version, target.before, operation=operation)
            native = await self.session.rpc(
                "aelix_control", method=selections.SET,
                params={"scope": "project", "kind": kind, "changes": native_changes},
                _before_native_write=operation.before_write, _selection_write=operation, _timeout=90,
            )
        except selections.UnsafeTarget:
            if operation.dispatched:
                raise
            return refusal("scope-readonly", scope="project", written=[])
        except BackendError as error:
            reason = getattr(error, "studio_selection_refusal", None)
            if reason is None or (reason == "cancelled" and operation.confirmation_sent):
                raise
            if reason == "selection-missing":
                # Native validates row keys before entering the save path.
                # The edit that removed the row may itself have replaced mcp.json.
                operation.no_effect = True
                result = await self.missing_selections(kind, changes, getattr(error, "studio_selection_key", None))
                return result if fits(result) else refusal("result-too-large")
            await target_io.run(target.require_version, target.before)
            operation.no_effect = True
            if reason == "cancelled":
                if not project["exists"] or operation.confirm_id is not None:
                    return refusal("consent-required", cause="cancelled", scope="project", written=[])
                return refusal("cancelled")
            if reason == "consent-required":
                return refusal(reason, cause=operation.consent_cause or "unanswered",
                               scope="project", written=[])
            if reason == "scope-readonly":
                return refusal(reason, scope="project", written=[])
            return refusal(reason)
        operation.require_current()
        result = selections.project_readback(kind, native, target.path, self.identifiers)
        version = await target_io.run(target.verify_save, result["written"])
        operation.require_current()
        if kind == "tool" and self.session.active_tools is not None:
            if result["apply"]["deferred"]:
                # A native deferred apply can still turn tools back on after
                # a repair RPC. Reopen with the saved override before prompting.
                self.mark_uncertain()
            else:
                await self.session.restore_tools(operation=operation)
                operation.require_current()
        after = self.selection_rows(kind, await self.rpc(selections.LIST, kind=kind))
        operation.require_current()
        await target_io.run(target.require_version, version)
        operation.require_current()
        changed, mismatches = selections.verified_diff(rows, after, changes, native["mismatches"])
        if any(mismatch["reason"] == "missing" for mismatch in mismatches):
            result = refusal("selection-missing", scope="project", written=result["written"], mismatches=mismatches)
        else:
            result.update(changed=changed, mismatches=mismatches)
        if not fits(result):
            raise internal_error()
        return result

    async def missing_selections(self, kind, changes, hint):
        # The pin gives just the first missing row, in a truncated display
        # message. Read all requested keys and include an unambiguous hint
        # even if that row has already reappeared.
        try:
            after = self.selection_rows(kind, await self.rpc(selections.LIST, kind=kind))
        except (ProtocolError, OSError, KeyError, TypeError, ValueError):
            after = None
        return refusal("selection-missing", scope="project", written=[], mismatches=[
            {"key": edit["key"], "reason": "missing"} for edit in changes
            if edit["key"] == hint or (after is not None and edit["key"] not in after)
        ])

    def reset(self, preferences):
        self.recorded = reselection.recorded_profile(preferences)
        self.previews.clear()
        self.restore_operation = None
        self.uncertain = False
        self.invalidate_pages()

    @staticmethod
    def source_for_diagnostic(path, roots):
        if isinstance(path, str):
            location = Path(path)
            if scope := roots.get(location.parent):
                try:
                    return source_label(scope, location)
                except ValueError:
                    pass
        return None

    def catalog_rows(self, rows, native_diagnostics, guarded, previews):
        cwd = Path(self.session.cwd)
        agent_dir = Path(os.environ.get("AELIX_CODING_AGENT_DIR") or Path.home() / ".aelix" / "agent").expanduser()
        roots = {cwd / ".aelix" / "agents": "project", cwd / agent_dir / "agents": "user"}
        profiles, diagnostics = {}, []
        remaining = MAX_CATALOG_SOURCE_BYTES
        for row in rows:
            path = row.get("filePath") if isinstance(row, dict) else None
            try:
                name, scope = row["name"], row["scope"]
                if not valid_name(name):
                    raise ValueError("Invalid native name")
                if guarded:
                    # Guarded discovery supplies metadata and its ref in one parse.
                    ref = row.get("ref")
                    if not valid_ref(ref) or ref["name"] != name or ref["scope"] != scope:
                        raise ValueError("Unbound native row")
                else:
                    # The interim pin supplies metadata and paths in one RPC.
                    # The file hash is provisional until a checked Studio show.
                    ref, _, stamp = file_identity(row, max_bytes=remaining)
                    remaining -= stamp[-1]
                checked = previews.get(ref["revision"])
                description = (checked["description"] if checked and checked["ref"] == ref
                               else catalog_description(row["description"]))
            except DisplayTooLarge:
                raise
            except (OSError, KeyError, TypeError, ValueError, RuntimeError):
                diagnostics.append({"code": "invalid-profile", "key": None,
                                    "source": self.source_for_diagnostic(path, roots)})
                continue
            if isinstance(path, str):
                roots.setdefault(Path(path).parent, scope)
            profiles[name] = {"ref": ref, "description": description}
        for entry in native_diagnostics:
            if isinstance(entry, dict):
                diagnostics.append({"code": "invalid-profile", "key": None,
                                    "source": self.source_for_diagnostic(entry.get("path"), roots)})
        return sorted(profiles.values(), key=lambda row: row["ref"]["name"].encode("utf-8")), diagnostics

    async def catalog(self):
        if not self.supports("agents/list"):
            return refusal("unsupported")
        try:
            data = await self.rpc("agents/list")
        except BackendError:
            return refusal("unreadable")
        if not isinstance(data, dict) or not isinstance(rows := data.get("profiles"), list):
            return refusal("unreadable")
        native_diagnostics = data.get("diagnostics", [])
        if not isinstance(native_diagnostics, list):
            return refusal("unreadable")
        if len(rows) + len(native_diagnostics) > MAX_CATALOG_ROWS:
            return refusal("result-too-large")
        try:
            profiles, diagnostics = await asyncio.to_thread(
                self.catalog_rows, rows, native_diagnostics, self.guarded(), dict(self.previews)
            )
        except DisplayTooLarge:
            return refusal("result-too-large")
        capabilities = self.session.controls.capabilities
        unverified = self.uncertain or (self.profile_operation is not None and self.profile_operation.uncertain_effect)
        if unverified:
            try:
                capabilities = await self.rpc("capabilities")
            except ProtocolError:
                capabilities = {}
        name = capabilities.get("activeAgent")
        active_ref = self.recorded["ref"] if self.recorded and not unverified else None
        return {"profiles": profiles,
                "activeAgent": name if valid_name(name) else None, "activeRef": active_ref,
                "diagnostics": diagnostics, "nextCursor": None}

    async def inspect(self, name, *, revision_bound=False, for_display=False):
        if not self.supports("agents/show"):
            return UNAVAILABLE
        try:
            native = (await self.rpc("agents/show", name=name))["profile"]
            ref, source, stamp = await asyncio.to_thread(file_identity, native)
            if ref["name"] != name:
                return None
            if revision_bound or "ref" in native:
                # The card must describe the exact bytes a guarded apply can
                # commit. Bracketing a parse with file reads cannot prove that
                # association if a file changes away and back.
                if not valid_ref(native.get("ref")):
                    return UNAVAILABLE
                if native["ref"] != ref:
                    return None
            else:
                confirmed = (await self.rpc("agents/show", name=name))["profile"]
                if native != confirmed or (ref, source, stamp) != await asyncio.to_thread(file_identity, confirmed):
                    return None
            result = preview(native, ref, source)
            if for_display:
                identifiers = self.identifiers
                result["displayTools"] = identifiers.names("tool", native["tools"])
                result["displayExcludeTools"] = identifiers.names("tool", native["exclude_tools"])
        except DisplayTooLarge:
            return TOO_LARGE
        except (BackendError, OSError, KeyError, TypeError, ValueError, RuntimeError):
            return None
        return result

    def remember(self, candidate):
        revision = candidate["ref"]["revision"]
        self.previews[revision] = candidate
        self.previews.move_to_end(revision)
        if len(self.previews) > MAX_PREVIEWS:
            self.previews.popitem(last=False)

    async def show(self, name, fits):
        candidate = await self.inspect(name, revision_bound=self.guarded(), for_display=True)
        if candidate is UNAVAILABLE:
            return refusal("unsupported")
        if candidate is TOO_LARGE:
            return refusal("result-too-large")
        if candidate is None:
            return refusal("profile-unavailable", name=name)
        profile = public_preview(candidate)
        if "displayTools" in candidate:
            profile["tools"] = candidate["displayTools"]
            profile["policy"]["excludeTools"] = candidate["displayExcludeTools"]
        result = {"profile": profile}
        if not fits(result):
            return refusal("result-too-large")
        self.remember(candidate)
        return result

    async def select(self, requested, operation):
        previous = self.recorded
        original = previous["ref"] if previous else None
        self.profile_operation = operation
        try:
            return await self.select_current(requested, previous, original, operation)
        except reselection.Stopped:
            if operation.uncertain_effect:
                raise
            return refusal("consent-required", cause="cancelled", recovery=reselection.recovery(original))
        finally:
            self.profile_operation = None

    async def select_current(self, requested, previous, original, operation):
        if not self.supports("agents/use"):
            return refusal("unsupported")
        operation.require_current()
        candidate = None
        if requested is not None:
            candidate = await self.inspect(requested["name"], revision_bound=self.guarded())
            operation.require_current()
            if candidate is UNAVAILABLE:
                return refusal("unsupported")
            if candidate is TOO_LARGE:
                return refusal("profile-refused")
            if candidate is None:
                return refusal("profile-unavailable", name=requested["name"])
            if candidate["ref"] != requested:
                before = previous if original == requested and previous else self.previews.get(requested["revision"])
                if before and before["ref"] != requested:
                    before = None
                return reselection.changed(requested, candidate["ref"], original, before, candidate)
            shown = self.previews.get(requested["revision"])
            if not self.guarded() and (shown is None or shown["ref"] != requested):
                # The pinned discovery RPC has no revision bound to its parsed
                # metadata. Require a checked preview before an interim pick.
                return refusal("profile-refused")
            if previous and original["name"] == requested["name"] and original != requested:
                reason = await reselection.approve(operation, previous, candidate)
                if reason:
                    return refusal("consent-required", cause=reason, recovery=reselection.recovery(original))
        native = await self.apply(candidate, operation)
        if native.get("accepted") is False:
            if native["reason"] == "profile-changed" and requested:
                self.invalidate_pages()
                current = native["currentRef"]
                latest = await self.inspect(current["name"], revision_bound=self.guarded()) if current else None
                if not isinstance(latest, dict) or latest["ref"] != current:
                    latest = None
                if operation.uncertain_effect:
                    operation.require_current()
                return reselection.changed(requested, current, original, candidate, latest)
            return refusal(native["reason"])
        self.recorded = candidate
        self.session.active_tools = None
        await self.session.controls.refresh()
        name = requested["name"] if requested else None
        capabilities = self.session.controls.capabilities
        if "activeAgent" not in capabilities or capabilities["activeAgent"] != name:
            raise internal_error()
        operation.require_current()
        await self.session.save(live=True)
        operation.require_current()
        self.invalidate_pages()
        return {"accepted": True, "activeAgent": name, "activeRef": requested, "toolOverride": None}

    async def publish(self):
        # The durable outcome stands even when an optional UI update can't be delivered.
        try:
            await self.session.commands()
            await self.session.update({"sessionUpdate": "config_option_update",
                                       "configOptions": self.session.controls.options()})
        except (ProtocolError, OSError):
            pass

    async def apply(self, candidate, operation):
        guarded = candidate is not None and self.guarded()
        name = candidate["ref"]["name"] if candidate else "--none"
        params = {"name": name}
        if guarded:
            params["expectedProfile"] = candidate["ref"]
        elif candidate is not None:
            before = await self.inspect(name)
            operation.require_current()
            observed = before["ref"] if isinstance(before, dict) else None
            if observed != candidate["ref"]:
                return self.changed_on_apply(observed)
        operation.require_current()
        result = await self.rpc("agents/use", operation=operation, **params)
        if not isinstance(result, dict) or ("accepted" in result and type(result["accepted"]) is not bool):
            raise internal_error()
        if result.get("accepted") is False:
            reason = result.get("reason")
            if guarded and reason == "profile-changed" and "currentRef" in result:
                current = result["currentRef"]
                if current is None or (valid_ref(current) and current["name"] == name
                                       and current != candidate["ref"]):
                    operation.no_effect = True
                    return result
            if reason in ("profile-refused", "write-failed"):
                operation.no_effect = True
                return result
            raise internal_error()
        operation.require_current()
        if guarded:
            if result.get("activeRef") != candidate["ref"]:
                raise internal_error()
        elif not isinstance(result.get("message"), str):
            raise internal_error()
        elif candidate is not None:
            active = await self.rpc("capabilities")
            if active.get("activeAgent") != name:
                raise internal_error()
            after = await self.inspect(name)
            operation.require_current()
            if not isinstance(after, dict):
                raise internal_error()
            observed = after["ref"]
            if observed != candidate["ref"]:
                # A name apply may already have changed the worker. Keep the
                # original preferences and fence prompts until the session reopens.
                self.mark_uncertain()
                return self.changed_on_apply(observed)
        elif result.get("activeRef") is not None:
            raise internal_error()
        return result

    @staticmethod
    def changed_on_apply(current):
        return refusal("profile-changed", currentRef=current)

    def preferences(self):
        if self.recorded is None:
            return {}
        ref = self.recorded["ref"]
        return {"agent": ref["name"], "agentRef": ref, "agentPreview": self.recorded}

    async def restore(self):
        previous = self.recorded
        ref = previous["ref"]
        operation = self.profile_operation = reselection.Operation(self, original=ref)
        try:
            operation.require_current()
            candidate = await self.inspect(ref["name"], revision_bound=self.guarded())
            operation.require_current()
            if candidate is UNAVAILABLE:
                raise reselection.load_error("guard-unavailable", ref)
            if candidate is TOO_LARGE:
                raise reselection.load_error("unanswered", ref)
            if candidate is None:
                return False
            if candidate["ref"] != ref:
                reason = await reselection.approve(operation, previous, candidate)
                if reason:
                    raise reselection.load_error(reason, ref)
            try:
                native = await self.apply(candidate, operation)
            except (ProtocolError, OSError) as error:
                if isinstance(error, reselection.Stopped):
                    raise
                raise reselection.load_error("outcome-unknown" if operation.uncertain_effect else "unanswered", ref) from None
            if native.get("accepted") is False:
                if native["reason"] == "profile-changed":
                    raise reselection.load_error("changed-again", ref)
                return False
            operation.require_current()
            self.recorded = candidate
            self.restore_operation = operation
            return True
        except reselection.Stopped:
            raise reselection.load_error("outcome-unknown" if operation.uncertain_effect else "cancelled", ref) from None
        finally:
            self.profile_operation = None

    @staticmethod
    def tools_match(result, names):
        active = result.get("activeTools") if isinstance(result, dict) else None
        return (isinstance(result, dict) and result.get("accepted", True) is True
                and isinstance(active, list) and all(isinstance(name, str) for name in active)
                and len(active) == len(names) and set(active) == set(names))

    async def set_tools(self, names, fits, operation):
        if not self.supports("resources/list", "tools/set_active"):
            return refusal("unsupported")
        self.tool_operation = operation
        identifiers = self.identifiers
        try:
            resources = await self.rpc("resources/list")
            operation.require_current()
            available = identifiers.index("tool", (tool["name"] for tool in resources["tools"]))
            for name in names:
                if name not in available:
                    return refusal("tool-unavailable", key=name)
            if not fits({"accepted": True, "toolOverride": names, "activeTools": names}):
                return refusal("result-too-large")
            native_names = [available[name] for name in names]
            result = await self.rpc("tools/set_active", names=native_names, operation=operation)
            active = result.get("activeTools")
            if "accepted" in result and type(result["accepted"]) is not bool:
                raise internal_error()
            if result.get("accepted") is False:
                reason, key = result.get("reason"), result.get("key")
                if reason == "write-failed":
                    operation.no_effect = True
                    return refusal(reason)
                if reason in ("tool-locked", "tool-unavailable") and key in native_names:
                    operation.no_effect = True
                    return refusal(reason, key=identifiers.key("tool", key))
                raise internal_error()
            if not self.tools_match(result, native_names):
                raise internal_error()
            operation.require_current()
            self.session.active_tools = active
            await self.session.save(live=True)
            operation.require_current()
        except BackendError as error:
            if not operation.dispatched:
                raise
            key = getattr(error, "studio_tool_lock", None) or tool_lock_key(str(error))
            if key in native_names:
                operation.no_effect = True
                return refusal("tool-locked", key=identifiers.key("tool", key))
            raise
        finally:
            self.tool_operation = None
            if operation.dispatched:
                self.invalidate_pages()
        public = identifiers.names("tool", active)
        return {"accepted": True, "toolOverride": public, "activeTools": public}
