"""The negotiated H1 session request registry and binding-local input receipts."""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from dataclasses import dataclass
import hashlib
import json
import time
import uuid

from .. import image_policy
from . import host_bytes
from ..wire.errors import ProtocolError, require_object, require_string
from ..wire.transport import encode_frame

META_KEY = "aelix/sessionRelay"
METHOD_PREFIX = "_aelix/session/"
LOOKUP_METHOD = METHOD_PREFIX + "requests/lookup"
OUTCOME_UNKNOWN = -33100
SCOPE_DENIED = -33102
RECEIPT_TTL = 600
# Every stage-one result has a fixed shape and a generated 32-character turn
# ID at most. This limits stored result bytes as well as the number of keys.
MAX_RECEIPTS = 256
MAX_RELAY_FRAME = 256 * 1024
IMAGE_REQUEST_SECONDS = 120


def advertisement(capabilities):
    offer = (capabilities.get("_meta") or {}).get(META_KEY)
    if (not isinstance(offer, dict) or set(offer) != {"version", "notifications"}
            or type(offer["version"]) is not int or offer["version"] != 1
            or type(offer["notifications"]) is not bool):
        return None
    requests = [*REQUESTS, *IMAGE_REQUESTS] if host_bytes.offered(capabilities) else list(REQUESTS)
    return {"version": 1, "requests": requests, "notifications": []}


def open_binding(metadata):
    if not isinstance(metadata, dict) or META_KEY not in metadata:
        return None
    binding = require_fields(metadata[META_KEY], {"bindingId"})
    value = require_string(binding["bindingId"], "bindingId")
    if len(value) > 128:
        raise ProtocolError("Invalid session relay binding.")
    return value


def supports_running_input(state):
    # The verified native RPC family reports both classic queue modes in
    # get_state. No probe writes to the user's queues at session open.
    return (state.get("steeringMode", state.get("steering_mode")) in ("all", "one-at-a-time")
            and state.get("followUpMode", state.get("follow_up_mode")) in ("all", "one-at-a-time"))


def bind_session(session, result, binding_id):
    if binding_id is not None and supports_running_input(session.state):
        session.input_relay = SessionRelay(session, binding_id)
        result.setdefault("_meta", {})[META_KEY] = {"bindingId": binding_id}
    return result


def require_fields(value, names):
    obj = require_object(value, "session relay object")
    if set(obj) != names:
        raise ProtocolError("Invalid session relay fields.")
    return obj


def request_key(value):
    invalid = "requestId must be a UUIDv7."
    if not isinstance(value, str) or len(value) != 36:
        raise ProtocolError(invalid)
    try:
        identifier = uuid.UUID(value)
    except ValueError:
        raise ProtocolError(invalid) from None
    if identifier.version != 7 or str(identifier) != value.lower():
        raise ProtocolError(invalid)
    return identifier.hex


def bounded_lookup_response(response):
    if (response["result"]["status"] == "completed"
            and len(encode_frame(response)) - 1 > MAX_RELAY_FRAME):
        return {**response, "result": {"status": "unavailable", "reason": "result-too-large"}}
    return response


def write_result(outcome):
    if outcome["status"] == "unknown":
        raise ProtocolError("Native queue delivery is unknown.", OUTCOME_UNKNOWN)
    return outcome["result"]


@dataclass
class Receipt:
    first_seen: float
    fingerprint: bytes
    outcome: asyncio.Future
    digests: bytes = b""
    content_fingerprint: bytes | None = None

    def matches(self, fingerprint):
        if self.content_fingerprint is None:
            return self.fingerprint == fingerprint
        return self.content_fingerprint == hashlib.sha256(fingerprint + self.digests).digest()


