"""Host #61 byte-source v1. Keep reverse wire details out of the queue."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import uuid

from ..wire.errors import ProtocolError

META_KEY = "aelix/hostBytes"
METHOD = "_aelix/host/bytes/read"
MAX_REPLY = 192 * 1024
MAX_CHUNK = 147_264
DIGEST = re.compile(r"[0-9a-f]{64}")


def offered(capabilities):
    offer = (capabilities.get("_meta") or {}).get(META_KEY)
    return (
        isinstance(offer, dict)
        and set(offer) == {"version"}
        and type(offer["version"]) is int
        and offer["version"] == 1
    )


def valid_ref(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


class InvalidGrant(ValueError):
    pass


class HostBytes:
    def __init__(self, server):
        self.server = server
        self.read_lock = asyncio.Lock()

    async def _request(self, params):
        # A retry is the same reverse read/finish, never a new queue submission.
        for _ in range(2):
            try:
                async with self.read_lock:
                    return await self.server.client_request(
                        METHOD, params, timeout=4, max_response_bytes=MAX_REPLY
                    )
            except TimeoutError:
                continue
            except (ProtocolError, OSError):
                break
        raise InvalidGrant("grant-unavailable")

    async def acquire(self, session_id, binding_id, ref, limit, *, current):
        params = {"sessionId": session_id, "bindingId": binding_id, "ref": ref, "readerId": uuid.uuid4().hex}
        body = bytearray()
        digest = hashlib.sha256()
        identity = None
        while current():
            offset = len(body)
            result = await self._request({**params, "offset": offset, "maxBytes": MAX_CHUNK})
            if set(result) != {"offset", "byteLength", "sha256", "eof", "data"}:
                break
            length, sha256, encoded = (result["byteLength"], result["sha256"], result["data"])
            if type(length) is not int or not 0 < length <= limit:
                raise InvalidGrant("image-bytes")
            if (
                type(result["offset"]) is not int
                or result["offset"] != offset
                or not isinstance(sha256, str)
                or not DIGEST.fullmatch(sha256)
                or type(result["eof"]) is not bool
                or not isinstance(encoded, str)
                or len(encoded) > 4 * ((MAX_CHUNK + 2) // 3)
            ):
                break
            if identity is None:
                identity = (length, sha256)
            if (length, sha256) != identity:
                break
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except ValueError:
                break
            if not chunk or len(chunk) > MAX_CHUNK or offset + len(chunk) > length:
                break
            body.extend(chunk)
            digest.update(chunk)
            if result["eof"] != (len(body) == length):
                break
            if result["eof"]:
                if digest.hexdigest() != sha256:
                    break
                if not current():
                    raise InvalidGrant("stale-turn")
                finished = await self._request({**params, "offset": length, "finish": True})
                if set(finished) != {"released"} or finished["released"] is not True:
                    break
                return body, sha256
        if not current():
            raise InvalidGrant("stale-turn")
        raise InvalidGrant("invalid-grant")
