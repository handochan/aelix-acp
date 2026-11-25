"""Session profile discovery, revision-bound selection and live tool overrides."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import os
from pathlib import Path
import re

from . import profile_reselection as reselection
from .profile_refs import (display_text, file_identity, preview, public_preview, source_label,
                           text_list, valid_name, valid_ref)
from ..wire.errors import BackendError, ProtocolError

MAX_PREVIEWS = 100
UNAVAILABLE = object()
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

    def supports(self, *methods):
        supported = self.session.controls.capabilities.get("methods", [])
        return all(method in supported for method in methods)

    def guarded(self):
        version = self.session.controls.capabilities.get("profileRefGuard")
        return type(version) is int and version == 1 and self.supports("agents/show", "agents/use")

    async def rpc(self, method, **params):
        return await self.session.rpc("aelix_control", method=method, params=params)

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
            try:
                ref, source, _ = await asyncio.to_thread(file_identity, row)
                description = display_text(row["description"])
                roots.append((ref["scope"], Path(row["filePath"]).parent))
            except (OSError, KeyError, TypeError, ValueError, RuntimeError):
                path = row.get("filePath") if isinstance(row, dict) else None
                diagnostics.append({"code": "invalid-profile", "key": None,
                                    "source": self.source_for_diagnostic(path, roots)})
                continue
            profiles[ref["name"]] = {"ref": ref, "description": description}
        for entry in data.get("diagnostics", []):
            if not isinstance(entry, dict):
                continue
            diagnostics.append({"code": "invalid-profile", "key": None,
                                "source": self.source_for_diagnostic(entry.get("path"), roots)})
        name = self.session.controls.capabilities.get("activeAgent")
        return {"profiles": [profiles[key] for key in sorted(profiles, key=lambda value: value.encode("utf-8"))],
                "activeAgent": name,
                "activeRef": self.recorded["ref"] if self.recorded else None,
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
        if candidate is None:
            return refusal("profile-unavailable", name=name)
        result = {"profile": public_preview(candidate)}
        if not fits(result):
            return refusal("result-too-large")
        self.remember(candidate)
        return result

    async def select_name(self, name):
        if name != "--none" and not valid_name(name):
            raise ProtocolError("Invalid profile name")
        if name == "--none":
            result, native = await self.select(None)
        elif not self.guarded():
            result, native = refusal("unsupported"), None
        else:
            candidate = await self.inspect(name, revision_bound=True)
            if candidate is UNAVAILABLE:
                result, native = refusal("unsupported"), None
            elif candidate is None:
                result, native = refusal("profile-unavailable", name=name), None
            else:
                result, native = await self.select(candidate["ref"])
        if not result["accepted"]:
            raise ProtocolError("Profile selection was refused.", -32004, result)
        await self.publish()
        return native

    async def select(self, requested):
        if not self.supports("agents/use") or (requested is not None and not self.guarded()):
            return refusal("unsupported"), None
        previous = self.recorded
        original = previous["ref"] if previous else requested
        candidate = None
        if requested is not None:
            candidate = await self.inspect(requested["name"], revision_bound=True)
            if candidate is UNAVAILABLE:
                return refusal("unsupported"), None
            if candidate is None:
                return refusal("profile-unavailable", name=requested["name"]), None
            if candidate["ref"] != requested:
                before = previous if original == requested and previous else self.previews.get(requested["revision"])
                if before and before["ref"] != requested:
                    before = None
                return reselection.changed(requested, candidate["ref"], original, before, candidate), None
            if previous and original["name"] == requested["name"] and original != requested:
                reason = await reselection.approve(self.session, previous, candidate)
                if reason:
                    return refusal("consent-required", cause=reason, recovery=reselection.recovery(original)), None
        native = await self.apply(candidate)
        if native.get("accepted") is False:
            if native.get("reason") == "profile-changed" and requested:
                self.invalidate_pages()
                current = native["currentRef"]
                latest = await self.inspect(requested["name"], revision_bound=True)
                if not isinstance(latest, dict) or latest["ref"] != current:
                    latest = None
                return reselection.changed(requested, current, original, candidate, latest), None
            return refusal(native["reason"]), None
        self.recorded = candidate
        self.session.active_tools = None
        try:
            await self.session.controls.refresh()
            name = requested["name"] if requested else None
            capabilities = self.session.controls.capabilities
            if "activeAgent" not in capabilities or capabilities["activeAgent"] != name:
                raise internal_error()
            await self.session.save(live=True)
        except BaseException:
            self.uncertain = True
            raise
        finally:
            self.invalidate_pages()
        return {"accepted": True, "activeAgent": name, "activeRef": requested, "toolOverride": None}, native

    async def publish(self):
        # The durable outcome stands even when an optional UI update can't be delivered.
        try:
            await self.session.commands()
            await self.session.update({"sessionUpdate": "config_option_update",
                                       "configOptions": self.session.controls.options()})
        except (ProtocolError, OSError):
            pass

    async def apply(self, candidate):
        params = {"name": "--none"} if candidate is None else {
            "name": candidate["ref"]["name"], "expectedProfile": candidate["ref"],
        }
        try:
            result = await self.rpc("agents/use", **params)
            if not isinstance(result, dict) or ("accepted" in result and type(result["accepted"]) is not bool):
                raise internal_error()
            if result.get("accepted") is False:
                reason = result.get("reason")
                if reason == "profile-changed" and candidate is not None and "currentRef" in result:
                    current = result["currentRef"]
                    if current is None or (valid_ref(current) and current["name"] == params["name"]
                                           and current != candidate["ref"]):
                        return result
                if reason in ("profile-refused", "write-failed"):
                    return result
                raise internal_error()
            if candidate is not None:
                if result.get("activeRef") != candidate["ref"]:
                    raise internal_error()
            elif not isinstance(result.get("message"), str) or result.get("activeRef") is not None:
                raise internal_error()
        except BaseException:
            self.uncertain = True
            self.invalidate_pages()
            raise
        return result

    def require_restore_guard(self):
        if self.recorded is not None and not self.guarded():
            raise reselection.load_error("guard-unavailable", self.recorded["ref"])

    def preferences(self):
        if self.recorded is None:
            return {}
        ref = self.recorded["ref"]
        return {"agent": ref["name"], "agentRef": ref, "agentPreview": self.recorded}

    async def restore(self):
        previous = self.recorded
        ref = previous["ref"]
        self.require_restore_guard()
        candidate = await self.inspect(ref["name"], revision_bound=True)
        if candidate is UNAVAILABLE:
            raise reselection.load_error("guard-unavailable", ref)
        if candidate is None:
            return False
        if candidate["ref"] != ref:
            reason = await reselection.approve(self.session, previous, candidate)
            if reason:
                raise reselection.load_error(reason, ref)
        try:
            native = await self.apply(candidate)
        except (ProtocolError, OSError):
            raise reselection.load_error("outcome-unknown", ref) from None
        if native.get("accepted") is False:
            if native.get("reason") == "profile-changed":
                raise reselection.load_error("changed-again", ref)
            return False
        self.recorded = candidate
        return True

    async def set_tools(self, names, fits):
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
            result = await self.rpc("tools/set_active", names=names)
            active = result.get("activeTools")
            if "accepted" in result and type(result["accepted"]) is not bool:
                raise internal_error()
            if result.get("accepted") is False:
                reason, key = result.get("reason"), result.get("key")
                if reason == "write-failed":
                    return refusal(reason)
                if reason in ("tool-locked", "tool-unavailable") and key in names:
                    return refusal(reason, key=key)
                raise internal_error()
            if not text_list(active) or len(active) != len(names) or set(active) != set(names):
                raise internal_error()
            self.session.active_tools = active
            await self.session.save(live=True)
        except BackendError as error:
            key = getattr(error, "studio_tool_lock", None) or tool_lock_key(str(error))
            if key in names:
                return refusal("tool-locked", key=key)
            self.uncertain = True
            raise
        except BaseException:
            self.uncertain = True
            raise
        finally:
            self.invalidate_pages()
        return {"accepted": True, "toolOverride": active, "activeTools": active}
