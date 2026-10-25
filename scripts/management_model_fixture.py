"""Synthetic model for ACP #7: emits an actual native write tool request."""
import asyncio
import contextlib
import json

from beta2_wire import Fixture


class ManagementFixture(Fixture):
    async def respond(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            header = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)).decode()
            length = int(next(line.split(":", 1)[1] for line in header.split("\r\n") if line.lower().startswith("content-length:")))
            request = json.loads(await reader.readexactly(length))
            self.requests.append(request)
            last = request["messages"][-1]
            if "QUALIFICATION_HOLD" in json.dumps(last):
                self.entered.set()
                await self.release.wait()
            if last.get("role") == "user" and "SYNTHETIC_TOOL" in json.dumps(last):
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "synthetic-write-call", "type": "function",
                    "function": {"name": "write", "arguments": json.dumps({"path": str(self.root / "workspace/must-not-exist.txt"), "content": "synthetic"})}}]}
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": "QUALIFICATION_OK"}, "stop"
            chunks = [{"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": request["model"],
                       "choices": [{"index": 0, "delta": d, "finish_reason": f}]} for d, f in ((delta, None), ({}, finish))]
            body = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)
