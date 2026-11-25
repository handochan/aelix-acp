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
from . import host_bytes, profile_reselection
from .leases import acquire as acquire_lease
from .profile_refs import text_list, valid_name, valid_ref
from .studio_resources import refusal
from ..wire.errors import ProtocolError, require_object, require_string
from ..wire.transport import encode_frame

META_KEY = "aelix/sessionRelay"
METHOD_PREFIX = "_aelix/session/"
LOOKUP_METHOD = METHOD_PREFIX + "requests/lookup"
OUTCOME_UNKNOWN = -33100
SCOPE_DENIED = -33102
RECEIPT_TTL = 600
MAX_RECEIPTS = 256
MAX_RELAY_FRAME = 256 * 1024
IMAGE_REQUEST_SECONDS = 120
PROFILE_CURSOR_TTL = 60
MAX_PROFILE_SNAPSHOTS = 8
MAX_PROFILE_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_RESOURCE_ID_BYTES = 4096


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
        if session.input_relay is not None:
            session.studio_resources.retire_approval()
            session.input_relay.invalidate_profile_pages()
            session.input_relay.invalidate_images()
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
            and not fits_resource(response["result"], response["id"])):
        return {**response, "result": {"status": "unavailable", "reason": "result-too-large"}}
    return response


def write_result(outcome):
    if outcome["status"] == "unknown":
        raise ProtocolError("Native queue delivery is unknown.", OUTCOME_UNKNOWN)
    return outcome["result"]


def resource_write_result(outcome):
    if outcome["status"] == "unknown":
        raise ProtocolError("Outcome unknown.", OUTCOME_UNKNOWN)
    if "rpcCode" in outcome:
        raise ProtocolError("Request refused.", outcome["rpcCode"])
    return outcome["result"]


def fits_resource(result, response_id):
    return all(len(encode_frame(envelope)) - 1 <= MAX_RELAY_FRAME for envelope in (
        {"jsonrpc": "2.0", "id": response_id, "result": result},
        {"success": True, "data": {"result": result}},
    ))


def validate_response_id(response_id):
    if len(encode_frame(response_id)) - 1 > MAX_RESOURCE_ID_BYTES:
        raise ProtocolError("Session resource request ID exceeds the size limit.")


def fits_resource_reply(result):
    # Reserve the same encoded ID space for initial replies and replays.
    return fits_resource(result, "x" * (MAX_RESOURCE_ID_BYTES - 2))


