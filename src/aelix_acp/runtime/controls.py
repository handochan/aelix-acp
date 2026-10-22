"""ACP configuration catalog and validated, session-scoped mutations."""
from __future__ import annotations

import contextlib

from ..wire.errors import ProtocolError

PERMISSION_MODES = (
    ("default", "Ask", "Request approval before every tool"),
    ("plan", "Plan", "Only verified built-in read-only tools may run"),
    ("auto-accept-edits", "Accept edits", "Allow verified built-in file edits; ask for other tools"),
    ("auto", "Auto", "Use Aelix's shell classifier; ask when classification is uncertain"),
    ("yolo", "Full access", "Skip client prompts; Aelix guardrails still apply"),
)
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def model_key(model: dict) -> str:
    return f"{model.get('provider', '')}/{model.get('id', '')}"


def sanitize_model(model: dict) -> dict:
    # RPC Model records may contain provider HTTP headers. Never send those to a UI.
    keys = ("id", "provider", "name", "reasoning", "thinkingLevelMap", "input", "contextWindow", "maxTokens")
    return {key: model[key] for key in keys if key in model}


def thinking_levels(model: dict, capabilities: dict) -> list[str]:
    levels = capabilities.get("thinkingLevels")
    if isinstance(levels, list) and levels and all(level in THINKING_LEVELS for level in levels):
        return levels
    if not model.get("reasoning"):
        return ["off"]
    mapping = model.get("thinkingLevelMap") or {}
    if mapping:
        return [level for level in THINKING_LEVELS if level == "off" or level in mapping]
    return ["off", "minimal", "low", "medium", "high"]


class SessionControls:
    def __init__(self, session):
        self.session = session
        self.models: list[dict] = []
        self.capabilities: dict = {}
        self.permission_mode = "default"

    async def refresh(self) -> None:
        self.session.state = await self.session.rpc("get_state")
        try:
            catalog = await self.session.rpc("get_available_models")
            self.models = [sanitize_model(m) for m in catalog.get("models", []) if isinstance(m, dict)]
        except ProtocolError:
            # Older RPC fixtures/runtimes can still expose their actual launch model.
            self.models = []
        current = sanitize_model(self.session.state.get("model") or {})
        if current.get("id") and not any(model_key(m) == model_key(current) for m in self.models):
            self.models.insert(0, current)
        try:
            self.capabilities = await self.session.rpc("aelix_control", method="capabilities", params={})
        except ProtocolError:
            self.capabilities = {}

    def options(self) -> list[dict]:
        state = self.session.state
        options = [{
            "id": "permission_mode", "name": "Permissions", "category": "mode", "type": "select",
            "currentValue": self.permission_mode,
            "options": [{"value": value, "name": name, "description": description}
                        for value, name, description in PERMISSION_MODES],
        }]
        current = state.get("model") or {}
        if current.get("id"):
            options.append({
                "id": "model", "name": "Model", "category": "model", "type": "select",
                "currentValue": model_key(current),
                "options": [{"value": model_key(m), "name": m.get("name") or m["id"],
                             "description": m.get("provider", "")} for m in self.models or [current]],
            })
            levels = thinking_levels(current, self.capabilities)
            actual_level = state.get("thinkingLevel", state.get("thinking_level", "off"))
            if actual_level not in levels:
                levels = [actual_level, *levels]
            options.append({
                "id": "thinking_level", "name": "Reasoning", "category": "thought_level", "type": "select",
                "currentValue": actual_level,
                "options": [{"value": level, "name": level.title()} for level in levels],
            })
        for field, name, values in (
            ("steeringMode", "Steering", ["all", "one-at-a-time"]),
            ("followUpMode", "Follow-up", ["all", "one-at-a-time"]),
            ("autoCompactionEnabled", "Auto compaction", ["true", "false"]),
            ("autoRetryEnabled", "Auto retry", ["true", "false"]),
        ):
            if field in state:
                value = str(state[field]).lower() if isinstance(state[field], bool) else state[field]
                options.append({"id": field, "name": name, "type": "select", "currentValue": value,
                                "options": [{"value": v, "name": v} for v in values]})
        return options

    def selectors(self) -> dict:
        state = self.session.state
        result = {"configOptions": self.options(), "modes": {
            "currentModeId": self.permission_mode,
            "availableModes": [{"id": value, "name": name, "description": description}
                               for value, name, description in PERMISSION_MODES],
        }}
        current = state.get("model") or {}
        if current.get("id"):
            result["models"] = {"currentModelId": model_key(current), "availableModels": [
                {"modelId": model_key(m), "name": m.get("name") or m["id"], "description": m.get("provider", "")}
                for m in self.models or [current]]}
        return result

    async def set(self, config_id: str, value: str, *, publish=True) -> dict:
        option = next((option for option in self.options() if option["id"] == config_id), None)
        if not isinstance(config_id, str) or not isinstance(value, str) or not option or value not in [entry["value"] for entry in option["options"]]:
            raise ProtocolError(f"Unsupported value for {config_id}")
        if self.session.done and not self.session.done.done():
            raise ProtocolError("Session configuration cannot change during a running turn", -32000)
        try:
            return await self._apply(config_id, value, publish=publish)
        except ProtocolError:
            # A backend hook can fail after applying a mutation. Re-query rather
            # than leaving the selector on an invented prior state.
            with contextlib.suppress(ProtocolError):
                await self.refresh()
                if publish:
                    self.session.save()
                    await self.session.update({"sessionUpdate": "config_option_update", "configOptions": self.options()})
            raise

    async def _apply(self, config_id, value, *, publish):
        if config_id == "permission_mode":
            if self.capabilities.get("permissionModes"):
                await self.session.rpc("aelix_control", method="permissions/set", params={"mode": value})
            # Client approval policy remains authoritative even on an older Aelix runtime.
            self.permission_mode = value
            if publish:
                await self.session.update({"sessionUpdate": "current_mode_update", "currentModeId": value})
        elif config_id == "model":
            model = next(m for m in self.models if model_key(m) == value)
            await self.session.rpc("set_model", provider=model["provider"], modelId=model["id"])
        elif config_id == "thinking_level":
            await self.session.rpc("set_thinking_level", level=value)
        else:
            command, parameter = {
                "steeringMode": ("set_steering_mode", "mode"),
                "followUpMode": ("set_follow_up_mode", "mode"),
                "autoCompactionEnabled": ("set_auto_compaction", "enabled"),
                "autoRetryEnabled": ("set_auto_retry", "enabled"),
            }[config_id]
            await self.session.rpc(command, **{parameter: value == "true" if parameter == "enabled" else value})
        self.session.state = await self.session.rpc("get_state")
        if config_id == "model" and self.capabilities.get("version") == 1:
            self.capabilities = await self.session.rpc("aelix_control", method="capabilities", params={})
        result = {"configOptions": self.options()}
        if publish:
            self.session.save()
            await self.session.update({"sessionUpdate": "config_option_update", **result})
        return result