class SessionRelay:
    def __init__(self, session, binding_id, *, clock=time.monotonic):
        self.session = session
        self.binding_id = binding_id
        self.clock = clock
        self.turn_id = None
        self.acquiring_images = False
        self.receipts: dict[str, Receipt] = {}

    def observe_prompt(self, streaming):
        if not streaming:
            self.turn_id = None
        elif self.turn_id is None:
            self.turn_id = getattr(self.session, "image_turn_id", None) or uuid.uuid4().hex

    def active_turn(self):
        session = self.session
        if (session.cancelled or session.changing or session.rpc_closed or session.proc is None
                or session.proc.returncode is not None or not supports_running_input(session.state)):
            return None
        return self.turn_id

    async def state(self, payload):
        require_fields(payload, set())
        turn_id = self.active_turn()
        return {"active": turn_id is not None, "turnId": turn_id}

    async def steer(self, payload):
        return await self._write("steer", payload)

    async def follow_up(self, payload):
        return await self._write("follow_up", payload)

    async def steer_images(self, payload):
        return await self._write("steer", payload, pixels=True)

    async def follow_up_images(self, payload):
        return await self._write("follow_up", payload, pixels=True)

    def image_policy(self):
        session = self.session
        if not session.socket_dir or not (Path(session.socket_dir) / "image-ready").is_file():
            return None
        return image_policy.policy_for(session.state.get("model"), session.server.image_policies)

    async def image_state(self, payload):
        require_fields(payload, set())
        session = self.session
        model = session.state.get("model") or {}
        name = "/".join(image_policy.model_fields(model)[:2]) if model.get("provider") and model.get("id") else None
        policy = self.image_policy()
        turn_id = self.active_turn()
        reason = ("model-text-only" if model and "image" not in model.get("input", []) else
                  "model-unverified" if not policy else "no-active-turn" if not turn_id else None)
        budget = image_policy.limits(policy) if policy else None
        remaining = ({**budget, "rawBytes": min(budget["rawBytes"], image_policy.MAX_SUBMISSION_BYTES),
                      "count": min(budget["count"], image_policy.MAX_SUBMISSION_COUNT),
                      "nativeLineBytes": image_policy.MAX_NATIVE_LINE} if budget and turn_id else None)
        result = {"status": "unavailable", "model": name, "available": reason is None, "reason": reason,
                  "turnId": turn_id, "budget": budget, "remaining": remaining}
        epoch = session.image_epoch
        if policy and epoch and not session.cancelled and not session.changing and not session.rpc_closed:
            path = Path(session.socket_dir) / "image-state.json"
            try:
                with path.open("rb") as file:
                    sample = json.loads(file.read(4096))
                if sample.pop("epoch", None) == epoch and sample.get("model") == name:
                    result.update(sample, status="available", workerGeneration=session.image_generation,
                                  requestTurnId=session.image_turn_id)
            except (OSError, ValueError):
                pass
        return result

    async def lookup(self, payload):
        require_fields(payload, {"requestId"})
        receipt = self.receipts.get(request_key(payload["requestId"]))
        if receipt is None or self.clock() - receipt.first_seen >= RECEIPT_TTL:
            return {"status": "unavailable"}
        return receipt.outcome.result() if receipt.outcome.done() else {"status": "pending"}

    async def _write(self, native_command, payload, *, pixels=False):
        fields = {"turnId", "message", "requestId"}
        require_fields(payload, fields | {"grants"} if pixels else fields)
        turn_id = require_string(payload["turnId"], "turnId")
        message = payload["message"] if pixels else require_string(payload["message"], "message")
        if not isinstance(message, str):
            raise ProtocolError("message must be a string.")
        key = request_key(payload["requestId"])
        if len(turn_id) > 128 or len(message.encode("utf-8")) > 64 * 1024:
            raise ProtocolError("Session relay input exceeds the size limit.")
        fingerprint_fields = [native_command, turn_id, message]
        if pixels:
            refs = payload["grants"]
            if (not isinstance(refs, list) or not 1 <= len(refs) <= image_policy.MAX_SUBMISSION_COUNT
                    or not all(host_bytes.valid_ref(ref) for ref in refs) or len(set(refs)) != len(refs)):
                raise ProtocolError("Invalid image grants.")
            # H-1 makes each ID an immutable commitment to the prepared bytes.
            # Reserve before reverse reads, then bind the verified digests.
            fingerprint_fields.extend((1, image_policy.CAPTION_VERSION, refs))
        fingerprint = hashlib.sha256(json.dumps(fingerprint_fields).encode("utf-8")).digest()
        now = self.clock()
        # A pending write retains its slot until it settles, even after lookup
        # expires. Duplicate waiters never cancel the original write.
        for old_key, receipt in tuple(self.receipts.items()):
            if receipt.outcome.done() and now - receipt.first_seen >= RECEIPT_TTL:
                del self.receipts[old_key]
        if prior := self.receipts.get(key):
            if not prior.matches(fingerprint):
                raise ProtocolError("Conflicting requestId.")
            return write_result(await asyncio.shield(prior.outcome))
        if len(self.receipts) >= MAX_RECEIPTS:
            return {"accepted": False, "reason": "dedup-capacity"}

        outcome = asyncio.get_running_loop().create_future()
        receipt = self.receipts[key] = Receipt(now, fingerprint, outcome)
        issued = False

        def issued_write():
            nonlocal issued
            issued = True

        try:
            # No suspension between the turn check and the native stdin write
            # inside rpc(). Native can read it after its last drain.
            if turn_id != self.active_turn():
                result = {"accepted": False, "reason": "stale-turn"}
            elif pixels:
                async with asyncio.timeout(IMAGE_REQUEST_SECONDS):
                    result = await self._write_images(native_command, turn_id, message, refs, receipt, issued_write)
            else:
                # H1 owns the caller deadline. Keep the native request
                # correlated and the receipt pending until the stream settles.
                await self.session.rpc(native_command, message=message, _timeout=None)
                result = {"turnId": turn_id, "accepted": True, "delivery": "native-queue"}
        except asyncio.CancelledError:
            if pixels and not issued:
                outcome.set_result({"status": "completed", "result": {"accepted": False, "reason": "cancelled"}})
            else:
                outcome.set_result({"status": "unknown"})
            raise
        except TimeoutError:
            if pixels and not issued:
                outcome.set_result({"status": "completed", "result": {"accepted": False, "reason": "source-timeout"}})
            else:
                outcome.set_result({"status": "unknown"})
        except (host_bytes.InvalidGrant, image_policy.ImageRejected) as error:
            outcome.set_result({"status": "completed", "result": {"accepted": False, "reason": str(error)}})
        except Exception:
            # Native enqueues before calling queue_update hooks. Even an
            # explicit native error can follow an enqueue, so keep uncertainty.
            if pixels and not issued:
                outcome.set_result({"status": "completed", "result": {"accepted": False, "reason": "preflight-unavailable"}})
            else:
                outcome.set_result({"status": "unknown"})
        else:
            outcome.set_result({"status": "completed", "result": result})
        return write_result(outcome.result())

    async def _write_images(self, command, turn, message, refs, receipt, issued):
        session = self.session
        server = session.server
        policy = self.image_policy()
        if not policy:
            raise image_policy.ImageRejected("model-unverified")
        if self.acquiring_images:
            raise image_policy.ImageRejected("image-acquisition-busy")
        model = session.state.get("model")
        generation = session.image_generation
        stop = session.aborts

        def current():
            return (self.active_turn() == turn and session.aborts == stop and session.image_generation == generation
                    and session.state.get("model") == model and session.input_relay is self
                    and server.sessions.get(session.id) is session and session.id not in server.starting_ids)

        budget = image_policy.limits(policy)
        if len(refs) > budget["count"]:
            raise image_policy.ImageRejected("image-count")
        left = min(image_policy.MAX_SUBMISSION_BYTES, budget["rawBytes"])
        images, digests = [], []
        self.acquiring_images = True
        try:
            for ref in refs:
                body, digest = await server.host_bytes.acquire(session.id, self.binding_id, ref,
                                                               min(budget["imageBytes"], left), current=current)
                mime = image_policy.inspect_image(body, max_side=budget["sidePixels"])
                images.append({"type": "image", "mimeType": mime, "data": base64.b64encode(body).decode("ascii")})
                digests.append(digest)
                left -= len(body)
            del body
            caption = not message.strip()
            native_message = image_policy.CAPTION if caption else message
            text_bytes = len(native_message.encode("utf-8"))
            if text_bytes + sum(len(image["data"]) for image in images) >= policy["bodyBytes"]:
                raise image_policy.ImageRejected("provider-body-limit")
            if text_bytes + len(images) * policy["imageTokens"] + policy["outputTokens"] > policy["contextTokens"]:
                raise image_policy.ImageRejected("provider-context-limit")
            live = await session.rpc("get_state", _timeout=5)
            if (not live.get("isStreaming", live.get("is_streaming", False))
                    or live.get("isCompacting", live.get("is_compacting", False))
                    or live.get("model") != model or not current()):
                raise image_policy.ImageRejected("stale-turn")
            receipt.digests = bytes.fromhex("".join(digests))
            receipt.content_fingerprint = hashlib.sha256(receipt.fingerprint + receipt.digests).digest()
            await session.rpc(command, message=native_message, images=images,
                              _timeout=None, _image_write=(current, issued))
        finally:
            self.acquiring_images = False
        return {"accepted": True, "delivery": "native-queue", "turnId": turn, "inputKind": "pixels",
                "captionAdded": caption}


# This finite table is both the wire advertisement and the only relay dispatch.
REQUESTS = {
    METHOD_PREFIX + "input/state": SessionRelay.state,
    METHOD_PREFIX + "input/steer": SessionRelay.steer,
    METHOD_PREFIX + "input/follow-up": SessionRelay.follow_up,
    LOOKUP_METHOD: SessionRelay.lookup,
}

IMAGE_REQUESTS = {
    METHOD_PREFIX + "input/steer-images": SessionRelay.steer_images,
    METHOD_PREFIX + "input/follow-up-images": SessionRelay.follow_up_images,
    METHOD_PREFIX + "input/image-state": SessionRelay.image_state,
}
