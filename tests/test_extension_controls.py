import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.runtime.compat import ExtensionControls
from aelix_acp.wire.errors import ProtocolError


class ExtensionControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncorrelated_reply_cannot_confirm_a_mutation(self):
        async def handle(reader, writer):
            await reader.readline()
            writer.write(b'{"id":"other","result":{}}\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        listener = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            client = ExtensionControls({"port": listener.sockets[0].getsockname()[1]}, "secret")
            with self.assertRaisesRegex(ProtocolError, "Invalid extension control response"):
                await client.call("models/set", {"model": "p/model"})
            self.assertFalse(client.pending)
        finally:
            listener.close()
            await listener.wait_closed()

    async def test_cancel_closes_pending_connection_and_returns_a_correlated_error(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def handle(reader, writer):
            payload = json.loads(await reader.readline())
            self.assertEqual(payload["token"], "secret")
            entered.set()
            await reader.read()
            closed.set()
            writer.close()
            await writer.wait_closed()
        listener = await asyncio.start_server(handle, "127.0.0.1", 0)
        client = ExtensionControls({"port": listener.sockets[0].getsockname()[1]}, "secret")
        try:
            task = asyncio.create_task(client.call("models/set", {"model": "p/model"}))
            await asyncio.wait_for(entered.wait(), 2)
            client.cancel_pending()
            with self.assertRaises(ProtocolError) as caught:
                await asyncio.wait_for(task, 2)
            self.assertEqual(caught.exception.code, -32003)
            await asyncio.wait_for(closed.wait(), 2)
            self.assertFalse(client.pending)
        finally:
            listener.close()
            await listener.wait_closed()
