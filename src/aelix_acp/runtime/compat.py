"""Correlated, bounded client for our beta.2 extension controls."""
import asyncio
import json
import uuid

from ..wire.errors import ProtocolError

LIMIT = 1024 * 1024


class ExtensionControls:
    def __init__(self, address, token):
        self.address, self.token = address, token
        self.pending = {}

    def cancel_pending(self):
        for entry in self.pending.values():
            entry["cancelled"] = True
            if entry.get("writer"):
                entry["writer"].close()

    async def call(self, method, params=None):
        request_id = uuid.uuid4().hex
        entry = {"cancelled": False}
        self.pending[request_id] = entry
        writer = None
        try:
            async with asyncio.timeout(35):
                reader, writer = await asyncio.open_connection("127.0.0.1", self.address["port"], limit=LIMIT)
                entry["writer"] = writer
                if entry["cancelled"]:
                    raise ConnectionError()
                data = (json.dumps({"id": request_id, "token": self.token, "method": method, "params": params or {}}) + "\n").encode()
                if len(data) > LIMIT:
                    raise ProtocolError("Extension control request exceeds 1 MiB")
                writer.write(data)
                await writer.drain()
                answer = json.loads(await reader.readline())
                if not isinstance(answer, dict) or answer.get("id") != request_id:
                    raise ProtocolError("Invalid extension control response", -32603)
                if "error" in answer:
                    raise ProtocolError(answer["error"], answer.get("code", -32603))
                if not isinstance(answer.get("result"), dict):
                    raise ProtocolError("Invalid extension control result", -32603)
                return answer["result"]
        except (OSError, ValueError, TimeoutError):
            if entry["cancelled"]:
                raise ProtocolError("Extension control cancelled; refresh actual state", -32003) from None
            raise ProtocolError("Extension control unavailable; refresh actual state", -32007) from None
        finally:
            self.pending.pop(request_id, None)
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(2):
                        await writer.wait_closed()
                except (ConnectionError, TimeoutError):
                    writer.transport.abort()
