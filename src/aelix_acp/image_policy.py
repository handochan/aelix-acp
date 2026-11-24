"""Image limits and the last native context/payload hooks, loaded from this file."""

import base64
import hashlib
import json
import os
import re
import struct
import zlib
from collections import Counter
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

MAX_FILE = 3_750_000
MAX_SIDE = 8000
MAX_SUBMISSION_COUNT = 4
MAX_SUBMISSION_BYTES = 6 * 1024**2
MAX_NATIVE_LINE = 8 * 1024**2
MAX_REQUEST_BYTES = 8 * 1024**2
MAX_REQUEST_COUNT = 20
CAPTION = "The user attached one or more images."
CAPTION_VERSION = 1
PLACEHOLDER = "[Earlier image omitted from this request due to the image limit.]"
# Pinned RPC only carries mimeType/data. The message_end reducer moves this
# transport tag to source, which the native history codec round-trips.
RUNNING_IMAGE_WIRE_PREFIX = "aelix-acp:running-image:v1,"
RUNNING_IMAGE_SOURCE = "aelix-acp:running-image:v1"
HISTORY_IMAGE_MARKER = re.compile(
    rb'"source"\s*:\s*"' + re.escape(RUNNING_IMAGE_SOURCE.encode("ascii")) + rb'"|'
    rb'"data"\s*:\s*"' + re.escape(RUNNING_IMAGE_WIRE_PREFIX.encode("ascii"))
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SOF = {0xC0, 0xC1, 0xC2}
MAX_HEADER = 256 * 1024


RESPONSES_APIS = frozenset({"openai-responses", "openai-codex-responses"})
SDK_BODY_APIS = frozenset({"openai-completions", "openai-responses"})
SDK_REQUEST_OPTIONS = frozenset({"extra_headers", "extra_query", "timeout"})
_HTTP_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_HTTP_QUOTED = r'"(?:[^"\\\r\n]|\\[^\r\n])*"'
JSON_CONTENT_TYPE = re.compile(
    rf"application/json(?:[ \t]*;[ \t]*{_HTTP_TOKEN}[ \t]*=[ \t]*(?:{_HTTP_TOKEN}|{_HTTP_QUOTED}))*",
    re.IGNORECASE | re.ASCII,
)
SDK_EXTRA_BODY_KEYS = frozenset({
    "reasoning", "provider", "enable_thinking", "chat_template_kwargs",
    "thinking", "tool_stream", "providerOptions", "prompt_cache_retention",
})
VERIFIED_IMAGE_POLICIES = (
    {
        "route": ("openai-codex", "gpt-6-luna", "openai-codex-responses", "https://chatgpt.com/backend-api"),
        "imageBytes": MAX_FILE,
        "sidePixels": MAX_SIDE,
        "rawBytes": MAX_REQUEST_BYTES,
        "count": MAX_REQUEST_COUNT,
        "bodyBytes": 12 * 1024**2,
        "contextTokens": 1_050_000,
        "imageTokens": 8192,
        "outputTokens": 128_000,
    },
)


class ImageRejected(ValueError):
    pass


def png_dimensions(raw):
    if len(raw) < 33 or raw[8:16] != b"\0\0\0\rIHDR":
        raise ImageRejected("invalid-image-header")
    width, height, depth, color, compression, filtering, interlace = struct.unpack_from(">IIBBBBB", raw, 16)
    depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if depth not in depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
        raise ImageRejected("invalid-image-header")
    if zlib.crc32(raw[12:29]) != struct.unpack_from(">I", raw, 29)[0]:
        raise ImageRejected("invalid-image-header")
    offset = 33
    while offset + 8 <= min(len(raw), MAX_HEADER):
        size, kind = struct.unpack_from(">I4s", raw, offset)
        end = offset + 12 + size
        if end > len(raw):
            break
        if kind == b"acTL":
            raise ImageRejected("animated-image")
        if kind == b"IDAT":
            return width, height
        if (
            end > MAX_HEADER
            or kind in (b"IHDR", b"IEND")
            or not kind.isalpha()
            or zlib.crc32(raw[offset + 4 : end - 4]) != struct.unpack_from(">I", raw, end - 4)[0]
        ):
            break
        offset = end
    raise ImageRejected("invalid-image-header")


def jpeg_dimensions(raw):
    offset = 2
    dimensions = None
    frame_components = set()
    while offset + 4 <= min(len(raw), MAX_HEADER):
        if raw[offset] != 0xFF:
            break
        while offset < min(len(raw), MAX_HEADER) and raw[offset] == 0xFF:
            offset += 1
        if offset + 3 > min(len(raw), MAX_HEADER):
            break
        marker = raw[offset]
        size = int.from_bytes(raw[offset + 1 : offset + 3], "big")
        end = offset + 1 + size
        if size < 2 or end > min(len(raw), MAX_HEADER) or marker in (0, 0xD8, 0xD9, *range(0xD0, 0xD8)):
            break
        if marker in JPEG_SOF:
            if size < 8 or dimensions is not None:
                break
            components = raw[offset + 8]
            if raw[offset + 3] != 8 or components not in (1, 3, 4) or size != 8 + 3 * components:
                break
            frame_components = set(raw[offset + 9 : end : 3])
            if len(frame_components) != components:
                break
            height, width = struct.unpack_from(">HH", raw, offset + 4)
            dimensions = (width, height)
        if marker == 0xDA:
            if not dimensions or size < 8 or end >= len(raw):
                break
            scans = raw[offset + 3]
            if size != 6 + 2 * scans:
                break
            scan_components = set(raw[offset + 4 : end - 3 : 2])
            if scans and len(scan_components) == scans and scan_components <= frame_components:
                return dimensions
            break
        offset = end
    raise ImageRejected("invalid-image-header")


def inspect_image(raw, *, max_side=MAX_SIDE):
    if raw.startswith(PNG_SIGNATURE):
        mime, dimensions = "image/png", png_dimensions(raw)
    elif raw.startswith(b"\xff\xd8"):
        mime, dimensions = "image/jpeg", jpeg_dimensions(raw)
    else:
        raise ImageRejected("unsupported-image")
    if any(not 0 < side <= max_side for side in dimensions):
        raise ImageRejected("image-dimensions")
    return mime


def model_fields(model):
    if isinstance(model, dict):
        return (
            model.get("provider"),
            model.get("id"),
            model.get("api"),
            model.get("baseUrl", model.get("base_url")),
        )
    return model.provider, model.id, model.api, model.base_url


def policy_for(model, policies):
    if not model:
        return None
    inputs = model.get("input", []) if isinstance(model, dict) else model.input
    if "image" in inputs:
        route = model_fields(model)
        return next((policy for policy in policies if tuple(policy["route"]) == route), None)
    return None


def limits(policy):
    raw = min(MAX_REQUEST_BYTES, policy["rawBytes"], (policy["bodyBytes"] // 4) * 3)
    vision_count = (policy["contextTokens"] - policy["outputTokens"]) // policy["imageTokens"]
    return {
        "imageBytes": min(MAX_FILE, policy["imageBytes"], raw),
        "sidePixels": min(MAX_SIDE, policy["sidePixels"]),
        "rawBytes": raw,
        "count": max(0, min(MAX_REQUEST_COUNT, policy["count"], vision_count)),
    }


def decode_image(encoded, mime, *, limit, max_side):
    if not isinstance(encoded, str):
        raise ImageRejected("unmeasurable-image")
    if encoded.startswith("data:"):
        header, _, encoded = encoded.partition(",")
        if header not in ("data:image/png;base64", "data:image/jpeg;base64"):
            raise ImageRejected("unmeasurable-image")
        embedded_mime = header[5:-7]
        if mime and mime != embedded_mime:
            raise ImageRejected("image-mime")
        mime = embedded_mime
    if not encoded or len(encoded) > 4 * ((limit + 2) // 3):
        raise ImageRejected("image-bytes")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ImageRejected("unmeasurable-image") from None
    if len(raw) > limit:
        raise ImageRejected("image-bytes")
    actual_mime = inspect_image(raw, max_side=max_side)
    if mime and mime != actual_mime:
        raise ImageRejected("image-mime")
    return actual_mime, hashlib.sha256(raw).digest(), len(raw)


def has_running_image(path):
    """Detect provenance in a persisted native JSONL session without loading its images."""
    tail = b""
    with open(path, "rb") as history:
        while chunk := history.read(64 * 1024):
            data = tail + chunk
            if HISTORY_IMAGE_MARKER.search(data):
                return True
            # The pinned codec writes compact field names and separators.
            tail = data[-256:]
    return False


def is_running_image(block):
    return block.source == RUNNING_IMAGE_SOURCE or block.data.startswith(RUNNING_IMAGE_WIRE_PREFIX)


def preserve_image_origin(message, image_type):
    if message.role != "user":
        return None
    content = [
        replace(block, source=RUNNING_IMAGE_SOURCE, data=block.data.removeprefix(RUNNING_IMAGE_WIRE_PREFIX))
        if isinstance(block, image_type) and block.data.startswith(RUNNING_IMAGE_WIRE_PREFIX)
        else block
        for block in message.content
    ]
    return replace(message, content=content) if content != message.content else None


def project_images(original, budget, image_type, text_type):
    messages = list(original)
    kept = Counter()
    total = omitted = 0
    cutoff = "model-unverified" if budget is None else None
    for mi in range(len(messages) - 1, -1, -1):
        message = messages[mi]
        if message.role not in ("user", "toolResult"):
            continue
        content = None
        for ci in range(len(message.content) - 1, -1, -1):
            block = message.content[ci]
            if not isinstance(block, image_type):
                continue
            if budget is None and not is_running_image(block):
                continue
            tagged = block.data.startswith(RUNNING_IMAGE_WIRE_PREFIX)
            if cutoff is None:
                if kept.total() == budget["count"]:
                    cutoff = "image_count"
                else:
                    try:
                        item = decode_image(
                            block.data.removeprefix(RUNNING_IMAGE_WIRE_PREFIX) or block.source,
                            block.mime_type,
                            limit=budget["imageBytes"],
                            max_side=budget["sidePixels"],
                        )
                    except ImageRejected as error:
                        if str(error) == "unmeasurable-image":
                            raise
                        cutoff = str(error)
                    else:
                        if total + item[2] <= budget["rawBytes"]:
                            kept[item] += 1
                            total += item[2]
                            if tagged:
                                if content is None:
                                    content = list(message.content)
                                content[ci] = replace(
                                    block,
                                    source=RUNNING_IMAGE_SOURCE,
                                    data=block.data.removeprefix(RUNNING_IMAGE_WIRE_PREFIX),
                                )
                            continue
                        cutoff = "raw_bytes"
            if content is None:
                content = list(message.content)
            content[ci] = text_type(text=PLACEHOLDER)
            omitted += 1
        if content is not None:
            messages[mi] = replace(message, content=content)
    return (
        messages,
        kept,
        {
            "keptCount": kept.total(),
            "keptRawBytes": total,
            "omittedCount": omitted,
            "reasons": {cutoff: omitted} if omitted else {},
        },
    )


def responses_images(payload):
    """Only visit slots which the pinned Responses encoder uses as content."""
    entries = payload.get("input")
    if not isinstance(entries, list):
        raise ImageRejected("unmeasurable-provider-payload")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ImageRejected("unmeasurable-provider-payload")
        kind = entry.get("type", "message")
        if kind == "message":
            role = entry.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                raise ImageRejected("unmeasurable-provider-payload")
            parts = entry.get("content")
            text_kind = "output_text" if role == "assistant" else "input_text"
            accepts_image = role == "user"
        elif kind == "function_call_output":
            if not isinstance(entry.get("call_id"), str) or not entry["call_id"]:
                raise ImageRejected("unmeasurable-provider-payload")
            parts = entry.get("output")
            text_kind = "input_text"
            accepts_image = True
        elif kind == "function_call":
            if not all(isinstance(entry.get(key), str) for key in ("call_id", "name", "arguments")):
                raise ImageRejected("unmeasurable-provider-payload")
            continue
        elif kind == "reasoning":
            # Native replays encrypted reasoning independently of messages.
            for key, part_kind in (("summary", "summary_text"), ("content", "reasoning_text")):
                items = entry.get(key)
                if items is not None and (
                    not isinstance(items, list)
                    or any(
                        not isinstance(part, dict)
                        or part.get("type") != part_kind
                        or not isinstance(part.get("text"), str)
                        for part in items
                    )
                ):
                    raise ImageRejected("unmeasurable-provider-payload")
            continue
        else:
            raise ImageRejected("unmeasurable-provider-payload")
        if isinstance(parts, str):
            continue
        if not isinstance(parts, list):
            raise ImageRejected("unmeasurable-provider-payload")
        for part in parts:
            if not isinstance(part, dict):
                raise ImageRejected("unmeasurable-provider-payload")
            part_kind = part.get("type")
            if part_kind == text_kind and isinstance(part.get("text"), str):
                continue
            if part_kind != "input_image" or not accepts_image:
                raise ImageRejected("unmeasurable-provider-payload")
            # The pinned encoder emits a data URL and uses auto detail.
            if set(part) - {"type", "image_url", "detail"} or part.get("detail", "auto") != "auto":
                raise ImageRejected("unmeasurable-provider-payload")
            yield part.get("image_url")


def completions_images(payload):
    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            kind = value.get("type")
            if "extra_body" in value or kind in ("image", "input_image"):
                raise ImageRejected("unmeasurable-provider-payload")
            if kind == "image_url":
                url = value.get("image_url")
                yield url.get("url") if isinstance(url, dict) else None
                continue
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def content_type_headers(headers):
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise ImageRejected("unmeasurable-provider-payload")
    return {name: value for name, value in headers.items()
            if isinstance(name, (str, bytes)) and name.lower() in ("content-type", b"content-type")}


def require_sdk_json(provider_headers, extra_headers):
    # OpenAI merges mappings by exact spelling, then httpx looks up headers
    # case-insensitively. Different spellings can therefore coexist on the wire.
    effective = {"Content-Type": "application/json", **provider_headers, **content_type_headers(extra_headers)}
    content_types = []
    for value in effective.values():
        if isinstance(value, bytes):
            value = value.decode("ascii")
        if not isinstance(value, str):
            from openai import Omit

            if isinstance(value, Omit):
                continue
            raise ImageRejected("unmeasurable-provider-payload")
        content_types.append(value)
    # httpx joins different spellings with a comma. Content-Type is a single
    # media type, so a duplicate or a missing field is unverifiable.
    if len(content_types) != 1 or not JSON_CONTENT_TYPE.fullmatch(content_types[0].strip(" \t")):
        raise ImageRejected("unmeasurable-provider-payload")


def provider_body_bytes(payload, api):
    """Encode the pinned Codex body or a qualified SDK route that sends JSON."""
    if api in SDK_BODY_APIS:
        params = {key: value for key, value in payload.items() if key not in SDK_REQUEST_OPTIONS}
        # The native adapters relocate provider fields after the hook. Let the
        # same functions determine the order in which the SDK appends them.
        relocated = SDK_EXTRA_BODY_KEYS if api == "openai-completions" else {"prompt_cache_retention"}
        if params.keys() & relocated:
            if api == "openai-completions":
                from aelix_ai.providers.openai_completions import _relocate_extra_body_params
            else:
                from aelix_ai.providers.openai_responses import _relocate_extra_body_params
            _relocate_extra_body_params(params)
        extra = params.pop("extra_body", {})
        # openai 2.54.0 create() emits schema order, with the two required
        # Completions arguments first. Codex passes its dict straight to httpx.
        order = ("messages", "model") if api == "openai-completions" else ()
        keys = sorted(params, key=lambda key: (key not in order, key))
        payload = {**{key: params[key] for key in keys}, **extra}
    # The SDK's openapi_dumps and httpx 0.28.1's encode_json use these options.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate_payload(payload, policy, expected, *, sdk_headers=None):
    """Validate the serialized image parts against the context window before HTTP."""
    if not isinstance(payload, dict) or "extra_body" in payload:
        raise ImageRejected("unmeasurable-provider-payload")
    api = policy["route"][2]
    try:
        if api in SDK_BODY_APIS:
            require_sdk_json(sdk_headers or {}, payload.get("extra_headers"))
        wire = provider_body_bytes(payload, api)
        if len(wire) > policy["bodyBytes"]:
            raise ImageRejected("provider-body-limit")
        body = json.loads(wire)
    except ImageRejected:
        raise
    except (TypeError, ValueError, RecursionError, ImportError):
        raise ImageRejected("unmeasurable-provider-payload") from None
    urls = responses_images(body) if api in RESPONSES_APIS else completions_images(body)
    images = Counter()
    image_count = raw_bytes = encoded_bytes = 0
    budget = limits(policy)
    for url in urls:
        if not isinstance(url, str) or not url.startswith("data:"):
            raise ImageRejected("unmeasurable-provider-payload")
        image_count += 1
        if image_count > budget["count"]:
            raise ImageRejected("provider-image-limit")
        image = decode_image(url, None, limit=budget["imageBytes"], max_side=budget["sidePixels"])
        raw_bytes += image[2]
        if raw_bytes > budget["rawBytes"]:
            raise ImageRejected("provider-image-limit")
        images[image] += 1
        encoded_bytes += len(url.partition(",")[2])
    if images != expected:
        raise ImageRejected("provider-image-mismatch")
    # Verified routes declare a conservative visual token allowance. Count
    # every remaining serialized byte against the textual context budget.
    output_limits = [body.get(key) for key in ("max_completion_tokens", "max_tokens", "max_output_tokens")]
    if any(limit is not None and (type(limit) is not int or limit < 0) for limit in output_limits):
        raise ImageRejected("unmeasurable-provider-payload")
    reserved_output = max(policy["outputTokens"], *(limit or 0 for limit in output_limits))
    if (
        len(wire) - encoded_bytes + image_count * policy["imageTokens"] + reserved_output
        > policy["contextTokens"]
    ):
        raise ImageRejected("provider-context-limit")
    if api in SDK_BODY_APIS:
        body.update((key, payload[key]) for key in SDK_REQUEST_OPTIONS if key in payload)
    return body


def setup(api):
    from aelix_agent_core.harness.hooks import (
        BeforeProviderPayloadResult,
        ContextResult,
        MessageEndEventResult,
    )
    from aelix_ai.messages import ImageContent, TextContent

    policies = json.loads(os.environ["AELIX_ACP_IMAGE_POLICIES"])
    state_path = Path(os.environ["AELIX_ACP_READY_FILE"]).with_name("image-state.json")
    epoch_path = state_path.with_name("image-epoch")
    ready_path = state_path.with_name("image-ready")
    sequence = 0
    projected = sending = None
    sdk_request = ContextVar("image-sdk-request", default=None)

    async def reset(event, ctx):
        nonlocal projected, sending
        projected = sending = None
        state_path.unlink(missing_ok=True)

    async def mark_image_origin(event, ctx):
        message = preserve_image_origin(event.message, ImageContent)
        if message is not None:
            return MessageEndEventResult(message=message)

    async def window(event, ctx):
        nonlocal projected
        await reset(event, ctx)
        policy = policy_for(ctx.model, policies)
        messages, images, stats = project_images(
            event.messages, limits(policy) if policy else None, ImageContent, TextContent
        )
        epoch = epoch_path.read_text(encoding="ascii") if epoch_path.is_file() else None
        projected = (model_fields(ctx.model) if ctx.model else None, images, stats, epoch)
        if stats["omittedCount"] or images:
            return ContextResult(messages=messages)

    async def before_request(event, ctx):
        route = model_fields(event.model) if event.model else None
        sdk_request.set(None)
        if route and route[2] in SDK_BODY_APIS and policy_for(event.model, policies):
            # This last hook sees the chained provider/auth options before the
            # native adapter merges them over the model's client defaults.
            headers = {
                **content_type_headers(event.model.headers),
                **content_type_headers(event.stream_options.get("headers")),
            }
            sdk_request.set((route, headers))

    async def before_payload(event, ctx):
        nonlocal sending, sequence
        sending = None
        state_path.unlink(missing_ok=True)
        policy = policy_for(event.model, policies)
        route = model_fields(event.model) if event.model else None
        if projected and projected[1] and (not policy or route != projected[0]):
            raise ImageRejected("provider-route-changed")
        if not policy:
            return
        if not isinstance(event.payload, dict) or event.payload.get("model") != route[1]:
            raise ImageRejected("provider-route-changed")
        expected = projected[1] if projected and route == projected[0] else Counter()
        headers = None
        if route[2] in SDK_BODY_APIS:
            request = sdk_request.get()
            if request is None or request[0] != route:
                raise ImageRejected("unmeasurable-provider-payload")
            headers = request[1]
        payload = validate_payload(event.payload, policy, expected, sdk_headers=headers)
        if projected and route == projected[0] and projected[3]:
            sequence += 1
            sending = {
                "epoch": projected[3],
                "requestSequence": sequence,
                "model": "/".join(route[:2]),
                **projected[2],
            }
        return BeforeProviderPayloadResult(payload=payload)

    async def after_response(event, ctx):
        nonlocal sending
        sample, sending = sending, None
        if (
            sample
            and ctx.model
            and 200 <= event.status < 300
            and "/".join(model_fields(ctx.model)[:2]) == sample["model"]
        ):
            pending = state_path.with_suffix(".tmp")
            pending.write_text(json.dumps(sample), encoding="utf-8")
            pending.replace(state_path)

    api.on("before_agent_start", reset)
    api.on("message_end", mark_image_origin, error_mode="throw")
    api.on("context", window, error_mode="throw")
    api.on("before_provider_request", before_request, error_mode="throw")
    api.on("before_provider_payload", before_payload, error_mode="throw")
    api.on("after_provider_response", after_response)
    api.on("session_before_compact", reset)
    api.on("model_select", reset)
    ready_path.touch()
