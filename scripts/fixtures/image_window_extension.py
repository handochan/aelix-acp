"""Trusted extensions used only by the A-0 probe."""
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import uuid

from aelix_agent_core.harness.hooks import (
    BeforeProviderPayloadResult, ContextResult, MessageEndEventResult, ToolResultPatch,
)
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
        if message.role != "user" or not message.content or not isinstance(message.content[0], TextContent):
            return None
        text = message.content[0].text
        if text == "steer":
            content = [message.content[0], fixture_image("steer.jpg"), fixture_image("steer-added.png")]
        elif text == "legacy large":
            image = fixture_image("legacy-large.png")
            content = [message.content[0], ImageContent(source="data:image/png;base64," + image.data)]
        elif text == "legacy remote":
            content = [message.content[0], ImageContent(source="https://example.invalid/fixture.png")]
        else:
            return None
        return MessageEndEventResult(message=replace(message, content=content))

    async def reinsert_image(event, ctx):
        payload = event.payload
        if payload["messages"][-1].get("content") == "reinsert after context":
            image = fixture_image("tool-added.png")
            extra = {"role": "user", "content": [{"type": "image_url",
                     "image_url": {"url": "data:image/png;base64," + image.data}}]}
            return BeforeProviderPayloadResult(payload={**payload, "messages": [*payload["messages"], extra]})
        return None

    api.register_tool(AgentTool(name="fixture_image", description="Return fixture pixels",
                                parameters={"type": "object", "properties": {}}, execute=execute))
    api.on("tool_result", tool_result)
    api.on("message_end", message_end)
    api.on("before_provider_payload", reinsert_image)


def image_raw_bytes(block):
    """Count the embedded representations used by the pinned Chat Completions adapter."""
    encoded = block.data or block.source
    if not block.data and encoded.startswith("data:"):
        header, _, encoded = encoded.partition(",")
        if not header.startswith("data:image/") or not header.endswith(";base64"):
            raise ValueError("Unmeasurable image in request context")
    if not encoded or (not block.data and encoded.startswith("http")):
        raise ValueError("Unmeasurable image in request context")
    try:
        return len(base64.b64decode(encoded, validate=True))
    except ValueError:
        raise ValueError("Unmeasurable image in request context") from None


def project_images(original, *, count, raw_bytes):
    kept_bytes = kept_count = omitted = 0
    messages = list(original)
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
            if cutoff is None:
                if kept_count == count:
                    cutoff = "image_count"
                else:
                    size = image_raw_bytes(block)
                    if kept_bytes + size > raw_bytes:
                        cutoff = "raw_bytes"
                    else:
                        kept_count += 1
                        kept_bytes += size
                        continue
            # A cutoff closes the suffix. The earlier blocks need no decoding.
            if content is None:
                content = list(message.content)
            content[ci] = TextContent(text=PLACEHOLDER)
            omitted += 1
        if content is not None:
            messages[mi] = replace(message, content=content)
    return messages, {"keptCount": kept_count, "keptRawBytes": kept_bytes,
                      "omittedCount": omitted, "reasons": {cutoff: omitted} if omitted else {}}


def register_window(api):
    # This worker-specific directory is owned by ACP. The public sample uses
    # a separate generation ID so the bridge's startup nonce stays private.
    state_path = Path(os.environ["AELIX_ACP_READY_FILE"]).with_name("image-window.json")
    generation = uuid.uuid4().hex
    sequence = turn = 0
    projected = None

    async def reset(event, ctx):
        nonlocal projected
        projected = None
        state_path.unlink(missing_ok=True)

    async def start_turn(event, ctx):
        nonlocal turn
        turn += 1
        await reset(event, ctx)

    async def window(event, ctx):
        nonlocal sequence, projected
        await reset(event, ctx)
        limits = json.loads((Path.cwd() / "policy.json").read_text())
        messages, stats = project_images(event.messages, count=limits["count"], raw_bytes=limits["rawBytes"])
        sequence += 1
        projected = {"workerGeneration": generation, "turnSequence": turn, "requestSequence": sequence, **stats}
        return ContextResult(messages=messages)

    async def check_payload(event, ctx):
        # This fixture knows the Chat Completions shape and registers last.
        # The HTTP receiver independently checks the body after SDK serialization.
        urls = [part["image_url"]["url"]
                for message in event.payload["messages"] if isinstance(message.get("content"), list)
                for part in message["content"] if part["type"] == "image_url"]
        if any(not url.startswith("data:") for url in urls):
            raise ValueError("Unmeasurable image in provider payload")
        wire_stats = (len(urls), sum(image_raw_bytes(ImageContent(source=url)) for url in urls))
        if projected is None or wire_stats != (projected["keptCount"], projected["keptRawBytes"]):
            raise ValueError("Provider payload images differ from the projected context")
        projected["model"] = f"{event.model.provider}/{event.model.id}"

    async def confirm_response(event, ctx):
        # The synthetic endpoint issues a distinct ID for each HTTP request.
        # Successful response headers provide evidence beyond pre-send projection.
        request_id = event.headers.get("x-fixture-request")
        if projected is None or "model" not in projected or not request_id or not 200 <= event.status < 300:
            return
        pending = state_path.with_suffix(".tmp")
        pending.write_text(json.dumps({**projected, "providerRequestId": request_id}), encoding="utf-8")
        pending.replace(state_path)

    api.on("before_agent_start", start_turn)
    api.on("context", window, error_mode="throw")
    api.on("before_provider_payload", check_payload, error_mode="throw")
    api.on("after_provider_response", confirm_response)
    api.on("session_before_compact", reset)
    api.on("model_select", reset)
