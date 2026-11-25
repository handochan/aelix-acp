"""D16 v1 selection vectors over the ACP relay and native JSONL boundary."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aelix_acp.runtime import profile_reselection, session_relay, studio_selections
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
        method = request["method"]
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
        keys = ["\U00010000", "\ue000", "한"] + [f"key-{index:03}" for index in range(100)]
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
            {**row, "key": str(index), "label": "한" * 40000} for index in range(3)]
        first = await self.call(case)
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(session_relay.fits_resource_reply(first))
        last = await self.call(case, payload={"kind": "skill", "cursor": first["nextCursor"]})
        self.assertEqual(len(last["items"]), 1)
        self.assertIsNone(last["nextCursor"])
        self.snapshots["skill"]["items"] = [{**row, "label": "한" * 90000}]
        too_large = CASES["relay-error-large-item"]["result"]
        self.assertEqual(await self.call(case), too_large)
        self.snapshots["tool"]["items"] = []
        self.active = ["한" * 90000]
        self.assertEqual(await self.call("selections-list-tool-first"), too_large)

    async def test_tool_page_cannot_mix_rows_with_a_concurrent_tool_override(self):
        catalog_read = asyncio.Event()
        held_reads, held_writes = [], []
        inventory = self.controls["resources/list"]

        def read_inventory(request):
            if not held_reads:
                held_reads.append(request)
                catalog_read.set()
                return None
            return inventory(request)

        def change_tools(request):
            held_writes.append(request)
            self.active = request["params"]["names"]
            self.requested.set()

        self.controls.update({"resources/list": read_inventory, "tools/set_active": change_tools})
        self.session.save = AsyncMock()
        listing = asyncio.create_task(self.call("selections-list-tool-first"))
        await asyncio.wait_for(catalog_read.wait(), 5)
        write = asyncio.create_task(self.call("tools-set-empty"))
        entered = asyncio.create_task(self.requested.wait())
        done, _ = await asyncio.wait((write, entered), timeout=5, return_when=asyncio.FIRST_COMPLETED)
        self.assertTrue(done)
        self.reply(held_reads[0], inventory(held_reads[0]))
        result = await listing
        if held_writes:
            self.reply(held_writes[0], {"activeTools": []})
        await write
        entered.cancel()
        await asyncio.gather(entered, return_exceptions=True)
        self.assertTrue(result["items"][0]["live"])
        self.assertEqual(result["activeTools"], ["read"])
        self.assertIsNone(result["toolOverride"])

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
