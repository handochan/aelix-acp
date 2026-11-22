"""Issue #52 macOS verification. The credential stays in native process memory."""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import secrets
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROVIDER = "openai-codex"
MODEL = "gpt-6-luna"
ORIGIN = "https://chatgpt.com/backend-api/codex/responses"
H1 = "aelix/sessionRelay"
ROOT = Path(__file__).resolve().parents[1]


class ProbeFailed(RuntimeError):
    pass


def credential(path):
    try:
        stored = json.loads(path.read_bytes())[PROVIDER]
        token = stored["access"]
        expires = stored["expires"]
        if stored.get("type") != "oauth" or not isinstance(token, str) or not token:
            raise ValueError()
        if expires < time.time() * 1000 + 600_000:
            raise ProbeFailed("OAuth refresh needed")
        return token, expires
    except (OSError, ValueError, KeyError, TypeError):
        raise ProbeFailed("Fresh Codex OAuth credential unavailable") from None


def native():
    import httpx
    from aelix_ai.oauth.auth_storage import AuthStorage

    root = Path(os.environ["ACP52_PROBE"])
    access, expires = credential(Path(os.environ["ACP52_READ_ONLY_AUTH"]))
    audit = root / "http.jsonl"
    init = AuthStorage.__init__
    send = httpx.AsyncClient.send
    serial = 0

    def report(value):
        with audit.open("a", encoding="ascii") as file:
            file.write(json.dumps(value) + "\n")

    def install(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self.set_runtime_api_key(PROVIDER, access)

    async def load_memory_only(self):
        self._loaded = True

    def prohibit_write(*_args, **_kwargs):
        report({"guard": "auth-write"})
        raise ProbeFailed("Auth write forbidden in probe")

    async def prohibit_oauth(*_args, **_kwargs):
        report({"guard": "auth-refresh"})
        raise ProbeFailed("Credential refresh forbidden in probe")

    async def observe(self, request, *args, **kwargs):
        nonlocal serial
        if (
            str(request.url) != ORIGIN
            or request.method != "POST"
            or request.headers.get("authorization") != "Bearer " + access
        ):
            report({"guard": "unexpected-http"})
            raise ProbeFailed("Unexpected HTTP destination")
        if time.time() * 1000 >= expires:
            report({"guard": "auth-expired"})
            raise ProbeFailed("Credential refresh needed")
        body = json.loads(request.content)
        if body.get("model") != MODEL or body.get("store") is not False:
            report({"guard": "unexpected-route"})
            raise ProbeFailed("Unexpected Codex route")
        images = set()
        for item in body.get("input", []):
            content = item.get("content", item.get("output", []))
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "input_image":
                        url = part["image_url"]
                        raw = base64.b64decode(url.partition(",")[2], validate=True)
                        images.add(hashlib.sha256(raw).hexdigest())
        serial += 1
        number = serial
        label = (root / "active-case").read_text(encoding="ascii")
        tags = json.loads((root / "fixture-digests.json").read_text(encoding="ascii"))
        report(
            {
                "stage": "wire",
                "case": label,
                "n": number,
                "images": sorted(tag for tag, digest in tags.items() if digest in images),
            }
        )
        # Hold only the initial transport of each synthetic turn. Let the
        # real HTTP call go through after the ACP queue has been exercised.
        while not (root / (label + ".release")).is_file():
            await asyncio.sleep(0.02)
        response = await send(self, request, *args, **kwargs)
        report({"stage": "http", "case": label, "n": number, "status": response.status_code})
        if response.status_code in (401, 403):
            await response.aclose()
            report({"guard": "auth-rejected"})
            raise ProbeFailed("Credential refresh or login needed")
        if label == "stop":
            try:
                await asyncio.Event().wait()
            finally:
                await response.aclose()
        return response

    AuthStorage.__init__ = install
    AuthStorage.load = load_memory_only
    AuthStorage.get_oauth_api_key = prohibit_oauth
    AuthStorage._write_raw = prohibit_write
    AuthStorage._cross_process_lock_fd = prohibit_write
    httpx.AsyncClient.send = observe
    from aelix_coding_agent.cli.entry import main_sync

    raise SystemExit(main_sync())


class Peer:
    def __init__(self, proc, images):
        self.proc, self.images = proc, images
        self.next_id, self.pending, self.text, self.waiting_grant = 0, {}, {}, asyncio.Event()
        self.read = asyncio.create_task(self.reader())
        self.stderr = asyncio.create_task(proc.stderr.read())
        self.output = asyncio.Lock()
        self.grant_requests = {}

    async def write(self, frame):
        async with self.output:
            self.proc.stdin.write((json.dumps(frame) + "\n").encode())
            await self.proc.stdin.drain()

    async def grant(self, frame):
        params = frame["params"]
        ref, offset = params["ref"], params["offset"]
        self.grant_requests[ref] = self.grant_requests.get(ref, 0) + 1
        if ref == "stop":
            self.waiting_grant.set()
            return
        raw = self.images[ref]
        if params.get("finish"):
            assert offset == len(raw)
            result = {"released": True}
        else:
            chunk = raw[offset : offset + params["maxBytes"]]
            result = {
                "offset": offset,
                "byteLength": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "eof": offset + len(chunk) == len(raw),
                "data": base64.b64encode(chunk).decode(),
            }
        await self.write({"jsonrpc": "2.0", "id": frame["id"], "result": result})

    async def reader(self):
        try:
            while line := await self.proc.stdout.readline():
                frame = json.loads(line)
                method = frame.get("method")
                if method == "_aelix/host/bytes/read":
                    await self.grant(frame)
                elif method == "session/update":
                    params = frame["params"]
                    update = params["update"]
                    if update["sessionUpdate"] == "agent_message_chunk":
                        self.text.setdefault(params["sessionId"], []).append(update["content"]["text"])
                elif (future := self.pending.get(frame.get("id"))) is not None and not future.done():
                    future.set_result(frame)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("ACP transport closed"))

    async def call(self, method, params):
        self.next_id += 1
        identifier = self.next_id
        self.pending[identifier] = future = asyncio.get_running_loop().create_future()
        try:
            await self.write({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
            reply = await future
            if "error" in reply:
                raise ProbeFailed(f"ACP error {method}: {reply['error'].get('code')}")
            return reply["result"]
        finally:
            self.pending.pop(identifier, None)

    async def finish(self):
        self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 8)
        except TimeoutError:
            self.proc.terminate()
            await self.proc.wait()
        await self.read
        return await self.stderr


async def probe(root, auth, models):
    from PIL import Image, ImageDraw, ImageFont

    token, _ = credential(auth)
    before = hashlib.sha256(auth.read_bytes()).digest()
    home, workspace, agent = root / "home", root / "work", root / "agent"
    for path in (home, workspace, agent):
        path.mkdir(mode=0o700)

    configured = json.loads(models.read_bytes())["providers"][PROVIDER]["models"]
    model = next(item for item in configured if item["id"] == MODEL)
    safe_keys = {"id", "name", "reasoning", "thinkingLevelMap", "input", "contextWindow", "maxTokens"}
    cfg = {key: value for key, value in model.items() if key in safe_keys}
    (agent / "models.json").write_text(json.dumps({"providers": {PROVIDER: {"models": [cfg]}}}))
    (agent / "settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": PROVIDER,
                "defaultModel": MODEL,
                "defaultThinkingLevel": "off",
                "checkForUpdates": False,
                "retry": {"enabled": False},
                "compaction": {"enabled": False},
            }
        )
    )
    tool = root / "probe_tool.py"
    tool.write_text("""from aelix_agent_core.types import AgentTool
from aelix_ai.messages import TextContent
from aelix_ai.tools import ToolResult
async def execute(*args, **kwargs):
    return ToolResult(content=[TextContent(text="Probe done. Inspect the latest image if present.")])
def setup(api):
    api.register_tool(AgentTool(name="a1b_probe", description="Complete the qualification fixture step",
        parameters={"type":"object","properties":{}}, execute=execute))
""")
    images, marks = {}, {}
    for label in ("steer", "follow", "derived", "stop"):
        marker = str(10_000 + secrets.randbelow(90_000))
        marks[label] = marker
        size = (6000, 4000) if label == "derived" else (460, 290)
        canvas = Image.new("RGB", size, "white")
        fontpath = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        font = ImageFont.truetype(fontpath, round(size[1] * 0.36))
        ImageDraw.Draw(canvas).text((size[0] // 2, size[1] // 2), marker, fill="black", font=font, anchor="mm")
        if label == "derived":
            canvas.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            assert canvas.size == (2048, 1365)
        with io.BytesIO() as out:
            canvas.save(out, "PNG" if label in ("steer", "stop") else "JPEG", quality=90)
            images[label] = out.getvalue()
    (root / "fixture-digests.json").write_text(
        json.dumps({label: hashlib.sha256(raw).hexdigest() for label, raw in images.items()})
    )

    env = {key: os.environ[key] for key in ("PATH", "TMPDIR") if key in os.environ}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONPATH=str(ROOT / "src"),
        AELIX_CODING_AGENT_DIR=str(agent),
        AELIX_AUTH_PATH=str(agent / "auth.json"),
        ACP52_PROBE=str(root),
        ACP52_READ_ONLY_AUTH=str(auth),
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aelix_acp.server",
        "--state-dir",
        str(root / "acp"),
        "--aelix",
        sys.executable,
        "--aelix-arg",
        str(Path(__file__).resolve()),
        "--aelix-arg=--native",
        "--permission-mode",
        "yolo",
        "--no-trust-project",
        "--provider",
        PROVIDER,
        "--model",
        MODEL,
        "--extension",
        str(tool),
        "--turn-timeout",
        "55",
        cwd=workspace,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024,
    )
    peer = Peer(proc, images)
    audit = root / "http.jsonl"

    def events():
        if not audit.is_file():
            return []
        with audit.open(encoding="ascii") as file:
            return [json.loads(line) for line in file if line.endswith("\n")]

    async def wait(check, stage, timeout=12):
        try:
            async with asyncio.timeout(timeout):
                while True:
                    if any("guard" in row for row in events()):
                        raise ProbeFailed("Native probe guard fired")
                    if found := await check():
                        return found
                    await asyncio.sleep(0.02)
        except TimeoutError:
            raise ProbeFailed("Timed out " + stage) from None

    report = {}
    try:
        await peer.call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "_meta": {H1: {"version": 1, "notifications": False}, "aelix/hostBytes": {"version": 1}}
                },
            },
        )
        for label in ("steer", "follow", "derived", "stop"):
            (root / "active-case").write_text(label, encoding="ascii")
            binding = "a1b-" + label
            opened = await peer.call(
                "session/new", {"cwd": str(workspace), "mcpServers": [], "_meta": {H1: {"bindingId": binding}}}
            )
            sid = opened["sessionId"]

            async def relay(method, payload, *, sid=sid, binding=binding):
                return await peer.call(
                    "_aelix/session/" + method, {"sessionId": sid, "bindingId": binding, "payload": payload}
                )

            state = await relay("input/image-state", {})
            if state.get("budget") is None:
                raise ProbeFailed("Selected native route is not allowlisted")

            prompt = (
                "Call the a1b_probe tool once before answering. Then, if an image appears, reply with only "
                "the five digits visible in the latest image. Do not guess and do not read any files."
            )
            if label == "follow":
                prompt = (
                    "Reply READY if you don't yet have an image. If a user attaches an image alone, "
                    "read the five digits visible in it and reply with only those digits. Never guess."
                )
            turn = asyncio.create_task(
                peer.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]})
            )

            async def active(relay=relay):
                item = await relay("input/state", {})
                return item if item["active"] else None

            async def audit_stage(stage, *, label=label):
                return any(row.get("stage") == stage and row.get("case") == label for row in events())

            current = await wait(active, "active " + label)
            await wait(lambda: audit_stage("wire"), "built provider request " + label)
            key = f"{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:4]}-7000-8000-{uuid.uuid4().hex[:12]}"
            message = (
                "" if label == "follow" else "Read the five digits in this image and reply with the digits only."
            )
            method = "follow-up-images" if label == "follow" else "steer-images"
            payload = {"turnId": current["turnId"], "requestId": key, "message": message, "grants": [label]}
            if label == "stop":
                pending = asyncio.create_task(relay("input/" + method, payload))
                await asyncio.wait_for(peer.waiting_grant.wait(), 8)
                assert await relay("requests/lookup", {"requestId": key}) == {"status": "pending"}
                (root / (label + ".release")).touch()
                await wait(lambda: audit_stage("http"), "HTTP before Stop", timeout=20)
                await peer.call("session/cancel", {"sessionId": sid})
                rejection, end = await asyncio.wait_for(asyncio.gather(pending, turn), 20)
                assert rejection == {"accepted": False, "reason": "stale-turn"}
                assert end == {"stopReason": "cancelled"}
                assert peer.grant_requests[label] == 1
                assert all(label not in row.get("images", []) for row in events())
                report[label] = {"pending": True, "cancelled": True, "pixelRequests": 0}
            else:
                receipt = await asyncio.wait_for(relay("input/" + method, payload), 8)
                assert receipt.get("accepted") and receipt["inputKind"] == "pixels"
                assert receipt["captionAdded"] is (label == "follow")
                assert (await relay("requests/lookup", {"requestId": key}))["result"] == receipt
                (root / (label + ".release")).touch()
                end = await asyncio.wait_for(turn, 50)
                assert end == {"stopReason": "end_turn"}
                assert marks[label] in "".join(peer.text[sid])
                rows = [row for row in events() if row.get("case") == label]
                outbound = next(row for row in rows if label in row.get("images", []))
                assert any(row.get("n") == outbound["n"] and row.get("status") == 200 for row in rows)
                report[label] = {
                    "accepted": True,
                    "captionAdded": receipt["captionAdded"],
                    "markerRead": True,
                    "providerDigestMatch": True,
                    "status": 200,
                }
            await peer.call("session/close", {"sessionId": sid})
            print(json.dumps({label: report[label]}), flush=True)
        assert not any("guard" in row for row in events())
    finally:
        diagnostic = await peer.finish()
        markers = [
            token.encode(),
            str(workspace).encode(),
            b"five digits",
            *map(str.encode, marks.values()),
            *[base64.b64encode(raw)[100:160] for raw in images.values()],
        ]
        if any(token and token in diagnostic for token in markers):
            raise ProbeFailed("Diagnostic marker leaked")
        if (agent / "auth.json").exists() or before != hashlib.sha256(auth.read_bytes()).digest():
            raise ProbeFailed("Auth store changed")
        guards = [evt["guard"] for evt in events() if "guard" in evt]
        print(json.dumps({"authWrites": 0, "diagnosticMarkers": 0, "guardEvents": guards}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--native":
        try:
            del sys.argv[1]
            native()
        except Exception:  # noqa: BLE001 - exceptions may carry sensitive provider data.
            print("Native probe stopped", file=sys.stderr)
            raise SystemExit(1) from None
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--auth-store", type=Path, default=Path.home() / ".aelix/agent/auth.json")
        parser.add_argument("--models", type=Path, help="Aelix models.json, defaults to the auth store's sibling")
        args = parser.parse_args()
        auth = args.auth_store.expanduser().resolve()
        models = args.models.expanduser().resolve() if args.models else auth.with_name("models.json")
        try:
            with tempfile.TemporaryDirectory(prefix="acp52-") as folder:
                asyncio.run(probe(Path(folder).resolve(), auth, models))
        except Exception as error:  # noqa: BLE001 - expose only controlled failure text.
            print(str(error) if isinstance(error, ProbeFailed) else type(error).__name__)
            raise SystemExit(1) from None
