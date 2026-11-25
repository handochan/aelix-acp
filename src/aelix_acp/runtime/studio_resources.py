"""Session profile discovery, revision-bound selection and live tool overrides."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import os
from pathlib import Path
import re

from . import profile_reselection as reselection
from .profile_refs import (DisplayTooLarge, file_identity, preview, public_preview, source_label,
                           text_list, valid_name, valid_ref)
from ..wire.errors import BackendError, ProtocolError

MAX_PREVIEWS = 100
UNAVAILABLE = object()
TOO_LARGE = object()
TOOL_LOCK = re.compile(r"Cannot turn on (.+): \1 is (?:locked off for this run: |removed for this run by ).+\Z")


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
        self.recorded = None
        self.previews = OrderedDict()
        self.uncertain = False
        self.starting_binding = None
        self.retired_binding = None
        self.profile_operation = None

    def binding(self):
        relay = self.session.input_relay
        return self.starting_binding or (relay.binding_id if relay is not None else None)

    def retire_approval(self):
        if self.profile_operation is not None:
            self.profile_operation.retire()

    def retire_replaced_approval(self, replacement):
        binding = self.binding()
        if binding is not None and binding != replacement:
            self.retired_binding = binding
            self.retire_approval()

    def mark_uncertain(self):
        self.uncertain = True
        self.invalidate_pages()

    def supports(self, *methods):
        supported = self.session.controls.capabilities.get("methods", [])
        return all(method in supported for method in methods)

    def guarded(self):
        version = self.session.controls.capabilities.get("profileRefGuard")
        return type(version) is int and version == 1 and self.supports("agents/show", "agents/use")

    async def rpc(self, method, *, operation=None, **params):
        kwargs = {"_before_native_write": operation.before_write} if operation is not None else {}
        return await self.session.rpc("aelix_control", method=method, params=params, **kwargs)

    def invalidate_pages(self):
        if relay := self.session.input_relay:
            relay.invalidate_profile_pages()

    def reset(self, preferences):
        self.recorded = reselection.recorded_profile(preferences)
        self.previews.clear()
        self.uncertain = False
        self.invalidate_pages()

    def source_for_diagnostic(self, path, roots):
        if not isinstance(path, str):
            return None
        location = Path(path)
        for scope, root in roots:
            if location.parent == root:
                try:
                    return source_label(scope, location)
                except ValueError:
                    return None
        return None

    async def catalog(self):
        if not self.supports("agents/list"):
            return refusal("unsupported")
        try:
            data = await self.rpc("agents/list")
        except BackendError:
            return refusal("unreadable")
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
            return refusal("unreadable")
        cwd = Path(self.session.cwd)
        agent_dir = Path(os.environ.get("AELIX_CODING_AGENT_DIR") or Path.home() / ".aelix" / "agent").expanduser()
        roots = [("project", cwd / ".aelix" / "agents"), ("user", cwd / agent_dir / "agents")]
        profiles, diagnostics = {}, []
        for row in data["profiles"]:
            name = row.get("name") if isinstance(row, dict) else None
            path = row.get("filePath") if isinstance(row, dict) else None
            if not valid_name(name):
                continue
            # agents/list on the interim pin supplies candidate names only.
            # Take description and revision from the same checked show.
            candidate = await self.inspect(name, revision_bound=self.guarded())
            if not isinstance(candidate, dict):
                diagnostics.append({"code": "invalid-profile", "key": None,
                                    "source": self.source_for_diagnostic(path, roots)})
                continue
            ref = candidate["ref"]
            if isinstance(path, str):
                roots.append((ref["scope"], Path(path).parent))
            profiles[name] = {"ref": ref, "description": candidate["description"]}
        for entry in data.get("diagnostics", []):
            if not isinstance(entry, dict):
                continue
            diagnostics.append({"code": "invalid-profile", "key": None,
                                "source": self.source_for_diagnostic(entry.get("path"), roots)})
        capabilities = self.session.controls.capabilities
        unverified = self.uncertain or (self.profile_operation is not None and self.profile_operation.uncertain_effect)
        if unverified:
            try:
                capabilities = await self.rpc("capabilities")
            except ProtocolError:
                capabilities = {}
        name = capabilities.get("activeAgent")
        active_ref = self.recorded["ref"] if self.recorded and not unverified else None
        return {"profiles": [profiles[key] for key in sorted(profiles, key=lambda value: value.encode("utf-8"))],
                "activeAgent": name if valid_name(name) else None, "activeRef": active_ref,
                "diagnostics": diagnostics, "nextCursor": None}

    async def inspect(self, name, *, revision_bound=False):
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
        candidate = await self.inspect(name, revision_bound=self.guarded())
        if candidate is UNAVAILABLE:
            return refusal("unsupported")
        if candidate is TOO_LARGE:
            return refusal("result-too-large")
        if candidate is None:
            return refusal("profile-unavailable", name=name)
        result = {"profile": public_preview(candidate)}
        if not fits(result):
            return refusal("result-too-large")
        self.remember(candidate)
        return result

    async def select(self, requested, operation):
        previous = self.recorded
        original = previous["ref"] if previous else requested
        self.profile_operation = operation
        try:
            return await self.select_current(requested, previous, original, operation)
        except reselection.Stopped:
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
                return reselection.changed(requested, current, original, candidate, latest)
            return refusal(native["reason"])
        self.recorded = candidate
        self.session.active_tools = None
        await self.session.controls.refresh()
        name = requested["name"] if requested else None
        capabilities = self.session.controls.capabilities
        if "activeAgent" not in capabilities or capabilities["activeAgent"] != name:
            raise internal_error()
        await self.session.save(live=True)
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
        operation = self.profile_operation = reselection.Operation(self)
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
            self.recorded = candidate
            return True
        except reselection.Stopped:
            raise reselection.load_error("cancelled", ref) from None
        finally:
            self.profile_operation = None

    @staticmethod
    def tools_match(result, names):
        active = result.get("activeTools") if isinstance(result, dict) else None
        return (isinstance(result, dict) and result.get("accepted", True) is True
                and text_list(active) and len(active) == len(names) and set(active) == set(names))

    async def set_tools(self, names, fits, operation):
        if not self.supports("resources/list", "tools/set_active"):
            return refusal("unsupported")
        resources = await self.rpc("resources/list")
        available = {tool["name"] for tool in resources["tools"]}
        for name in names:
            if name not in available:
                return refusal("tool-unavailable", key=name)
        if not fits({"accepted": True, "toolOverride": names, "activeTools": names}):
            return refusal("result-too-large")
        try:
            result = await self.rpc("tools/set_active", names=names, operation=operation)
            active = result.get("activeTools")
            if "accepted" in result and type(result["accepted"]) is not bool:
                raise internal_error()
            if result.get("accepted") is False:
                reason, key = result.get("reason"), result.get("key")
                if reason == "write-failed":
                    operation.no_effect = True
                    return refusal(reason)
                if reason in ("tool-locked", "tool-unavailable") and key in names:
                    operation.no_effect = True
                    return refusal(reason, key=key)
                raise internal_error()
            if not self.tools_match(result, names):
                raise internal_error()
            self.session.active_tools = active
            await self.session.save(live=True)
        except BackendError as error:
            key = getattr(error, "studio_tool_lock", None) or tool_lock_key(str(error))
            if key in names:
                operation.no_effect = True
                return refusal("tool-locked", key=key)
            raise
        finally:
            if operation.dispatched:
                self.invalidate_pages()
        return {"accepted": True, "toolOverride": active, "activeTools": active}
