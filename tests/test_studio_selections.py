"""D16 v1 selection vectors over the ACP relay and native JSONL boundary."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from threading import Event
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aelix_acp.runtime import commands, profile_reselection, session_relay, studio_selections
from aelix_acp.server import Server, Session
from aelix_acp.wire.errors import ProtocolError
from aelix_acp.wire.transport import encode_frame
from test_studio_resources import CASES

PREFIX = session_relay.METHOD_PREFIX + "selections/"
LIST_CASES = {
    "tool": ("selections-list-tool-first", "selections-list-tool-next"),
    "skill": ("selections-list-skill",),
    "mcp": ("selections-list-mcp",),
}
SET_CASES = ("selections-set-tool", "selections-set-skill", "selections-set-mcp",
             "selections-deferred", "selections-saved-not-applied", "selections-mismatch")

BLOCKED_TARGET_PROBE = """
import asyncio
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

from aelix_acp.runtime import studio_selections
from aelix_acp.server import Server, Session

async def run():
    server = Server(SimpleNamespace(state_dir="state", profile="coding"))
    session = Session(server, "s", str(Path.cwd()))
    server.sessions[session.id] = session
    session.controls.capabilities = {"methods": ["selections/list"], "selectionKinds": ["tool"]}
    dispatch = server.dispatch

    async def request(method, params, **kwargs):
        if method == "fixture/probe":
            return await session.studio_resources.selections_catalog("tool")
        return await dispatch(method, params, **kwargs)

    async def rpc(*args, **kwargs):
        return {"kind": "tool", "effect": "next_request", "items": [], "policyPending": False,
                "notices": [], "problems": [], "unreadable": [],
                "scopes": {scope: {"path": None, "writable": False, "exists": False, "reason": None}
                           for scope in ("project", "global")}}

    def blocked(*args):
        with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=10) as connection:
            connection.settimeout(None)
            connection.sendall(b"b")
            connection.recv(1)

    session.rpc = rpc
    server.dispatch = request
    studio_selections.project_target = blocked
    await server.run()
    print("ACP cleanup finished", flush=True)

