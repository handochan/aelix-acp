"""Image limits and the last native context/payload hooks, loaded from this file."""

import base64
import hashlib
import json
import os
import struct
import zlib
from collections import Counter
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
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SOF = {0xC0, 0xC1, 0xC2}
MAX_HEADER = 256 * 1024


RESPONSES_APIS = frozenset({"openai-responses", "openai-codex-responses"})
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
            if cutoff is None:
                if kept.total() == budget["count"]:
                    cutoff = "image_count"
                else:
                    try:
                        item = decode_image(
                            block.data or block.source,
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


def validate_payload(payload, policy, expected):
    """Validate the actual image parts against the context window before HTTP."""
    try:
        wire = json.dumps(payload, allow_nan=False, ensure_ascii=True).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        raise ImageRejected("unmeasurable-provider-payload") from None
    if len(wire) > policy["bodyBytes"]:
        raise ImageRejected("provider-body-limit")
    api = policy["route"][2]
    responses = api in RESPONSES_APIS
    if responses and not isinstance(payload.get("input"), list):
        raise ImageRejected("unmeasurable-provider-payload")
    image_type = "input_image" if responses else "image_url"
    pending = [payload]
    images = Counter()
    image_count = raw_bytes = encoded_bytes = 0
    budget = limits(policy)
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            kind = value.get("type")
            if "extra_body" in value or kind == "image":
                raise ImageRejected("unmeasurable-provider-payload")
            if kind in ("image_url", "input_image"):
                if kind != image_type:
                    raise ImageRejected("unmeasurable-provider-payload")
                url = value.get("image_url")
                if responses:
                    # The pinned encoder uses auto detail and emits a data URL
                    # even when the context stored only raw base64.
                    if set(value) - {"type", "image_url", "detail"} or value.get("detail", "auto") != "auto":
                        raise ImageRejected("unmeasurable-provider-payload")
                else:
                    url = url.get("url") if isinstance(url, dict) else None
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
                continue
            if responses and kind == "input_text" and not isinstance(value.get("text"), str):
                raise ImageRejected("unmeasurable-provider-payload")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if images != expected:
        raise ImageRejected("provider-image-mismatch")
    # Verified routes declare a conservative visual token allowance. Count
    # every remaining serialized byte against the textual context budget.
    output_limits = [payload.get(key) for key in ("max_completion_tokens", "max_tokens", "max_output_tokens")]
    if any(limit is not None and (type(limit) is not int or limit < 0) for limit in output_limits):
        raise ImageRejected("unmeasurable-provider-payload")
    reserved_output = max(policy["outputTokens"], *(limit or 0 for limit in output_limits))
    if len(wire) - encoded_bytes + image_count * policy["imageTokens"] + reserved_output > policy["contextTokens"]:
        raise ImageRejected("provider-context-limit")


def setup(api):
    from aelix_agent_core.harness.hooks import ContextResult
    from aelix_ai.messages import ImageContent, TextContent

    policies = json.loads(os.environ["AELIX_ACP_IMAGE_POLICIES"])
    state_path = Path(os.environ["AELIX_ACP_READY_FILE"]).with_name("image-state.json")
    epoch_path = state_path.with_name("image-epoch")
    ready_path = state_path.with_name("image-ready")
    sequence = 0
    projected = sending = None

    async def reset(event, ctx):
        nonlocal projected, sending
        projected = sending = None
        state_path.unlink(missing_ok=True)

    async def window(event, ctx):
        nonlocal projected
        await reset(event, ctx)
        policy = policy_for(ctx.model, policies)
        messages, images, stats = project_images(
            event.messages, limits(policy) if policy else None, ImageContent, TextContent
        )
        epoch = epoch_path.read_text(encoding="ascii") if epoch_path.is_file() else None
        projected = (model_fields(ctx.model) if ctx.model else None, images, stats, epoch)
        return ContextResult(messages=messages)

    async def before_payload(event, ctx):
        nonlocal sending, sequence
        sending = None
        state_path.unlink(missing_ok=True)
        policy = policy_for(event.model, policies)
        if not policy:
            if projected and projected[1]:
                raise ImageRejected("provider-route-changed")
            return
        route = model_fields(event.model)
        expected = projected[1] if projected and projected[0] == route else Counter()
        if not isinstance(event.payload, dict) or event.payload.get("model") != route[1]:
            raise ImageRejected("provider-route-changed")
        validate_payload(event.payload, policy, expected)
        if projected and projected[0] == route and projected[3]:
            sequence += 1
            sending = {
                "epoch": projected[3],
                "requestSequence": sequence,
                "model": "/".join(route[:2]),
                **projected[2],
            }

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
    api.on("context", window, error_mode="throw")
    api.on("before_provider_payload", before_payload, error_mode="throw")
    api.on("after_provider_response", after_response)
    api.on("session_before_compact", reset)
    api.on("model_select", reset)
    ready_path.touch()
