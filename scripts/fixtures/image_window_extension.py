"""Trusted extensions used only by the A-0 probe."""
import base64
from dataclasses import replace
import json
import os
from pathlib import Path

from aelix_agent_core.harness.hooks import ContextResult, MessageEndEventResult, ToolResultPatch
from aelix_agent_core.types import AgentTool
from aelix_ai.messages import ImageContent, TextContent
from aelix_ai.tools import ToolResult

PLACEHOLDER = "[Earlier image omitted from this request due to the image limit.]"


def fixture_image(name):
    data = (Path.cwd() / "images" / name).read_bytes()
    return ImageContent(mime_type="image/jpeg" if name.endswith(".jpg") else "image/png",
                        data=base64.b64encode(data).decode("ascii"))


def setup(api):
    async def execute(*args, **kwargs):
        return ToolResult(content=[TextContent(text="tool before"),
                                            fixture_image("tool-before.png"),
                                            TextContent(text="tool after")])

    async def tool_result(event, ctx):
        if event.tool_name == "fixture_image":
            prefix = "large-" if Path("overflow-tool").is_file() else ""
            return ToolResultPatch(content=[event.content[0], fixture_image(f"{prefix}tool.png"),
                                            event.content[2], fixture_image(f"{prefix}tool-added.png")])

    async def message_end(event, ctx):
        message = event.message
        if message.role == "user" and any(isinstance(block, TextContent) and block.text == "steer"
                                          for block in message.content):
            return MessageEndEventResult(message=replace(
                message, content=[message.content[0], fixture_image("steer.jpg"), fixture_image("steer-added.png")]))

    api.register_tool(AgentTool(name="fixture_image", description="Return fixture pixels",
                                parameters={"type": "object", "properties": {}}, execute=execute))
    api.on("tool_result", tool_result)
    api.on("message_end", message_end)


def register_window(api):
    # ACP owns this private directory for the lifetime of a single native worker.
    state_path = Path(os.environ["AELIX_ACP_READY_FILE"]).with_name("image-window.json")
    sequence = 0

    async def window(event, ctx):
        nonlocal sequence
        limits = json.loads((Path.cwd() / "policy.json").read_text())
        kept_bytes = kept_count = 0
        excluded = {}
        messages = list(event.messages)
        cutoff = None
        for mi in range(len(messages) - 1, -1, -1):
            message = messages[mi]
            if message.role not in ("user", "toolResult"):
                continue
            content = None
            for ci in range(len(message.content) - 1, -1, -1):
                block = message.content[ci]
                if not isinstance(block, ImageContent):
                    continue
                # The fixture uses embedded bytes. A-1 must explicitly handle
                # legacy/URL sources, provider encodings and final body limits.
                size = len(base64.b64decode(block.data, validate=True))
                if cutoff is None:
                    if kept_count == limits["count"]:
                        cutoff = "image_count"
                    elif kept_bytes + size > limits["rawBytes"]:
                        cutoff = "raw_bytes"
                if cutoff is None:
                    kept_count += 1
                    kept_bytes += size
                else:
                    if content is None:
                        content = list(message.content)
                    content[ci] = TextContent(text=PLACEHOLDER)
                    excluded[cutoff] = excluded.get(cutoff, 0) + 1
            if content is not None:
                messages[mi] = replace(message, content=content)
        sequence += 1
        state = {"requestSequence": sequence, "keptCount": kept_count, "keptRawBytes": kept_bytes,
                 "omittedCount": sum(excluded.values()), "reasons": excluded}
        pending = state_path.with_suffix(".tmp")
        pending.write_text(json.dumps(state), encoding="utf-8")
        pending.replace(state_path)
        return ContextResult(messages=messages)

    api.on("context", window)