@dataclass
class ProfilePage:
    snapshot_id: str
    snapshot: dict
    limit: int
    start: int
    result: dict | None = None


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
        self.queue_order = {command: asyncio.Lock() for command in ("steer", "follow_up")}
        self.image_acceptances = {}
        self.receipts: dict[str, Receipt] = {}
        self.profile_pages: dict[str, ProfilePage] = {}
        self.profile_snapshots = {}
        self.profile_generation = 0

    def invalidate_profile_pages(self):
        self.profile_pages.clear()
        self.profile_snapshots.clear()
        self.profile_generation += 1

    def invalidate_images(self):
        # Each entry belongs to the original request, never a duplicate waiter.
        for task, invalidated in self.image_acceptances.items():
            if not invalidated:
                self.image_acceptances[task] = True
                task.cancel()

    def observe_prompt(self, streaming):
        if not streaming:
            self.turn_id = None
            self.invalidate_images()
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
        receipt, owned = self.reserve(key, fingerprint)
        if receipt is None:
            return refusal("dedup-capacity")
        if not owned:
            return write_result(await asyncio.shield(receipt.outcome))
        outcome = receipt.outcome
        issued = locked = False
        task = asyncio.current_task()
        lock = self.queue_order[native_command]

        def release_order():
            nonlocal locked
            if locked:
                lock.release()
                locked = False

        def issued_write():
            nonlocal issued
            issued = True
            self.image_acceptances.pop(task, None)
            release_order()

        if pixels:
            self.image_acceptances[task] = False
            session = self.session
            fence = (session.state.get("model"), session.image_generation, session.aborts)

        try:
            async with asyncio.timeout(IMAGE_REQUEST_SECONDS if pixels else None):
                await lock.acquire()
                locked = True
                if turn_id != self.active_turn():
                    result = {"accepted": False, "reason": "stale-turn"}
                elif pixels:
                    result = await self._write_images(native_command, turn_id, message, refs, receipt, issued_write, fence)
                else:
                    # rpc() reaches stdin.write before its first suspension.
                    release_order()
                    await self.session.rpc(native_command, message=message, _timeout=None)
                    result = {"turnId": turn_id, "accepted": True, "delivery": "native-queue"}
        except asyncio.CancelledError:
            if pixels and not issued:
                fenced = self.image_acceptances.get(task, False)
                outcome.set_result({"status": "completed", "result": {
                    "accepted": False, "reason": "stale-turn" if fenced else "cancelled"}})
                if fenced and task.uncancel() == 0:
                    return write_result(outcome.result())
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
        finally:
            self.image_acceptances.pop(task, None)
            release_order()
        return write_result(outcome.result())

    def reserve(self, key, fingerprint):
        now = self.clock()
        # A pending write retains its slot until it settles, even after lookup
        # expires. Duplicate waiters never cancel the original write.
        for old_key, receipt in tuple(self.receipts.items()):
            if receipt.outcome.done() and now - receipt.first_seen >= RECEIPT_TTL:
                del self.receipts[old_key]
        if prior := self.receipts.get(key):
            if not prior.matches(fingerprint):
                raise ProtocolError("Conflicting requestId.")
            return prior, False
        if len(self.receipts) >= MAX_RECEIPTS:
            return None, False
        outcome = asyncio.get_running_loop().create_future()
        receipt = self.receipts[key] = Receipt(now, fingerprint, outcome)
        return receipt, True

    def drop_profile_snapshot(self, snapshot_id):
        del self.profile_snapshots[snapshot_id]
        for token, page in tuple(self.profile_pages.items()):
            if page.snapshot_id == snapshot_id:
                del self.profile_pages[token]

    def retain_profile_snapshot(self, identifier, snapshot):
        # Account for the catalog and cursors even when the client uses limit=1.
        size = len(encode_frame(snapshot)) + 384 * len(snapshot["profiles"])
        if (len(self.profile_snapshots) >= MAX_PROFILE_SNAPSHOTS
                or sum(size for size, _ in self.profile_snapshots.values()) + size > MAX_PROFILE_SNAPSHOT_BYTES):
            return False
        self.profile_snapshots[identifier] = (size, self.clock() + PROFILE_CURSOR_TTL)
        return True

    async def profiles_list(self, payload, *, response_id):
        payload = require_object(payload)
        now = self.clock()
        for identifier, (_, deadline) in tuple(self.profile_snapshots.items()):
            if now >= deadline:
                self.drop_profile_snapshot(identifier)
        if set(payload) == {"cursor"}:
            cursor = require_string(payload["cursor"], "cursor")
            page = self.profile_pages.get(cursor)
            if page is None:
                return refusal("cursor-stale")
        else:
            if set(payload) not in (set(), {"limit"}):
                raise ProtocolError("Invalid profile page fields")
            limit = payload.get("limit", 40)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ProtocolError("Invalid profile page limit")
            generation = self.profile_generation
            snapshot = await self.session.studio_resources.catalog()
            if generation != self.profile_generation:
                return refusal("cursor-stale")
            if snapshot.get("accepted") is False:
                return snapshot
            page = ProfilePage(uuid.uuid4().hex, snapshot, limit, 0)
        if page.result is not None:
            return page.result
        profiles = page.snapshot["profiles"]
        end = min(page.start + page.limit, len(profiles))
        cursor = "p." + uuid.uuid4().hex
        while end >= page.start:
            following = cursor if end < len(profiles) else None
            result = {**page.snapshot, "profiles": profiles[page.start:end], "nextCursor": following}
            if fits_resource_reply(result):
                break
            end -= 1
        if end < page.start or (end == page.start and end < len(profiles)):
            return refusal("result-too-large")
        if following is not None:
            if page.start == 0 and not self.retain_profile_snapshot(page.snapshot_id, page.snapshot):
                return refusal("result-too-large")
            self.profile_pages[following] = ProfilePage(page.snapshot_id, page.snapshot, page.limit, end)
        page.result = result
        return result

    async def profiles_show(self, payload, *, response_id):
        require_fields(payload, {"name"})
        if not valid_name(payload["name"]):
            raise ProtocolError("Invalid profile name")
        return await self.session.studio_resources.show(payload["name"], fits_resource_reply)

    async def profiles_use(self, payload, *, response_id):
        require_fields(payload, {"profile", "requestId"})
        profile = payload["profile"]
        if profile is not None and not valid_ref(profile):
            raise ProtocolError("Invalid profile reference")
        return await self.resource_write("profiles/use", profile, payload["requestId"], response_id)

    async def tools_set_active(self, payload, *, response_id):
        require_fields(payload, {"names", "requestId"})
        names = payload["names"]
        if not text_list(names) or any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ProtocolError("Expected unique tool names")
        return await self.resource_write("tools/set-active", names, payload["requestId"], response_id)

    async def resource_write(self, method, value, request_id, response_id):
        key = request_key(request_id)
        fingerprint = hashlib.sha256(
            json.dumps([method, value], sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).digest()
        receipt, owned = self.reserve(key, fingerprint)
        if receipt is None:
            return refusal("dedup-capacity")
        if not owned:
            return resource_write_result(await asyncio.shield(receipt.outcome))
        session = self.session
        operation = profile_reselection.Operation(session.studio_resources)
        try:
            result = await self.apply_resource(method, value, operation)
        except (Exception, asyncio.CancelledError) as error:
            if operation.uncertain_effect:
                session.studio_resources.mark_uncertain()
                outcome = {"status": "unknown"}
            else:
                outcome = {"status": "completed", "rpcCode": getattr(error, "code", -32603)}
            if isinstance(error, asyncio.CancelledError):
                receipt.outcome.set_result(outcome)
                raise
        else:
            outcome = {"status": "completed", "result": result}
        receipt.outcome.set_result(outcome)
        settled = resource_write_result(outcome)
        if method == "profiles/use" and settled["accepted"]:
            await session.studio_resources.publish()
        return settled

    async def apply_resource(self, method, value, operation):
        session = self.session
        resources = session.studio_resources
        if (session.mutation_lock.locked() or session.changing or session.turn_active or session.rpc_closed
                or session.proc is None or session.proc.returncode is not None
                or session.compaction_recovery_needed):
            return refusal("busy")
        try:
            lease = acquire_lease(session.server.args.state_dir, session.id, purpose="activity")
        except ProtocolError as error:
            if error.code == -32000:
                return refusal("busy")
            raise
        with lease:
            async with session.mutation_lock:
                if resources.uncertain:
                    return refusal("busy")
                session.reset_cancellation()
                operation.generation = session.cancel_generation
                if method == "profiles/use":
                    expected = {"accepted": True, "activeAgent": value["name"] if value else None,
                                "activeRef": value, "toolOverride": None}
                    if not fits_resource_reply(expected):
                        return refusal("profile-refused")
                    result = await resources.select(value, operation)
                    if fits_resource_reply(result):
                        return result
                    if operation.uncertain_effect:
                        raise ProtocolError("Outcome unknown.", OUTCOME_UNKNOWN)
                    return refusal("profile-refused")
                return await resources.set_tools(value, fits_resource_reply, operation)

    async def _write_images(self, command, turn, message, refs, receipt, issued, fence):
        session = self.session
        server = session.server
        model, generation, stop = fence
        task = asyncio.current_task()

        def current():
            return (not self.image_acceptances.get(task, True) and self.active_turn() == turn
                    and session.aborts == stop and session.image_generation == generation
                    and session.state.get("model") == model and session.input_relay is self
                    and server.sessions.get(session.id) is session and session.id not in server.starting_ids)

        if not current():
            raise image_policy.ImageRejected("stale-turn")
        policy = self.image_policy()
        if not policy:
            raise image_policy.ImageRejected("model-unverified")
        if self.acquiring_images:
            raise image_policy.ImageRejected("image-acquisition-busy")
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
                images.append({"type": "image", "mimeType": mime,
                               "data": image_policy.RUNNING_IMAGE_WIRE_PREFIX + base64.b64encode(body).decode("ascii")})
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

RESOURCE_REQUESTS = {
    METHOD_PREFIX + "profiles/list": SessionRelay.profiles_list,
    METHOD_PREFIX + "profiles/show": SessionRelay.profiles_show,
    METHOD_PREFIX + "profiles/use": SessionRelay.profiles_use,
    METHOD_PREFIX + "tools/set-active": SessionRelay.tools_set_active,
}
REQUESTS.update(RESOURCE_REQUESTS)