asyncio.run(run())
"""


class SelectionShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_eof_exits_while_target_probe_is_still_blocked(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        accepted = asyncio.Queue()
        listener = await asyncio.start_server(lambda reader, writer: accepted.put_nowait((reader, writer)),
                                              "127.0.0.1", 0)
        self.addAsyncCleanup(listener.wait_closed)
        self.addCleanup(listener.close)
        port = listener.sockets[0].getsockname()[1]
        source = Path(__file__).resolve().parents[1] / "src"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", BLOCKED_TARGET_PROBE, str(port), cwd=root,
            env=dict(os.environ, PYTHONPATH=str(source)), stdin=-1, stdout=-1, stderr=-1,
        )
        try:
            process.stdin.write(encode_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                              "params": {"protocolVersion": 1}}))
            self.assertIn("result", json.loads(await asyncio.wait_for(process.stdout.readline(), 10)))
            process.stdin.write(encode_frame({"jsonrpc": "2.0", "id": 2, "method": "fixture/probe"}))
            await process.stdin.drain()
            reader, writer = await asyncio.wait_for(accepted.get(), 10)
            try:
                self.assertEqual(await asyncio.wait_for(reader.readexactly(1), 5), b"b")
                process.stdin.close()
                self.assertEqual((await asyncio.wait_for(process.stdout.readline(), 5)).strip(), b"ACP cleanup finished")
                self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            if process.returncode is None:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


class SelectionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.server = Server(SimpleNamespace(profile="coding", state_dir=str(self.root)))
        self.addCleanup(self.server.db.close)
        await self.server.dispatch("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "_meta": {session_relay.META_KEY: {"version": 1, "notifications": False}},
        }})
        self.session = Session(self.server, "s-42", str(self.root))
        self.server.sessions[self.session.id] = self.session
        self.now = 0
        self.relay = session_relay.SessionRelay(self.session, "b-21", clock=lambda: self.now)
        self.session.input_relay = self.relay
        self.session.controls.capabilities = {
            "methods": ["selections/list", "selections/set", "resources/list", "tools/set_active", "ui/enable"],
            "selectionKinds": ["tool", "skill", "mcp"],
        }
        self.stdout = asyncio.StreamReader()
        self.session.proc = SimpleNamespace(returncode=None, stdout=self.stdout, stdin=self)
        self.calls = []
        self.mutations = []
        self.requested = asyncio.Event()
        self.controls = {"selections/list": self.list_native, "selections/set": self.set_native,
                         "resources/list": lambda _: {"tools": [{"name": "read"}, {"name": "write"}],
                                                       "activeTools": self.active},
                         "tools/set_active": self.set_tools}
        self.snapshots = {kind: self.snapshot(kind) for kind in LIST_CASES}
        (self.root / ".aelix").mkdir()
        for kind in ("tool", "mcp"):
            (self.root / studio_selections.file_label(kind)).write_bytes(b"{}")
        self.global_file = self.root / "global/config.json"
        self.global_file.parent.mkdir()
        self.global_file.write_bytes(b"{}")
        self.active = ["read"]
        self.writes = {kind: self.write_result("selections-set-" + kind) for kind in LIST_CASES}
        self.server.send = self.send
        self.client_frames = []
        self.native_confirm = {**CASES["project-file-consent"]["nativeConfirm"],
                               "title": studio_selections.CREATE_TITLE, "message": studio_selections.CREATE_HINT}
        self.selection_frame = None
        self.permission_sent = asyncio.Event()
        self.permission_outcome = None
        self.confirm = False
        self.reader = asyncio.create_task(self.session.read_rpc())
        self.addAsyncCleanup(self.close_reader)

    async def close_reader(self):
        self.session._close_task = self.reader
        self.reader.cancel()
        await asyncio.gather(self.reader, *self.session.tasks, return_exceptions=True)

    def snapshot(self, kind):
        results = [CASES[case]["result"] for case in LIST_CASES[kind]]
        items = []
        for result in reversed(results):
            for item in result["items"]:
                items.append({**item, "refusal": None, "path": "/private/row-path", "env": {"TOKEN": "secret"}})
        return {
            **{key: results[0][key] for key in ("kind", "effect", "policyPending")},
            "scopes": {
                "project": {**results[0]["scopes"]["project"],
                            "path": str(self.root / studio_selections.file_label(kind))},
                "global": {"path": str(self.root / "global/config.json"),
                           "exists": True, "writable": True, "reason": None},
            },
            "items": items, "problems": [], "notices": [], "unreadable": [],
        }

    def write_result(self, case):
        result = deepcopy(CASES[case]["result"])
        result.pop("accepted")
        result["written"] = [str(self.root / path) for path in result["written"]]
        result["changed"] = [{"label": row["key"], "saved": row["saved"]} for row in result["changed"]]
        result["mismatches"] = [row["key"] + ": saved, but the global file keeps it on here"
                                for row in result["mismatches"]]
        if result["apply"] and result["apply"]["warning"]:
            result["apply"]["warning"] = "saved, but not applied (/private/settings.json)"
        return result

    def emit(self, event):
        self.stdout.feed_data(encode_frame(event))

    def reply(self, request, data=None, *, error=None):
        frame = {"type": "response", "id": request["id"], "command": request["type"]}
        self.emit({**frame, **({"success": True, "data": data} if error is None
                              else {"success": False, "error": error})})

    def write(self, raw):
        request = json.loads(raw)
        self.calls.append(request)
        if request["type"] == "extension_ui_response":
            self.assertEqual(request["id"], self.native_confirm["id"])
            if request.get("confirmed") is True:
                self.finish_set(self.selection_frame)
            else:
                self.reply(self.selection_frame, error="Not saved: creating settings.json was not confirmed")
            return
        method = request.get("method", request["type"])
        if method == "selections/set":
            self.mutations.append(request["params"])
            self.selection_frame = request
        data = self.controls[method](request)
        if data is not None:
            self.reply(request, data)

    async def drain(self):
        pass

    def list_native(self, request):
        return deepcopy(self.snapshots[request["params"]["kind"]])

    def set_native(self, request):
        if self.confirm:
            self.emit(self.native_confirm)
        else:
            self.finish_set(request)

    def finish_set(self, request):
        kind = request["params"]["kind"]
        result = self.writes[kind]
        changed = {row["label"]: row["saved"] for row in result["changed"]}
        keys = {edit["key"] for edit in request["params"]["changes"]}
        for row in self.snapshots[kind]["items"]:
            if row["key"] in keys and row["label"] in changed:
                row["saved"] = changed[row["label"]]
        for path in map(Path, result["written"]):
            replacement = path.with_suffix(".tmp")
            replacement.write_bytes(b"{}")
            replacement.replace(path)
        self.reply(request, result)

    def missing_project_file(self, kind):
        self.snapshots[kind]["scopes"]["project"]["exists"] = False
        (self.root / studio_selections.file_label(kind)).unlink(missing_ok=True)

    def set_tools(self, request):
        self.active = request["params"]["names"]
        return {"activeTools": self.active}

    async def send(self, frame, *, before_send=None):
        if before_send is not None:
            before_send()
        self.client_frames.append(frame)
        if frame.get("method") == "session/request_permission":
            self.permission_sent.set()
            if self.permission_outcome is not None:
                self.server.pending[frame["id"]].set_result({"outcome": self.permission_outcome})

    async def call(self, case, *, payload=None, binding="b-21", wire=False):
        post = CASES[case]["post"]
        params = {"sessionId": self.session.id, "bindingId": binding,
                  "payload": post["payload"] if payload is None else payload}
        if wire:
            await self.server.handle({"jsonrpc": "2.0", "id": 7, "method": post["method"], "params": params})
            return self.client_frames[-1]
        return await self.server.dispatch(post["method"], params)

    async def test_all_list_fixture_vectors_project_safe_runtime_facts(self):
        for kind, cases in LIST_CASES.items():
            with self.subTest(kind=kind):
                first = await self.call(cases[0])
                cursor = first["nextCursor"]
                expected = CASES[cases[0]]["result"]
                self.assertEqual({**first, "nextCursor": expected["nextCursor"]}, expected)
                if len(cases) == 2:
                    self.assertIsInstance(cursor, str)
                    second = await self.call(cases[1], payload={"kind": kind, "cursor": cursor})
                    self.assertEqual(second, CASES[cases[1]]["result"])
                else:
                    self.assertIsNone(cursor)

    async def test_all_set_fixture_vectors_keep_native_apply_and_mismatches(self):
        for case in SET_CASES:
            with self.subTest(case=case):
                payload = CASES[case]["post"]["payload"]
                self.writes[payload["kind"]] = self.write_result(case)
                for row in self.snapshots[payload["kind"]]["items"]:
                    for edit in payload["changes"]:
                        if edit["key"] == row["key"]:
                            row["saved"] = not edit["on"]
                self.assertEqual(await self.call(case), CASES[case]["result"])
                expected = {key: payload[key] for key in ("scope", "kind", "changes")}
                self.assertEqual(self.mutations[-1], expected)
        self.assertEqual(len(self.mutations), len(SET_CASES))

    async def test_preflight_refusals_and_scope_reject_without_native_write(self):
        self.assertEqual(await self.call("relay-rpc-scope", wire=True), CASES["relay-rpc-scope"]["acpError"])
        self.assertEqual(await self.call("relay-rpc-binding", binding="b-retired", wire=True),
                         CASES["relay-rpc-binding"]["acpError"])
        self.assertFalse(self.calls)
        snapshot = self.snapshots["skill"]
        row = snapshot["items"].pop()
        self.assertEqual(await self.call("relay-error-unknown-row"), CASES["relay-error-unknown-row"]["result"])
        snapshot["items"] = [{**row, "lock": {"kind": "cli", "value": False}}]
        payload = dict(CASES["selections-set-skill"]["post"]["payload"], requestId="019aa123-0001-7000-8000-000000000020")
        self.assertEqual(await self.call("relay-error-locked-row", payload=payload),
                         CASES["relay-error-locked-row"]["result"])
        snapshot["items"] = [row]
        snapshot["scopes"]["project"]["writable"] = False
        payload["requestId"] = "019aa123-0001-7000-8000-000000000021"
        self.assertEqual(await self.call("relay-error-scope-readonly", payload=payload),
                         CASES["relay-error-scope-readonly"]["result"])
        self.snapshots["tool"]["unreadable"] = ["/private/path couldn't be read"]
        self.assertEqual(await self.call("relay-error-unreadable"), CASES["relay-error-unreadable"]["result"])
        self.assertFalse(self.mutations)

    async def test_invalid_inputs_never_reach_native(self):
        valid = CASES["selections-set-skill"]["post"]["payload"]
        for changes in ([], [{"key": "review", "on": None}], [{"key": "review", "on": 1}],
                        [{"key": "review", "unset": True}], [{"key": "review", "on": True, "path": "x"}],
                        valid["changes"] * 2, [{"key": str(i), "on": True} for i in range(1025)]):
            with self.subTest(changes=changes[:2]), self.assertRaises(ProtocolError):
                await self.call("selections-set-skill", payload={**valid, "changes": changes})
        for field in ("confirmed", "cwd", "path", "command"):
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                await self.call("selections-set-skill", payload={**valid, field: True})
        for page in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1, "cursor": "x"}):
            with self.subTest(page=page), self.assertRaises(ProtocolError):
                await self.call("selections-list-skill", payload={"kind": "skill", **page})
        self.assertFalse(self.calls)

    async def test_paging_utf8_boundaries_stable_replay_context_expiry_and_invalidated(self):
        case = "selections-list-skill"
        row = self.snapshots["skill"]["items"][0]
        keys = ["\U00010000", "\ufffd", "한"] + [f"key-{index:03}" for index in range(100)]
        self.snapshots["skill"]["items"] = [{**row, "key": key} for key in reversed(keys)]
        expected = sorted(keys, key=lambda key: key.encode())
        first = await self.call(case)
        self.assertEqual([row["key"] for row in first["items"]], expected[:40])
        full = await self.call(case, payload={"kind": "skill", "limit": 100})
        self.assertEqual([row["key"] for row in full["items"]], expected[:100])
        cursor = full["nextCursor"]
        second = await self.call(case, payload={"kind": "skill", "cursor": cursor})
        self.assertEqual([row["key"] for row in second["items"]], expected[100:])
        self.assertIsNone(second["nextCursor"])
        stale = CASES["relay-error-stale-cursor"]["result"]
        self.assertEqual(await self.call(case, payload={"kind": "tool", "cursor": cursor}), stale)
        self.assertEqual(await self.relay.profiles_list({"cursor": cursor}, response_id=7), stale)
        other = session_relay.SessionRelay(self.session, "another-binding")
        self.assertEqual(await other.selections_list({"kind": "skill", "cursor": cursor}, response_id=7), stale)
        self.snapshots["skill"]["items"].clear()
        self.now = 59.999
        self.assertEqual(await self.call(case, payload={"kind": "skill", "cursor": cursor}), second)
        self.now = 60
        self.assertEqual(await self.call(case, payload={"kind": "skill", "cursor": cursor}), stale)
        self.assertEqual((await self.call(case))["items"], [])
        self.session.studio_resources.invalidate_pages()
        self.assertEqual(await self.call(case, payload={"kind": "skill", "cursor": first["nextCursor"]}), stale)

    async def test_paging_response_size_and_oversized_row_or_header(self):
        case = "selections-list-skill"
        row = self.snapshots["skill"]["items"][0]
        self.snapshots["skill"]["items"] = [
            {**row, "key": str(index), "label": "한" * 15000} for index in range(6)]
        first = await self.call(case)
        self.assertEqual(len(first["items"]), 5)
        self.assertTrue(session_relay.fits_resource_reply(first))
        last = await self.call(case, payload={"kind": "skill", "cursor": first["nextCursor"]})
        self.assertEqual(len(last["items"]), 1)
        self.assertIsNone(last["nextCursor"])
        self.snapshots["skill"]["items"] = [{**row, "label": "한" * 15000}]
        self.snapshots["skill"]["problems"] = ["fixture"] * 4100
        too_large = CASES["relay-error-large-item"]["result"]
        self.assertEqual(await self.call(case), too_large)
        self.snapshots["tool"]["items"] = []
        self.session.active_tools = [f"{index}-" + "한" * 7500 for index in range(12)]
        self.assertEqual(await self.call("selections-list-tool-first"), too_large)

    async def test_tool_page_cannot_mix_rows_with_a_concurrent_tool_override(self):
        held_writes = []
        listing_started = asyncio.Event()

        def change_tools(request):
            held_writes.append(request)
            self.active = request["params"]["names"]
            for row in self.snapshots["tool"]["items"]:
                row["live"] = row["key"] in self.active
            self.requested.set()

        async def list_during_write():
            listing_started.set()
            return await self.call("selections-list-tool-first")

        self.controls["tools/set_active"] = change_tools
        self.session.save = AsyncMock()
        first = await self.call("selections-list-tool-first")
        cursor = first["nextCursor"]
        write = asyncio.create_task(self.call("tools-set-empty"))
        await asyncio.wait_for(self.requested.wait(), 5)
        listing = asyncio.create_task(list_during_write())
        await asyncio.wait_for(listing_started.wait(), 5)
        try:
            self.assertFalse(listing.done())
        finally:
            self.reply(held_writes[0], {"activeTools": []})
            self.assertTrue((await write)["accepted"])
        result = await listing
        self.assertFalse(result["items"][0]["live"])
        self.assertEqual(result["activeTools"], [])
        self.assertEqual(result["toolOverride"], [])
        self.assertEqual(await self.call("selections-list-tool-next", payload={"kind": "tool", "cursor": cursor}),
                         CASES["relay-error-stale-cursor"]["result"])

    async def test_first_page_wait_for_resource_mutation_is_bounded(self):
        async with self.session.studio_resources.selection_lock:
            with patch.object(session_relay, "SELECTION_READ_WAIT", 0):
                self.assertEqual(await self.call("selections-list-tool-first"),
                                 CASES["relay-error-unreadable"]["result"])
        self.assertFalse(self.calls)
        self.assertIn("items", await self.call("selections-list-tool-first"))

    async def test_close_retires_tool_write_waiting_for_snapshot_lock(self):
        started = asyncio.Event()

        async def write_tools():
            started.set()
            return await self.call("tools-set-empty")

        self.session.save = AsyncMock()
        self.server.close_session = AsyncMock(return_value={})
        try:
            async with self.session.studio_resources.selection_lock:
                write = asyncio.create_task(write_tools())
                await asyncio.wait_for(started.wait(), 5)
                self.assertEqual(await self.server.dispatch("session/close", {"sessionId": self.session.id}), {})
        finally:
            result, = await asyncio.gather(write, return_exceptions=True)
        self.assertIsInstance(result, ProtocolError)
        self.assertFalse(self.calls)

    async def test_stop_leaves_prompt_admission_free_while_relay_write_waits_for_read(self):
        started = asyncio.Event()

        async def write_tools():
            started.set()
            return await self.call("tools-set-empty")

        self.session.cancel_native = AsyncMock()
        self.session.prompt = AsyncMock(return_value={"stopReason": "end_turn"})
        params = {"sessionId": self.session.id}
        try:
            async with self.session.studio_resources.selection_lock:
                write = asyncio.create_task(write_tools())
                await asyncio.wait_for(started.wait(), 5)
                await self.server.dispatch("session/cancel", params)
                result = await self.server.dispatch("session/prompt", {
                    **params, "prompt": [{"type": "text", "text": "next"}],
                })
                self.assertEqual(result, {"stopReason": "end_turn"})
        finally:
            write_result, = await asyncio.gather(write, return_exceptions=True)
        self.assertIsInstance(write_result, ProtocolError)
        self.assertFalse(self.calls)

    async def test_selection_recovery_preserves_stop_and_close_retirement(self):
        params = {"sessionId": self.session.id}
        self.session.cancel_native = AsyncMock()
        for retire in ("session/cancel", "session/close"):
            with self.subTest(retire=retire):
                entered, release = asyncio.Event(), asyncio.Event()
                self.session.proc = None
                self.session.rpc_closed = True
                self.session.compaction_recovery_needed = True

                async def recover(*, preserve_cancellation=False):
                    self.assertTrue(preserve_cancellation)
                    self.session.studio_resources.retire_approval(keep_recovery=True)
                    entered.set()
                    await release.wait()
                    self.session.proc = SimpleNamespace(returncode=None)
                    self.session.rpc_closed = False
                    self.session.compaction_recovery_needed = False

                self.session.recover_after_compaction = recover
                pending = (self.call("tools-set-empty") if retire == "session/cancel"
                           else self.server.dispatch("_aelix/manage", {
                               **params, "action": "tools/set_active", "arguments": {"names": []},
                           }))
                write = asyncio.create_task(pending)
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    self.assertFalse(write.done())
                    if retire == "session/close":
                        with self.assertRaises(ProtocolError):
                            await self.server.dispatch(retire, params)
                    else:
                        await self.server.dispatch(retire, params)
                finally:
                    release.set()
                    result, = await asyncio.gather(write, return_exceptions=True)
                self.assertIsInstance(result, ProtocolError)
                self.assertEqual(result.code, -32004)
                self.assertFalse(self.calls)

    async def test_close_retires_queued_legacy_tool_change_before_dispatch(self):
        started = asyncio.Event()
        params = {"sessionId": self.session.id}

        async def write_tools():
            started.set()
            return await self.server.dispatch("_aelix/manage", {
                **params, "action": "tools/set_active", "arguments": {"names": []},
            })

        self.session.save = AsyncMock()
        self.server.close_session = AsyncMock(return_value={})
        try:
            async with self.session.studio_resources.selection_lock:
                write = asyncio.create_task(write_tools())
                await asyncio.wait_for(started.wait(), 5)
                self.assertEqual(await self.server.dispatch("session/close", params), {})
        finally:
            write_result, = await asyncio.gather(write, return_exceptions=True)
        self.assertIsInstance(write_result, ProtocolError)
        self.assertFalse(self.calls)

    async def test_stop_fences_legacy_tool_change_during_inventory_preflight(self):
        raw = str(self.root / "tool")
        key = self.relay.identifiers.key("tool", raw)
        held = []
        params = {"sessionId": self.session.id}

        def pause(request):
            held.append(request)
            self.requested.set()

        self.controls["resources/list"] = pause
        self.session.cancel_native = AsyncMock()
        self.session.save = AsyncMock()
        write = asyncio.create_task(self.server.dispatch("_aelix/manage", {
            **params, "action": "tools/set_active", "arguments": {"names": [key]},
        }))
        await asyncio.wait_for(self.requested.wait(), 5)
        await self.server.dispatch("session/cancel", params)
        self.reply(held[0], {"tools": [{"name": raw}]})
        with self.assertRaises(ProtocolError):
            await write
        self.assertEqual([call["method"] for call in self.calls], ["resources/list"])

    async def test_queued_write_yields_to_admitted_slash_mutation(self):
        write_waiting = asyncio.Event()
        slash_started = asyncio.Event()
        lock = self.session.studio_resources.selection_lock

        async def write_tools():
            write_waiting.set()
            return await self.call("tools-set-empty")

        async def slash(*_):
            slash_started.set()
            async with lock:
                return {"stopReason": "end_turn"}

        self.session.prompt = slash
        async with lock:
            write = asyncio.create_task(write_tools())
            await asyncio.wait_for(write_waiting.wait(), 5)
            prompt = asyncio.create_task(self.server.dispatch("session/prompt", {
                "sessionId": self.session.id, "prompt": [{"type": "text", "text": "/tools read"}],
            }))
            await asyncio.wait_for(slash_started.wait(), 5)
        self.assertEqual(await asyncio.wait_for(write, 5), CASES["relay-error-busy"]["result"])
        self.assertEqual(await asyncio.wait_for(prompt, 5), {"stopReason": "end_turn"})
        self.assertFalse(self.calls)

    @patch.object(profile_reselection, "host_verified_manual_approval", return_value=True)
    async def test_confirm_approve_refuse_cancel_and_timeout(self, _):
        self.confirm = True
        self.missing_project_file("skill")
        fixture = CASES["project-file-consent"]
        for index, case in enumerate(("project-file-consent", "project-permission-deny",
                                      "project-permission-cancel", "project-permission-timeout"), start=1):
            with self.subTest(case=case):
                vector = CASES[case]
                self.permission_outcome = (fixture["reply"]["result"]["outcome"] if index == 1
                                           else vector["permissionOutcome"])
                payload = {**fixture["post"]["payload"], "requestId": f"019aa123-0001-7000-8000-{index:012d}"}
                timeout = 0.1 if self.permission_outcome is None else 45
                self.missing_project_file("skill")
                self.snapshots["skill"]["items"][0]["saved"] = True
                with patch.object(profile_reselection, "APPROVAL_SECONDS", timeout):
                    self.assertEqual(await self.call(case if index == 1 else "project-file-consent", payload=payload),
                                     vector["result"])
                self.assertEqual([call for call in self.calls if call["type"] == "extension_ui_response"][-1],
                                 vector["nativeReply"])
                card = [frame for frame in self.client_frames if frame["method"] == "session/request_permission"][-1]
                self.assertEqual(card["params"]["toolCall"]["kind"], "edit")
                self.assertEqual([(o["optionId"], o["kind"]) for o in card["params"]["options"]],
                                 [("allow-once", "allow_once"), ("reject-once", "reject_once")])
        self.assertTrue(any(frame["method"] == "$/cancel_request" for frame in self.client_frames))

    async def test_unverified_host_cannot_admit_first_file_creation(self):
        self.confirm = True
        self.missing_project_file("skill")
        self.permission_outcome = {"outcome": "selected", "optionId": "allow-once"}
        self.assertEqual(await self.call("selections-set-skill"), CASES["project-permission-timeout"]["result"])
        self.assertFalse(self.mutations)
        self.assertFalse(self.client_frames)
        self.assertFalse((self.root / ".aelix/settings.json").exists())

    @patch.object(profile_reselection, "host_verified_manual_approval", return_value=True)
    async def test_yolo_cannot_create_and_unsupported_kind_is_never_sent(self, _):
        self.session.controls.permission_mode = "yolo"
        self.missing_project_file("skill")
        self.assertEqual(await self.call("selections-set-skill"), CASES["project-permission-timeout"]["result"])
        self.session.controls.capabilities["selectionKinds"].remove("skill")
        self.assertEqual(await self.call("relay-error-unsupported"), CASES["relay-error-unsupported"]["result"])
        self.assertFalse(self.mutations)

    async def test_override_reapplied_before_reply_and_receipts_are_deduped(self):
        self.session.active_tools = []
        self.active = []
        self.controls["selections/set"] = lambda _: self.requested.set()
        original = asyncio.create_task(self.call("selections-set-tool"))
        await asyncio.wait_for(self.requested.wait(), 5)
        duplicate = asyncio.create_task(self.call("selections-set-tool"))
        self.assertEqual(await self.relay.lookup({"requestId": CASES["selections-set-tool"]["post"]["payload"]["requestId"]}),
                         {"status": "pending"})
        self.active = ["write"]  # Native re-enabled a tool while applying the new defaults.
        self.finish_set(self.selection_frame)
        for task in (original, duplicate):
            self.assertEqual(await task, CASES["selections-set-tool"]["result"])
        self.assertEqual(self.active, [])
        self.assertEqual(self.session.active_tools, [])
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual([call["params"] for call in self.calls if call.get("method") == "tools/set_active"],
                         [{"names": []}])

    async def test_opaque_tool_lock_refusal_cannot_echo_the_native_identifier(self):
        native = str(self.root / "tool")
        key = self.relay.identifiers.key("tool", native)
        self.assertNotEqual(self.relay.identifiers.key("tool", key), key)
        self.controls["resources/list"] = lambda _: {"tools": [{"name": native}, {"name": key}]}

        def refuse(request):
            self.assertEqual(request["params"]["names"], [native])
            self.reply(request, error=f"Cannot turn on {native}: {native} is locked off for this run: profile")

        self.controls["tools/set_active"] = refuse
        payload = {**CASES["tools-set-empty"]["post"]["payload"], "names": [key]}
        self.assertEqual(await self.call("tools-set-empty", payload=payload),
                         {"accepted": False, "reason": "tool-locked", "key": key})

    async def test_resource_inventory_and_errors_redact_native_free_text(self):
        fictional = "FICTIONAL_NOT_A_SECRET_ACP71"
        detail = "fixture password=" + fictional
        self.controls["resources/list"] = lambda _: {
            "tools": [{"name": "read", "description": detail}],
            "skills": [{"name": "review", "description": detail}],
            "skippedTools": [{"tool": "write", "reason": detail}],
            "extensionErrors": [{"message": detail}],
        }
        params = {"sessionId": self.session.id, "action": "resources/list"}
        inventory = await self.server.dispatch("_aelix/manage", params)
        self.assertNotIn(fictional, json.dumps(inventory))
        self.assertEqual(inventory["tools"][0]["description"], "fixture [redacted credential]")

        self.controls["resources/list"] = lambda request: self.reply(request, error=detail)
        await self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "_aelix/manage", "params": params})
        self.assertEqual(self.client_frames[-1]["error"]["message"], "fixture [redacted credential]")

    async def test_command_prompt_and_mcp_inventory_text_is_redacted(self):
        fictional = "FICTIONAL_NOT_A_SECRET_ACP71"
        detail = "extension:password=" + fictional
        self.controls["resources/list"] = lambda _: {
            "commands": [{"name": "fixture", "description": detail}],
            "prompts": [{"name": "fixture", "description": detail}],
            "mcp": [{"name": "server", "clientName": detail, "connected": False}],
        }
        inventory = await self.server.dispatch("_aelix/manage", {
            "sessionId": self.session.id, "action": "resources/list",
        })
        self.assertNotIn(fictional, json.dumps(inventory))
        self.assertEqual(inventory["commands"][0]["description"], "extension:[redacted credential]")
        self.assertEqual(inventory["prompts"][0]["description"], "extension:[redacted credential]")
        self.assertEqual(inventory["mcp"][0]["clientName"], "extension:[redacted credential]")

    @patch.object(profile_reselection, "host_verified_manual_approval", return_value=True)
    async def test_close_and_rebind_while_waiting_for_consent_reject_late_selection(self, _):
        self.confirm = True
        self.missing_project_file("skill")
        for method in ("session/close", "session/load"):
            with self.subTest(method=method):
                self.permission_sent.clear()
                self.session.studio_resources.retired_binding = None
                payload = dict(CASES["selections-set-skill"]["post"]["payload"],
                               requestId=f"019aa123-0001-7000-8000-{len(self.mutations):012x}")
                write = asyncio.create_task(self.call("selections-set-skill", payload=payload))
                await asyncio.wait_for(self.permission_sent.wait(), 5)
                with self.assertRaises(ProtocolError):
                    await self.server.dispatch(method, {"sessionId": self.session.id, "cwd": str(self.root),
                        "_meta": {session_relay.META_KEY: {"bindingId": "replacement"}}})
                self.assertEqual(await write, CASES["project-permission-cancel"]["result"])
                self.assertEqual(self.calls[-1], CASES["project-permission-cancel"]["nativeReply"])

    async def test_close_during_preflight_sends_zero_mutations(self):
        held = []
        self.controls["selections/list"] = lambda request: (held.append(request), self.requested.set())[1]
        write = asyncio.create_task(self.call("selections-set-skill"))
        await asyncio.wait_for(self.requested.wait(), 5)
        with self.assertRaises(ProtocolError):
            await self.server.dispatch("session/close", {"sessionId": self.session.id})
        self.reply(held[0], self.snapshots["skill"])
        with self.assertRaises(ProtocolError):
            await write
        self.assertFalse(self.mutations)

    async def test_delete_and_resource_reload_serialize_with_dispatched_write(self):
        self.snapshots["mcp"]["items"][0]["saved"] = True
        self.controls["selections/set"] = lambda _: self.requested.set()
        write = asyncio.create_task(self.call("selections-set-mcp"))
        await asyncio.wait_for(self.requested.wait(), 5)
        for method in ("session/delete", "_aelix/manage"):
            with self.subTest(method=method), self.assertRaises(ProtocolError) as raised:
                await self.server.dispatch(method, {"sessionId": self.session.id,
                                                    "action": "resources/reload", "arguments": {}})
            self.assertEqual(raised.exception.code, -32000)
        self.finish_set(self.selection_frame)
        self.assertEqual(await write, CASES["selections-set-mcp"]["result"])

    async def test_ack_after_close_started_is_unknown(self):
        self.controls["selections/set"] = lambda _: self.requested.set()
        write = asyncio.create_task(self.call("selections-set-tool"))
        await asyncio.wait_for(self.requested.wait(), 5)
        with self.assertRaises(ProtocolError):
            await self.server.dispatch("session/close", {"sessionId": self.session.id})
        self.finish_set(self.selection_frame)
        with self.assertRaises(ProtocolError) as raised:
            await write
        self.assertEqual(raised.exception.code, session_relay.OUTCOME_UNKNOWN)
        self.assertTrue(self.session.studio_resources.uncertain)

    async def test_native_refusal_and_unattributable_mismatch(self):
        def refuse(request):
            self.reply(request, error="Not saved: Cannot save: unreadable fixture")
        self.controls["selections/set"] = refuse
        self.assertEqual(await self.call("relay-error-write-failed"), CASES["relay-error-write-failed"]["result"])
        self.controls["selections/set"] = self.set_native
        vector = CASES["unkeyed-mismatch"]
        self.writes["tool"]["mismatches"] = vector["nativeFragments"]["mismatches"]
        result = await self.call("selections-set-tool")
        self.assertEqual(result["mismatches"], vector["dto"]["mismatches"])

    async def test_native_validation_reports_a_row_disappearing_after_preflight(self):
        def refuse(request):
            self.snapshots["skill"]["items"].clear()
            self.reply(request, error="Unknown skill row: review")

        self.controls["selections/set"] = refuse
        self.assertEqual(await self.call("selections-set-skill"), CASES["relay-error-unknown-row"]["result"])

    async def test_native_pre_save_missing_row_survives_external_mcp_file_replacement(self):
        def refuse(request):
            target = self.root / ".aelix/mcp.json"
            replacement = target.with_suffix(".tmp")
            replacement.write_bytes(b'{"mcpServers": {}}')
            replacement.replace(target)
            self.snapshots["mcp"]["items"].clear()
            self.reply(request, error="Unknown mcp row: local-docs")

        self.controls["selections/set"] = refuse
        self.assertEqual(await self.call("selections-set-mcp"),
                         {"accepted": False, "reason": "selection-missing", "scope": "project",
                          "written": [], "mismatches": [{"key": "local-docs", "reason": "missing"}]})
        self.assertFalse(self.session.studio_resources.uncertain)

    async def test_mcp_save_refuses_partial_batch_and_reports_written_project_file(self):
        case = "selections-set-mcp"
        snapshot = self.snapshots["mcp"]
        snapshot["items"].append({**snapshot["items"][0], "key": "gone", "label": "gone", "saved": True})
        snapshot["items"][0]["saved"] = True
        payload = deepcopy(CASES[case]["post"]["payload"])
        payload["changes"].append({"key": "gone", "on": False})

        def partial(request):
            snapshot["items"].pop()
            snapshot["items"][0]["saved"] = False
            self.finish_set(request)

        self.controls["selections/set"] = partial
        result = await self.call(case, payload=payload)
        self.assertEqual(result, {"accepted": False, "reason": "selection-missing", "scope": "project",
                                  "written": [".aelix/mcp.json"],
                                  "mismatches": [{"key": "gone", "reason": "missing"}]})
        self.assertEqual(await self.relay.lookup({"requestId": payload["requestId"]}),
                         {"status": "completed", "result": result})

    @patch.object(profile_reselection, "host_verified_manual_approval", return_value=True)
    async def test_native_busy_and_dialog_cancellation_are_definite_refusals(self, _):
        self.controls["selections/set"] = lambda request: self.reply(
            request, error="Runtime mutations require an idle session")
        self.assertEqual(await self.call("selections-set-tool"), CASES["relay-error-busy"]["result"])
        self.controls["selections/set"] = self.set_native
        self.confirm = True
        self.missing_project_file("skill")
        write = asyncio.create_task(self.call("selections-set-skill"))
        await asyncio.wait_for(self.permission_sent.wait(), 5)
        self.reply(self.selection_frame, error="Command cancelled")
        self.assertEqual(await write, CASES["project-permission-cancel"]["result"])
        self.assertFalse(self.session.studio_resources.uncertain)

    async def test_mcp_file_alias_to_global_is_readonly(self):
        scopes = self.snapshots["mcp"]["scopes"]
        scopes["global"]["path"] = scopes["project"]["path"]
        listing = await self.call("selections-list-mcp")
        self.assertFalse(listing["scopes"]["project"]["writable"])
        self.assertEqual(await self.call("selections-set-mcp"), CASES["relay-error-scope-readonly"]["result"])
        self.assertFalse(self.mutations)

    async def test_readonly_native_scope_never_opens_project_file(self):
        project = self.snapshots["skill"]["scopes"]["project"]
        project.update(writable=False, reason="this folder is not trusted", exists=False)
        # A project file is physically present even though native declines to inspect it.
        self.assertTrue(Path(project["path"]).is_file())
        for case in ("selections-list-skill", "selections-set-skill"):
            with self.subTest(case=case), patch.object(
                studio_selections, "target_stamp", wraps=studio_selections.target_stamp
            ) as probe:
                result = await self.call(case)
                probe.assert_not_called()
                if case == "selections-list-skill":
                    self.assertEqual(result["scopes"]["project"],
                                     {"writable": False, "reason": "untrusted-project", "exists": False})
                else:
                    self.assertEqual(result, CASES["relay-error-scope-readonly"]["result"])
        self.assertFalse(self.mutations)

    async def test_stop_is_admitted_while_acp_target_probe_is_blocked(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release, returned = Event(), Event()
        stamp = studio_selections.target_stamp
        self.session.cancel_native = AsyncMock()

        def blocked_stamp(path, *, project=False):
            if project:
                loop.call_soon_threadsafe(entered.set)
                release.wait(2)
                returned.set()
            return stamp(path, project=project)

        with patch.object(studio_selections, "target_stamp", side_effect=blocked_stamp):
            listing = asyncio.create_task(self.call("selections-list-tool-first"))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.assertEqual(await self.server.dispatch("session/cancel", {"sessionId": self.session.id}), {})
                self.session.cancel_native.assert_awaited_once()
                self.assertFalse(returned.is_set())
            finally:
                release.set()
                await listing

    @asynccontextmanager
    async def saturated_probe_pool(self):
        slots = self.server.selection_target_io.slots
        for _ in range(4):
            await slots.acquire()
        waiting = asyncio.Event()
        acquire = slots.acquire

        async def observe_wait():
            waiting.set()
            return await acquire()

        try:
            with patch.object(slots, "acquire", observe_wait):
                yield waiting
        finally:
            for _ in range(4):
                slots.release()

    async def test_stop_and_close_release_write_admission_while_probe_slots_are_held(self):
        params = {"sessionId": self.session.id}
        self.session.cancel_native = AsyncMock()
        self.session.prompt = AsyncMock(return_value={"stopReason": "end_turn"})
        self.server.close_session = AsyncMock(return_value={})
        for index, method in enumerate(("session/cancel", "session/close"), start=1):
            with self.subTest(method=method):
                payload = {**CASES["selections-set-skill"]["post"]["payload"],
                           "requestId": f"019aa123-0001-7000-8000-{index:012x}"}
                write = None
                try:
                    async with self.saturated_probe_pool() as waiting:
                        write = asyncio.create_task(self.call("selections-set-skill", payload=payload))
                        await asyncio.wait_for(waiting.wait(), 5)
                        if method == "session/close":
                            with self.assertRaises(ProtocolError) as busy:
                                await self.server.dispatch(method, params)
                            self.assertEqual(busy.exception.code, -32000)
                        else:
                            await self.server.dispatch(method, params)
                        with self.assertRaises(ProtocolError) as retired:
                            await asyncio.wait_for(asyncio.shield(write), 1)
                        self.assertEqual(retired.exception.code, -32004)
                        self.assertEqual(await self.server.dispatch("session/prompt", {
                            **params, "prompt": [{"type": "text", "text": "next"}],
                        }), {"stopReason": "end_turn"})
                        self.assertEqual(await self.server.dispatch("session/close", params), {})
                finally:
                    if write is not None:
                        await asyncio.gather(write, return_exceptions=True)
        self.assertFalse(self.mutations)

    async def test_selection_write_returns_busy_when_probe_slot_wait_times_out(self):
        async with self.saturated_probe_pool():
            with patch.object(studio_selections, "TARGET_PROBE_WAIT", 0):
                self.assertEqual(await asyncio.wait_for(self.call("selections-set-skill"), 1),
                                 CASES["relay-error-busy"]["result"])
        self.assertFalse(self.mutations)

    async def test_retired_write_skips_slot_wait_after_native_preflight_reply(self):
        requests = []

        def hold_snapshot(request):
            requests.append(request)
            self.requested.set()

        self.controls["selections/list"] = hold_snapshot
        self.session.cancel_native = AsyncMock()
        write = None
        try:
            async with self.saturated_probe_pool() as waiting:
                write = asyncio.create_task(self.call("selections-set-skill"))
                await asyncio.wait_for(self.requested.wait(), 5)
                await self.server.dispatch("session/cancel", {"sessionId": self.session.id})
                self.reply(requests[0], self.snapshots["skill"])
                with self.assertRaises(ProtocolError) as retired:
                    await asyncio.wait_for(asyncio.shield(write), 1)
                self.assertEqual(retired.exception.code, -32004)
                self.assertFalse(waiting.is_set())
        finally:
            if write is not None:
                await asyncio.gather(write, return_exceptions=True)
        self.assertFalse(self.mutations)

    @asynccontextmanager
    async def park_resource_write(self, stage):
        if stage == "probe-slot":
            async with self.saturated_probe_pool() as entered:
                yield entered
            return
        entered = asyncio.Event()
        if stage == "recovery":
            async def hold_recovery(**_):
                entered.set()
                await asyncio.Future()

            with patch.multiple(self.session, compaction_recovery_needed=True, rpc_closed=True, proc=None,
                                recover_after_compaction=hold_recovery):
                yield entered
            return
        if stage == "selection-lock":
            lock = self.session.studio_resources.selection_lock
            acquire = lock.acquire

            async def observe_lock():
                entered.set()
                await acquire()

            async with lock:
                with patch.object(lock, "acquire", observe_lock):
                    yield entered
            return
        if stage in ("target-probe", "version-probe"):
            loop = asyncio.get_running_loop()
            release = Event()
            owner, name = ((studio_selections, "project_target") if stage == "target-probe"
                           else (studio_selections.TargetFence, "require_version"))
            probe = getattr(owner, name)

            def hold_probe(*args):
                loop.call_soon_threadsafe(entered.set)
                release.wait(10)
                return probe(*args)

            try:
                with patch.object(owner, name, hold_probe):
                    yield entered
            finally:
                release.set()
            return
        requests = []

        def hold_reply(request):
            requests.append(request)
            entered.set()

        replies = {
            "selections/list": self.snapshots["skill"],
            "resources/list": {"tools": [{"name": str(self.root / "tool")}]},
            "agents/show": {},
        }
        try:
            with patch.dict(self.controls, {stage: hold_reply}):
                yield entered
        finally:
            for request in requests:
                self.reply(request, replies[stage])

    async def test_retirement_releases_all_pre_dispatch_resource_waits(self):
        self.session.cancel_native = AsyncMock()
        self.session.save = AsyncMock()
        self.server.close_session = AsyncMock(return_value={})
        self.session.controls.capabilities["methods"] += ["agents/show", "agents/use", "resources/reload"]
        params = {"sessionId": self.session.id}
        raw_tool = str(self.root / "tool")
        opaque_tool = self.relay.identifiers.key("tool", raw_tool)

        async def prompt(blocks, *_):
            self.session.reset_cancellation()
            if blocks[0]["text"].startswith("/tools"):
                await commands.execute(self.session, blocks[0]["text"])
            return {"stopReason": "end_turn"}

        self.session.prompt = prompt
        stages = (
            ("selection-lock", "selections-set-skill"),
            ("recovery", "selections-set-skill"),
            ("probe-slot", "selections-set-skill"),
            ("target-probe", "selections-set-skill"),
            ("version-probe", "selections-set-skill"),
            ("selections/list", "selections-set-skill"),
            ("resources/list", "tools-set-empty"),
            ("agents/show", "profiles-use"),
            ("resources/list", "legacy"),
            ("selection-lock", "slash"),
            ("resources/list", "slash"),
        )
        cases = [(stage, write_case, retire) for stage, write_case in stages
                 for retire in ("session/cancel", "session/close")]
        cases += [("target-probe", "selections-set-skill", retire)
                  for retire in ("binding-retire", "session/load", "resources/reload")]
        for index, (stage, write_case, retire) in enumerate(cases, start=1):
            with self.subTest(stage=stage, write=write_case, retire=retire):
                self.session.studio_resources.retired_binding = None
                self.calls.clear()
                if write_case == "legacy":
                    request = self.server.dispatch("_aelix/manage", {
                        **params, "action": "tools/set_active", "arguments": {"names": [opaque_tool]},
                    })
                elif write_case == "slash":
                    request = self.server.dispatch("session/prompt", {
                        **params, "prompt": [{"type": "text", "text": f"/tools {opaque_tool}"}],
                    })
                else:
                    payload = {**CASES[write_case]["post"]["payload"],
                               "requestId": f"019aa123-0001-7000-8000-{index:012x}"}
                    request = self.call(write_case, payload=payload)
                write = None
                try:
                    async with self.park_resource_write(stage) as entered:
                        write = asyncio.create_task(request)
                        await asyncio.wait_for(entered.wait(), 2)
                        if retire == "binding-retire":
                            self.session.studio_resources.retire_replaced_approval("replacement")
                        elif retire == "session/load":
                            with self.assertRaises(ProtocolError):
                                await self.server.dispatch(retire, {
                                    **params, "cwd": self.session.cwd,
                                    "_meta": {session_relay.META_KEY: {"bindingId": self.relay.binding_id}},
                                })
                        elif retire == "resources/reload":
                            with self.assertRaises(ProtocolError):
                                await self.server.dispatch("_aelix/manage", {**params, "action": retire})
                        elif retire == "session/close" and self.session.mutation_lock.locked():
                            with self.assertRaises(ProtocolError):
                                await self.server.dispatch(retire, params)
                        else:
                            await self.server.dispatch(retire, params)
                        with self.assertRaises(ProtocolError) as cancelled:
                            await asyncio.wait_for(asyncio.shield(write), 1)
                        self.assertEqual(cancelled.exception.code, -32004)
                        self.assertEqual(await self.server.dispatch("session/prompt", {
                            **params, "prompt": [{"type": "text", "text": "next"}],
                        }), {"stopReason": "end_turn"})
                        self.assertEqual(await self.server.dispatch("session/close", params), {})
                        self.assertFalse(any(call.get("method") in {
                            "selections/set", "tools/set_active", "agents/use",
                        } for call in self.calls))
                finally:
                    if write is not None:
                        await asyncio.gather(write, return_exceptions=True)

    async def test_stale_queued_catalogue_probe_never_touches_target(self):
        for invalidate in (self.relay.invalidate_list_pages,
                           lambda: self.session.studio_resources.retire_replaced_approval("b-22")):
            with patch.object(studio_selections, "project_target", wraps=studio_selections.project_target) as probe:
                async with self.saturated_probe_pool() as waiting:
                    listing = asyncio.create_task(self.call("selections-list-tool-first"))
                    await asyncio.wait_for(waiting.wait(), 5)
                    invalidate()
                self.assertEqual(await asyncio.wait_for(listing, 5), CASES["relay-error-unreadable"]["result"])
                probe.assert_not_called()
                self.assertFalse(self.relay.list_pages)
                self.assertFalse(self.server.selection_target_io.slots.locked())

    async def test_cancelled_probe_awaiter_does_not_free_the_running_threads_slot(self):
        pool = self.server.selection_target_io
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = Event()

        def blocked():
            loop.call_soon_threadsafe(entered.set)
            release.wait()

        for _ in range(3):
            await pool.slots.acquire()
        worker = asyncio.create_task(pool.run(blocked))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
            with patch.object(studio_selections, "TARGET_PROBE_WAIT", 0):
                with self.assertRaises(studio_selections.TargetProbeBusy):
                    await pool.run(lambda: self.fail("Probe started while every worker slot was occupied"))
        finally:
            release.set()
            # Wait for the worker to return its own permit before releasing
            # the other three reservations made by this test.
            await asyncio.wait_for(pool.slots.acquire(), 5)
            for _ in range(4):
                pool.slots.release()

    async def test_fresh_snapshot_is_not_sent_while_native_stop_is_pending(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pause_stop():
            entered.set()
            await release.wait()

        self.session.cancel_native = pause_stop
        await self.server.admit_cancel(None, {"sessionId": self.session.id})
        stop = self.server.cancel_tasks[self.session.id].task
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(await self.call("selections-list-tool-first"), CASES["relay-error-unreadable"]["result"])
            self.assertFalse(self.calls)
        finally:
            release.set()
            await stop
        self.assertIn("items", await self.call("selections-list-tool-first"))

    async def test_native_stop_reaches_worker_without_waiting_for_catalogue_reply(self):
        requests = []

        def hold_selection(request):
            requests.append(request)
            self.requested.set()

        self.session.controls.capabilities["methods"].append("commands/cancel")
        self.controls.update({"selections/list": hold_selection, "commands/cancel": lambda _: {},
                              "abort": lambda _: {}})
        listing = asyncio.create_task(self.call("selections-list-tool-first"))
        await asyncio.wait_for(self.requested.wait(), 5)
        try:
            await asyncio.wait_for(self.server.dispatch("session/cancel", {"sessionId": self.session.id}), 5)
            self.assertFalse(listing.done())
            self.assertEqual([call.get("method", call["type"]) for call in self.calls],
                             ["selections/list", "commands/cancel", "abort"])
        finally:
            self.reply(requests[0], self.snapshots["tool"])
            await listing

    async def test_project_directory_replaced_after_dispatch_is_outcome_unknown(self):
        project = self.root / ".aelix"
        project.mkdir(exist_ok=True)
        self.controls["selections/set"] = lambda _: self.requested.set()
        write = asyncio.create_task(self.call("selections-set-mcp"))
        await asyncio.wait_for(self.requested.wait(), 5)
        project.rename(self.root / "previous")
        project.mkdir()
        self.finish_set(self.selection_frame)
        with self.assertRaises(ProtocolError) as raised:
            await write
        self.assertEqual(raised.exception.code, session_relay.OUTCOME_UNKNOWN)
        self.assertTrue(self.session.studio_resources.uncertain)

    async def test_global_file_change_during_save_is_outcome_unknown(self):
        self.controls["selections/set"] = lambda _: self.requested.set()
        write = asyncio.create_task(self.call("selections-set-mcp"))
        await asyncio.wait_for(self.requested.wait(), 5)
        self.global_file.write_bytes(b'{"unexpected": true}')
        self.finish_set(self.selection_frame)
        with self.assertRaises(ProtocolError) as raised:
            await write
        self.assertEqual(raised.exception.code, session_relay.OUTCOME_UNKNOWN)
        self.assertTrue(self.session.studio_resources.uncertain)

    async def test_project_file_hardlinked_to_global_is_readonly(self):
        target = self.root / ".aelix/mcp.json"
        target.unlink()
        target.hardlink_to(self.global_file)
        self.assertEqual(await self.call("selections-set-mcp"), CASES["relay-error-scope-readonly"]["result"])
        self.assertFalse(self.mutations)

    async def test_different_path_spellings_for_the_same_file_refuse_before_dispatch(self):
        target = self.root / ".aelix/mcp.json"
        alias = target.with_name("MCP.JSON")
        if alias == target or not alias.exists() or not alias.samefile(target):
            self.skipTest("Requires a filesystem with distinct path spellings for the same file")
        self.snapshots["mcp"]["scopes"]["global"]["path"] = str(alias)
        self.assertEqual(await self.call("selections-set-mcp"), CASES["relay-error-scope-readonly"]["result"])
        self.assertFalse(self.mutations)

    async def test_deferred_tool_apply_fences_prompts_with_an_override(self):
        self.session.active_tools = []
        self.active = []
        self.writes["tool"]["apply"].update(applied=False, deferred=True, turnedOff=[])
        result = await self.call("selections-set-tool")
        self.assertTrue(result["accepted"])
        self.assertTrue(result["apply"]["deferred"])
        self.assertTrue(self.session.studio_resources.uncertain)
        self.assertFalse(any(call.get("method") == "tools/set_active" for call in self.calls))
        self.assertEqual(self.session.active_tools, [])
