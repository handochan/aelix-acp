"""The negotiated H1 session request registry and binding-local input receipts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import time
import uuid

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


def advertisement(capabilities):
    offer = (capabilities.get("_meta") or {}).get(META_KEY)
    if (not isinstance(offer, dict) or set(offer) != {"version", "notifications"}
            or type(offer["version"]) is not int or offer["version"] != 1
            or type(offer["notifications"]) is not bool):
        return None
    return {"version": 1, "requests": list(REQUESTS), "notifications": []}


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


class SessionRelay:
    def __init__(self, session, binding_id, *, clock=time.monotonic):
        self.session = session
        self.binding_id = binding_id
        self.clock = clock
        self.turn_id = None
        self.receipts: dict[str, Receipt] = {}

    def observe_prompt(self, streaming):
        if not streaming:
            self.turn_id = None
        elif self.turn_id is None:
            self.turn_id = uuid.uuid4().hex

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

    async def lookup(self, payload):
        require_fields(payload, {"requestId"})
        receipt = self.receipts.get(request_key(payload["requestId"]))
        if receipt is None or self.clock() - receipt.first_seen >= RECEIPT_TTL:
            return {"status": "unavailable"}
        return receipt.outcome.result() if receipt.outcome.done() else {"status": "pending"}

    async def _write(self, native_command, payload):
        require_fields(payload, {"turnId", "message", "requestId"})
        turn_id = require_string(payload["turnId"], "turnId")
        message = require_string(payload["message"], "message")
        key = request_key(payload["requestId"])
        if len(turn_id) > 128 or len(message.encode("utf-8")) > 64 * 1024:
            raise ProtocolError("Session relay input exceeds the size limit.")
        fingerprint = hashlib.sha256(json.dumps([native_command, turn_id, message]).encode("utf-8")).digest()
        now = self.clock()
        # A pending write retains its slot until it settles, even after lookup
        # expires. Duplicate waiters never cancel the original write.
        for old_key, receipt in tuple(self.receipts.items()):
            if receipt.outcome.done() and now - receipt.first_seen >= RECEIPT_TTL:
                del self.receipts[old_key]
        if prior := self.receipts.get(key):
            if prior.fingerprint != fingerprint:
                raise ProtocolError("Conflicting requestId.")
            return write_result(await asyncio.shield(prior.outcome))
        if len(self.receipts) >= MAX_RECEIPTS:
            return {"accepted": False, "reason": "dedup-capacity"}

        outcome = asyncio.get_running_loop().create_future()
        self.receipts[key] = Receipt(now, fingerprint, outcome)
        try:
            # No suspension between the turn check and the native stdin write
            # inside rpc(). Native can read it after its last drain.
            if turn_id != self.active_turn():
                result = {"accepted": False, "reason": "stale-turn"}
            else:
                # H1 owns the caller deadline. Keep the native request
                # correlated and the receipt pending until the stream settles.
                await self.session.rpc(native_command, message=message, _timeout=None)
                result = {"turnId": turn_id, "accepted": True, "delivery": "native-queue"}
        except asyncio.CancelledError:
            outcome.set_result({"status": "unknown"})
            raise
        except Exception:
            # Native enqueues before calling queue_update hooks. Even an
            # explicit native error can follow an enqueue, so keep uncertainty.
            outcome.set_result({"status": "unknown"})
        else:
            outcome.set_result({"status": "completed", "result": result})
        return write_result(outcome.result())


# This finite table is both the wire advertisement and the only relay dispatch.
REQUESTS = {
    METHOD_PREFIX + "input/state": SessionRelay.state,
    METHOD_PREFIX + "input/steer": SessionRelay.steer,
    METHOD_PREFIX + "input/follow-up": SessionRelay.follow_up,
    LOOKUP_METHOD: SessionRelay.lookup,
}
