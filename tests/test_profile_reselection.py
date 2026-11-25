"""D16 decision 2 through ACP stdio and a synthetic guarded native worker."""
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from aelix_acp.runtime.profile_reselection import APPROVAL_SECONDS
from support import AcpClient
from test_session_relay import initialize_offer, META
from test_studio_resources import CASES, QUIET

MANUAL_APPROVAL_ENV = "AELIX_ACP_STUDIO_MANUAL_PROFILE_APPROVAL"

GUARDED_CHILD = r'''
import hashlib
import json
import os
from pathlib import Path
import sys
import time

root = Path.cwd()
Path(os.environ["AELIX_ACP_READY_FILE"]).write_text(os.environ["AELIX_ACP_NONCE"])
mode = sys.argv[sys.argv.index("--permission-mode") + 1]
methods = ["agents/show", "agents/use", "permissions/get", "permissions/set", "tools/set_active", "resources/list", "resources/reload"]
active = None
tools = ["read"]

def read_profile():
    project = root / ".aelix/agents/quiet.md"
    scope, path = ("project", project) if project.exists() else ("user", root / "agent/agents/quiet.md")
    raw = path.read_bytes()
    revision = hashlib.sha256(("studio-profile/1\0" + scope + "\0" + path.as_uri() + "\0").encode() + raw).hexdigest()
    allowed = ["read"] if b"[read]" in raw else []
    profile = {
        "name": "quiet", "scope": scope, "file_path": str(path),
        "description": "Review only", "body": raw.decode().split("---\n")[2].strip(),
        "system_prompt": "append", "model": None, "provider": None, "tools": allowed,
        "skills": [], "approval_mode": "inherit", "builtin_tools": True, "exclude_tools": [],
        "inherit_skills": True, "extensions": [], "inherit_extensions": False, "context_files": True,
        "thinking": None, "role": "leaf", "output_cap": 51200, "timeout_ms": None, "agents": None,
    }
    return {"name": "quiet", "scope": scope, "revision": "sha256:" + revision}, profile

def log(name, data):
    with Path("worker.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps([name, data]) + "\n")

for line in sys.stdin:
    frame = json.loads(line)
    name = frame.get("method", frame["type"])
    params = frame.get("params", {})
    log(name, params)
    result = {}
    success = True
    if name == "get_state":
        result = {"messageCount": 0, "steeringMode": "one-at-a-time", "followUpMode": "all"}
    elif name == "get_messages":
        result = {"messages": []}
    elif name == "capabilities":
        result = {"version": 1, "methods": methods, "activeAgent": active}
        if not Path("unguarded").exists():
            result["profileRefGuard"] = 1
    elif name == "permissions/get":
        result = {"mode": mode}
    elif name == "permissions/set":
        mode = params["mode"]
        result = {"mode": mode}
    elif name == "resources/list":
        if Path("pause-inventory").exists():
            Path("inventory-blocked").touch()
            deadline = time.monotonic() + 8
            while Path("pause-inventory").exists() and time.monotonic() < deadline:
                time.sleep(0.005)
        result = {"tools": [{"name": "read"}], "activeTools": tools}
    elif name == "resources/reload":
        tools = ["read"]
        result = {"cancelled": False}
    elif name == "tools/set_active":
        tools = ["read"] if Path("wrong-restore-tools").exists() else params["names"]
        if Path("pause-tool-ack").exists():
            Path("tool-ack-blocked").touch()
            deadline = time.monotonic() + 8
            while Path("pause-tool-ack").exists() and time.monotonic() < deadline:
                time.sleep(0.005)
        result = {"activeTools": tools}
    elif name == "agents/show":
        if Path("pause-show").exists():
            Path("inspecting").touch()
            deadline = time.monotonic() + 8
            while Path("pause-show").exists() and time.monotonic() < deadline:
                time.sleep(0.005)
        try:
            ref, profile = read_profile()
            result = {"profile": profile}
            if not Path("unguarded").exists():
                profile["ref"] = ref
        except FileNotFoundError:
            success = False
    elif name == "agents/use":
        ref, profile = read_profile()
        if "expectedProfile" in params and params["expectedProfile"] != ref:
            result = {"accepted": False, "reason": "profile-changed", "currentRef": ref}
        else:
            if Path("replace-after-guard").exists():
                Path(profile["file_path"]).write_bytes(Path("replacement").read_bytes())
            active = ref["name"]
            tools = profile["tools"]
            log("profile-applied", {"ref": ref, "tools": tools})
            if Path("pause-apply").exists():
                Path("applied-blocked").touch()
                deadline = time.monotonic() + 8
                while Path("pause-apply").exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
            result = {"message": "Agent profile selected"}
            if not Path("unguarded").exists():
                result["activeRef"] = ref
    response = {"type": "response", "command": frame["type"], "success": success, "id": frame["id"], "data": result}
    if not success:
        response["error"] = "profile not found"
    print(json.dumps(response), flush=True)
'''


PUBLICATION_GATES = """
import asyncio
from pathlib import Path
import time
from aelix_acp.runtime import store
from aelix_acp.server import Server, main

original_store = store._write_store
def write_store(directory, write, args, check_commit):
    if check_commit is None or write is not store.save_session or not Path("pause-commit").exists():
        return original_store(directory, write, args, check_commit)

    def blocked():
        pause = Path("pause-commit")
        Path("commit-blocked").touch()
        deadline = time.monotonic() + 8
        while pause.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        check_commit()
    return original_store(directory, write, args, blocked)

original_send = Server.send
async def send(server, message, **kwargs):
    result = message.get("result")
    binding = result.get("_meta", {}).get("aelix/sessionRelay", {}) if isinstance(result, dict) else {}
    if binding.get("bindingId") == "b-22" and Path("pause-publication").exists():
        Path("publication-blocked").touch()
        async with asyncio.timeout(8):
            while Path("pause-publication").exists():
                await asyncio.sleep(0.005)
    return await original_send(server, message, **kwargs)

original_dispatch = Server.dispatch
async def dispatch(server, method, params, **kwargs):
    if method == "fixture/require-compaction-recovery":
        server.session(params).compaction_recovery_needed = True
        return {}
    if method == "fixture/receipt":
        relay = server.session(params).input_relay
        return await relay.lookup({"requestId": params["requestId"]})
    return await original_dispatch(server, method, params, **kwargs)

store._write_store = write_store
Server.send = send
Server.dispatch = dispatch
main()
"""


class ProfileReselectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="acp-d16-", dir="/tmp" if os.name != "nt" else None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "home").mkdir()
        self.user = self.root / "agent/agents/quiet.md"
        self.project = self.root / ".aelix/agents/quiet.md"
        self.user.parent.mkdir(parents=True)
        self.project.parent.mkdir(parents=True)
        self.old_ref = self.write_profile(self.user, 0)
        self.script = self.root / "guarded.py"
        self.script.write_text(GUARDED_CHILD, encoding="utf-8")
        self.client = await self.start_client()
        self.session = (await self.client.request("session/new", self.open_params("b-21")))["sessionId"]
        self.original = self.write_record()

    async def start_client(self, *, manual=True, inherited_flag=False):
        # Exercise future authenticated-host behavior only inside the fixture
        # process. The adapter itself has no configuration that grants it.
        fixture_hook = "r.host_verified_manual_approval=lambda operation: True;" if manual else ""
        entry = "from aelix_acp.runtime import profile_reselection as r;r.APPROVAL_SECONDS=1.5;" + fixture_hook
        entry += PUBLICATION_GATES
        client = await AcpClient.start(
            self.root, sys.executable, prefix=[str(self.script)], capabilities=initialize_offer()["clientCapabilities"],
            extra_env={MANUAL_APPROVAL_ENV: "1"} if inherited_flag else None,
            entry=("-c", entry), adapter_args=("--max-sessions", "1"),
        )
        self.addAsyncCleanup(client.close)
        return client

    def write_profile(self, path, index, suffix=b""):
        vector = CASES["profile-ref"]["vectors"][index]
        raw = vector["rawUtf8"].encode() + suffix
        path.write_bytes(raw)
        scope = vector["ref"]["scope"]
        prefix = ("studio-profile/1\0" + scope + "\0" + path.as_uri() + "\0").encode()
        return {**vector["ref"], "revision": "sha256:" + hashlib.sha256(prefix + raw).hexdigest()}

    def preferences(self, update=None):
        with closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db, db:
            current = json.loads(db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session,)).fetchone()[0])
            if update is not None:
                current.update(update)
                db.execute("UPDATE preferences SET value=? WHERE session_id=?", (json.dumps(current), self.session))
            return current

    def write_record(self):
        return self.preferences({
            **CASES["load-drift-open"]["stored"], "agentRef": self.old_ref,
            "agentPreview": {**QUIET, "ref": self.old_ref}, "permission_mode": "yolo",
        })

    def open_params(self, binding):
        return {"cwd": str(self.root), "mcpServers": [], "_meta": {META: {"bindingId": binding}}}

    async def load(self):
        return await self.client.request("session/load", {**self.open_params("b-22"), "sessionId": self.session})

    def calls(self, name):
        return [data for method, data in map(json.loads, (self.root / "worker.jsonl").read_text().splitlines())
                if method == name]

    def permission(self, candidate, example="load-drift-open"):
        cards = [request for request in self.client.requests if request["method"] == "session/request_permission"]
        self.assertEqual(len(cards), 1)
        params = cards[0]["params"]
        expected = CASES[example]["acpToClient"]["params"]
        self.assertEqual(params["sessionId"], self.session)
        self.assertEqual([item["kind"] for item in params["options"]], [item["kind"] for item in expected["options"]])
        self.assertEqual([item["optionId"] for item in params["options"]], [item["optionId"] for item in expected["options"]])
        raw = params["toolCall"]["rawInput"]
        self.assertEqual(raw["recorded"], self.old_ref)
        self.assertEqual(raw["candidate"], candidate)
        for value in (self.old_ref["revision"], candidate["revision"]):
            self.assertIn(value, raw["description"])
        return raw

    async def assert_load_error(self, example):
        with self.assertRaises(RuntimeError) as failure:
            await self.load()
        actual = json.loads(str(failure.exception))
        expected = CASES[example]["response"]["error"]
        if "recovery" in expected["data"] and expected["data"]["recovery"]["original"] is not None:
            expected = {**expected, "data": {**expected["data"], "recovery": {
                **expected["data"]["recovery"], "original": self.old_ref,
            }}}
        self.assertEqual(actual, expected)
        self.assertEqual(self.preferences(), self.original)
        self.assertEqual(self.calls("profile-applied"), [])
        self.assertEqual(self.calls("prompt"), [])
        return actual

    async def test_load_allow_preserves_yolo_and_explicit_empty_override(self):
        candidate = self.write_profile(self.project, 1)
        async def allow(_):
            self.assertEqual(len(self.calls("agents/use")), CASES["load-drift-open"]["assert"]["nativeUseCallsBeforeAnswer"])
            return "allow-once"
        self.client.permission = allow
        result = await self.load()
        example = CASES["load-drift-allow"]
        self.assertEqual(result["_meta"][META], example["loadSuccessFragment"]["result"]["_meta"][META])
        raw = self.permission(candidate)
        expected_diff = CASES["load-drift-open"]["acpToClient"]["params"]["toolCall"]["rawInput"]["differences"]
        self.assertEqual(raw["differences"], [expected_diff[0], {"field": "revision", "before": self.old_ref["revision"],
                                                               "after": candidate["revision"]}, expected_diff[2]])
        stored = self.preferences()
        self.assertEqual(stored["agentRef"], candidate)
        self.assertEqual(stored["agentPreview"], {**example["storedAfter"]["agentPreview"], "ref": candidate})
        self.assertEqual(stored["activeTools"], [])
        self.assertEqual(stored["permission_mode"], "yolo")
        self.assertEqual(self.calls("agents/use"), [{"name": "quiet", "expectedProfile": candidate}])
        self.assertEqual(self.calls("tools/set_active"), [{"names": []}])
        self.assertEqual(self.calls("prompt"), [])

    async def test_load_recorded_match_without_approval(self):
        await self.load()
        self.assertEqual(len(self.client.requests), CASES["load-recorded-match"]["assert"]["permissionCards"])
        self.assertEqual(self.calls("agents/use"), [{"name": "quiet", "expectedProfile": self.old_ref}])
        self.assertEqual(self.preferences()["activeTools"], [])

    async def test_load_refusal_cancel_and_silence_keep_original(self):
        candidate = self.write_profile(self.project, 1)
        self.assertEqual(APPROVAL_SECONDS * 1000, CASES["load-drift-silence"]["clockAdvanceMs"])
        wait = asyncio.Event()

        async def silent(_):
            await wait.wait()
            return "allow-once"

        for case, answer in (("load-drift-reject", lambda _: "reject-once"),
                             ("load-drift-cancel", lambda _: None), ("load-drift-silence", silent)):
            with self.subTest(case=case):
                self.client.requests.clear()
                self.client.permission = answer
                await self.assert_load_error(case)
                self.permission(candidate)
                self.assertEqual(len(self.calls("agents/use")), CASES[case]["assert"]["nativeUseCalls"])
        cancelled = CASES["load-drift-silence"]["withdraw"]
        await self.wait_until(lambda: any(message["method"] == cancelled["method"] and
                              message["params"]["requestId"] == self.client.requests[0]["id"]
                              for message in self.client.updates))
        wait.set()
        await asyncio.sleep(0)
        self.assertEqual(self.calls("agents/use"), [])

    async def test_load_change_during_permission_refuses_without_reasking(self):
        candidate = self.write_profile(self.project, 1)

        def replace(_):
            self.project.unlink()
            self.write_profile(self.user, 2)
            return "allow-once"

        self.client.permission = replace
        await self.assert_load_error("load-drift-raced")
        self.permission(candidate)
        self.assertEqual(self.calls("agents/use"), [{"name": "quiet", "expectedProfile": candidate}])

    async def test_load_uses_snapshot_when_file_changes_after_native_guard(self):
        candidate = self.write_profile(self.project, 1)
        (self.root / "replace-after-guard").touch()
        (self.root / "replacement").write_bytes(CASES["profile-ref"]["vectors"][3]["rawUtf8"].encode())
        await self.load()
        self.assertEqual(self.calls("profile-applied"), [{"ref": candidate, "tools": ["read"]}])
        self.assertEqual(self.preferences()["agentRef"], candidate)
        self.permission(candidate)

    async def test_missing_and_invalid_records_have_no_approval(self):
        self.user.unlink()
        await self.assert_load_error("load-profile-missing")
        self.original = self.preferences(CASES["load-bad-record"]["stored"])
        await self.assert_load_error("load-bad-record")
        self.assertFalse(self.client.requests)
        self.assertEqual(self.calls("agents/use"), [])

    async def test_stale_use_then_explicit_reselection_and_dedup(self):
        await self.load()
        self.client.requests.clear()
        candidate = self.write_profile(self.project, 1)
        # The successful load changed the live binding to b-22.
        for case, picked in (("use-stale-preview", self.old_ref), ("use-recorded-drift", candidate)):
            post = CASES[case]["post"]
            params = {"sessionId": self.session, "bindingId": "b-22", "payload": {**post["payload"], "profile": picked}}
            first, replay = await asyncio.gather(*(self.client.request(post["method"], params) for _ in range(2)))
            self.assertEqual(first, replay)
            expected = CASES[case]["result"]
            if case == "use-stale-preview":
                self.assertEqual(first, {**expected, "recorded": self.old_ref, "candidate": candidate,
                    "differences": [expected["differences"][0], {"field": "revision",
                        "before": self.old_ref["revision"], "after": candidate["revision"]}, expected["differences"][2]],
                    "recovery": {**expected["recovery"], "original": self.old_ref}})
                self.assertFalse(self.client.requests)
            else:
                self.assertEqual(first, {**expected, "activeRef": candidate})
        self.permission(candidate)
        self.assertEqual(self.preferences()["agentRef"], candidate)
        self.assertIsNone(self.preferences()["activeTools"])
        self.assertEqual(len(self.calls("agents/use")), 2)

    async def test_live_permission_refusals_and_race_keep_saved_selection(self):
        await self.load()
        original = self.preferences()
        candidate = self.write_profile(self.project, 1)
        post = CASES["use-recorded-drift"]["post"]
        cases = (("use-permission-deny", lambda _: "reject-once"),
                 ("use-permission-cancel", lambda _: None),
                 ("use-permission-timeout", lambda _: "allow-always"),
                 ("use-changed-again", self.replace_during_permission))
        for number, (case, answer) in enumerate(cases):
            with self.subTest(case=case):
                self.client.requests.clear()
                self.client.permission = answer
                result = await self.client.request(post["method"], {
                    "sessionId": self.session, "bindingId": "b-22", "payload": {
                        "profile": candidate, "requestId": f"019aa123-0001-7000-8000-{number:012x}",
                    },
                })
                expected = CASES[case]["result"]
                self.assertEqual(result["reason"], expected["reason"])
                self.assertEqual(result["recovery"], {**expected["recovery"], "original": self.old_ref})
                if "cause" in expected:
                    self.assertEqual(result["cause"], expected["cause"])
                else:
                    self.assertEqual(result["recorded"], candidate)
                    self.assertEqual(result["candidate"], self.replacement_ref)
                    self.assertEqual([row["field"] for row in result["differences"]],
                                     [row["field"] for row in expected["differences"]])
                self.permission(candidate)
                self.assertEqual(self.preferences(), original)
        self.assertEqual(len(self.calls("profile-applied")), 1)
        self.assertEqual(self.calls("prompt"), [])

    def replace_during_permission(self, _):
        self.project.unlink()
        self.replacement_ref = self.write_profile(self.user, 2)
        return "allow-once"

    async def test_same_source_prompt_tail_is_named_in_permission(self):
        long_prompt = b"\n" + b"A" * 600
        self.old_ref = self.write_profile(self.user, 0, long_prompt + b"1")
        self.preferences({"agentRef": self.old_ref, "agentPreview": {
            **QUIET, "ref": self.old_ref,
            "promptPreview": ("Review only.\n\n" + "A"*600)[:512], "promptTruncated": True,
            "omittedPromptDigest": hashlib.sha256(("Review only.\n\n" + "A"*600 + "1")[512:].encode()).hexdigest(),
        }})
        candidate = self.write_profile(self.user, 0, long_prompt + b"2")
        await self.load()
        raw = self.permission(candidate)
        self.assertIn("The omitted portion of the prompt changed.", raw["description"])
        self.assertEqual([field["field"] for field in raw["differences"]], ["revision", "omittedPrompt"])


    async def wait_until(self, check):
        async with asyncio.timeout(6):
            while not check():
                await asyncio.sleep(0.01)

    async def test_stop_during_starting_inspection_returns_recovery(self):
        self.write_profile(self.project, 1)
        paused = self.root / "pause-show"
        paused.touch()
        loading = asyncio.create_task(self.assert_load_error("load-drift-cancel"))
        await self.wait_until(lambda: (self.root / "inspecting").exists())
        try:
            # Send Stop on the wire while native is blocked. Processing it
            # must not depend on native answering the inspection.
            stopping = asyncio.create_task(self.client.request("session/cancel", {"sessionId": self.session}))
            await self.wait_until(lambda: any(identifier > 3 for identifier in self.client.pending))
            # The server handles Stop admission before reading this request.
            await self.client.request("session/list", {})
        finally:
            paused.unlink()
        await stopping
        await loading
        self.assertFalse(self.client.requests)
        self.assertEqual(self.calls("agents/use"), [])

    async def test_replacement_withdraws_card_before_busy_response_and_late_allow(self):
        candidate = self.write_profile(self.project, 1)
        release = asyncio.Event()

        async def delayed(_):
            await release.wait()
            return "allow-once"

        self.client.permission = delayed
        loading = asyncio.create_task(self.assert_load_error("load-drift-cancel"))
        await self.wait_until(lambda: bool(self.client.requests))
        self.permission(candidate)
        with self.assertRaisesRegex(RuntimeError, "already starting"):
            await self.client.request("session/load", {**self.open_params("b-new"), "sessionId": self.session})
        await loading
        await self.wait_until(lambda: any(update["method"] == "$/cancel_request" for update in self.client.updates))
        release.set()
        self.assertEqual(self.calls("agents/use"), [])

    async def test_unguarded_restore_detects_post_apply_change(self):
        (self.root / "unguarded").touch()
        (self.root / "replace-after-guard").touch()
        (self.root / "replacement").write_bytes(CASES["profile-ref"]["vectors"][2]["rawUtf8"].encode())
        with self.assertRaises(RuntimeError) as failure:
            await self.load()
        self.assertEqual(json.loads(str(failure.exception))["data"]["reason"], "changed-again")
        self.assertEqual(self.calls("agents/use"), [{"name": "quiet"}])
        self.assertEqual(len(self.calls("profile-applied")), 1)
        self.assertEqual(self.preferences(), self.original)

    async def test_restore_rejects_nonempty_ack_for_empty_override(self):
        (self.root / "wrong-restore-tools").touch()
        with self.assertRaises(RuntimeError) as failure:
            await self.load()
        self.assertEqual(json.loads(str(failure.exception))["data"]["kind"], "restrictions_unenforceable")
        self.assertEqual(self.calls("tools/set_active"), [{"names": []}])
        self.assertEqual(self.preferences(), self.original)


    async def test_unverified_host_cannot_supply_approval(self):
        await self.client.close()
        self.client = await self.start_client(manual=False)
        self.write_profile(self.project, 1)
        await self.assert_load_error("load-drift-silence")
        self.assertFalse(self.client.requests)
        self.assertEqual(self.calls("agents/use"), [])


    async def test_inherited_manual_approval_flag_cannot_enable_a_card(self):
        await self.client.close()
        self.client = await self.start_client(manual=False, inherited_flag=True)
        self.write_profile(self.project, 1)
        await self.assert_load_error("load-drift-silence")
        self.assertFalse(self.client.requests)
        self.assertEqual(self.calls("agents/use"), [])

    async def retire_paused_apply(self, pending, *, busy_message):
        paused = self.root / "pause-apply"
        try:
            await self.wait_until(lambda: (self.root / "applied-blocked").exists())
            with self.assertRaisesRegex(RuntimeError, busy_message):
                await self.client.request("session/load", {**self.open_params("b-replacement"),
                                                           "sessionId": self.session})
        finally:
            paused.unlink(missing_ok=True)
        return await pending

    async def test_load_retired_mid_apply_refuses_publication_and_preserves_record(self):
        self.write_profile(self.project, 1)
        (self.root / "pause-apply").touch()
        pending = asyncio.create_task(self.load())
        with self.assertRaises(RuntimeError) as failure:
            await self.retire_paused_apply(pending, busy_message="already starting")
        data = json.loads(str(failure.exception))["data"]
        self.assertEqual(data["reason"], "outcome-unknown")
        self.assertEqual(data["recovery"]["original"], self.old_ref)
        self.assertEqual(len(self.calls("profile-applied")), 1)
        self.assertEqual(self.preferences(), self.original)

    async def test_live_selection_retired_mid_apply_has_unknown_outcome_and_preserves_record(self):
        await self.load()
        saved = self.preferences()
        candidate = self.write_profile(self.project, 1)
        (self.root / "pause-apply").touch()
        post = CASES["use-recorded-drift"]["post"]
        pending = asyncio.create_task(self.client.request(post["method"], {
            "sessionId": self.session, "bindingId": "b-22", "payload": {
                **post["payload"], "profile": candidate,
            },
        }))
        with self.assertRaises(RuntimeError) as failure:
            await self.retire_paused_apply(pending, busy_message="Session is busy")
        self.assertEqual(json.loads(str(failure.exception))["code"], -33100)
        self.assertEqual(len(self.calls("profile-applied")), 2)
        self.assertEqual(self.preferences(), saved)

    async def test_failed_live_reload_fences_the_next_prompt(self):
        await self.load()
        (self.root / "wrong-restore-tools").touch()
        with self.assertRaisesRegex(RuntimeError, "restrictions_unenforceable"):
            await self.client.request("_aelix/manage", {"sessionId": self.session, "action": "resources/reload"})
        resources = await self.client.request("_aelix/manage", {"sessionId": self.session, "action": "resources/list"})
        self.assertEqual(resources["activeTools"], ["read"])
        with self.assertRaisesRegex(RuntimeError, "Reopen the session"):
            await self.client.request("session/prompt", {"sessionId": self.session,
                                                         "prompt": [{"type": "text", "text": "/tools"}]})
        self.assertEqual(self.calls("prompt"), [])


    async def open_with_gate(self, name):
        self.write_profile(self.project, 1)
        pause = self.root / ("pause-" + name)
        pause.touch()
        self.addCleanup(pause.unlink, missing_ok=True)
        pending = asyncio.create_task(self.load())
        await self.wait_until(lambda: (self.root / (name + "-blocked")).exists())
        return pause, pending

    async def test_retirement_before_sqlite_commit_admission_rolls_back_restore(self):
        pause, pending = await self.open_with_gate("commit")
        try:
            with self.assertRaisesRegex(RuntimeError, "already starting"):
                await self.client.request("session/load", {**self.open_params("b-replacement"),
                                                           "sessionId": self.session})
        finally:
            pause.unlink()
        with self.assertRaises(RuntimeError) as error:
            await pending
        self.assertEqual(json.loads(str(error.exception))["data"]["reason"], "outcome-unknown")
        self.assertEqual(self.preferences(), self.original)

    async def test_pending_publication_blocks_prompt_and_rechecks_binding_after_commit(self):
        pause, pending = await self.open_with_gate("publication")
        try:
            with self.assertRaisesRegex(RuntimeError, "already starting"):
                await self.client.request("session/prompt", {"sessionId": self.session,
                                                             "prompt": [{"type": "text", "text": "/tools"}]})
            with self.assertRaisesRegex(RuntimeError, "already starting"):
                await self.client.request("session/load", {**self.open_params("b-replacement"),
                                                           "sessionId": self.session})
        finally:
            pause.unlink()
        with self.assertRaises(RuntimeError) as error:
            await pending
        self.assertEqual(json.loads(str(error.exception))["data"]["reason"], "outcome-unknown")
        self.assertEqual(self.preferences()["agentRef"]["scope"], "project")
        self.assertFalse(self.calls("prompt"))

    async def retire_before_release(self, pending, pause, blocked, *, busy_message):
        try:
            await self.wait_until(lambda: blocked.exists())
            with self.assertRaisesRegex(RuntimeError, busy_message):
                await self.client.request("session/load", {
                    **self.open_params("b-replacement"), "sessionId": self.session,
                })
        finally:
            pause.unlink(missing_ok=True)
        return await pending

    async def fail_compaction_restart(self):
        saved = self.preferences()
        await self.client.request("fixture/require-compaction-recovery", {"sessionId": self.session})
        pause = self.root / "pause-commit"
        pause.touch()
        pending = asyncio.create_task(self.client.request("session/prompt", {
            "sessionId": self.session, "prompt": [{"type": "text", "text": "/tools"}],
        }))
        with self.assertRaises(RuntimeError) as failure:
            await self.retire_before_release(pending, pause, self.root / "commit-blocked",
                                             busy_message="already starting")
        self.assertEqual(json.loads(str(failure.exception))["data"]["reason"], "outcome-unknown")
        self.assertEqual(self.preferences(), saved)

    async def assert_failed_compaction_restart_recoverable(self, *, close_first):
        await self.load()
        await self.fail_compaction_restart()
        if close_first:
            self.assertEqual(await self.client.request("session/close", {"sessionId": self.session}), {})
            with self.assertRaisesRegex(RuntimeError, "Unknown session"):
                await self.client.request("session/prompt", {
                    "sessionId": self.session, "prompt": [{"type": "text", "text": "/tools"}],
                })
        opened = await self.client.request("session/load", {
            **self.open_params("b-fresh"), "sessionId": self.session,
        })
        self.assertEqual(opened["_meta"][META], {"bindingId": "b-fresh"})
        self.assertEqual(await self.client.request("session/close", {"sessionId": self.session}), {})
        self.assertFalse(self.calls("prompt"))

    async def test_failed_compaction_restart_allows_fresh_load(self):
        await self.assert_failed_compaction_restart_recoverable(close_first=False)

    async def test_failed_compaction_restart_allows_close_then_load(self):
        await self.assert_failed_compaction_restart_recoverable(close_first=True)

    async def test_delete_releases_the_retained_compaction_session_slot(self):
        await self.load()
        await self.fail_compaction_restart()
        self.assertEqual(await self.client.request("session/delete", {"sessionId": self.session}), {})
        self.assertEqual((await self.client.request("session/list", {}))["sessions"], [])
        opened = await self.client.request("session/new", self.open_params("b-new"))
        self.assertNotEqual(opened["sessionId"], self.session)

    async def test_delete_rollback_releases_closed_session_lease_for_load_and_retry(self):
        await self.load()
        await self.fail_compaction_restart()
        saved = self.preferences()
        with closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            with db:
                db.execute("""
                    CREATE TRIGGER fail_partial_session_delete BEFORE DELETE ON sessions
                    WHEN NOT EXISTS (SELECT 1 FROM preferences WHERE session_id = OLD.id)
                    BEGIN SELECT RAISE(ABORT, 'fixture delete failure'); END
                """)
            try:
                with self.assertRaisesRegex(RuntimeError, "session_store_write_failed"):
                    await self.client.request("session/delete", {"sessionId": self.session})
            finally:
                with db:
                    db.execute("DROP TRIGGER fail_partial_session_delete")

        self.assertEqual(self.preferences(), saved)
        listed = await self.client.request("session/list", {})
        self.assertEqual([row["sessionId"] for row in listed["sessions"]], [self.session])
        opened = await self.client.request("session/load", {
            **self.open_params("b-after-rollback"), "sessionId": self.session,
        })
        self.assertEqual(opened["_meta"][META], {"bindingId": "b-after-rollback"})
        self.assertEqual(await self.client.request("session/delete", {"sessionId": self.session}), {})
        self.assertEqual((await self.client.request("session/list", {}))["sessions"], [])

    async def test_close_retires_tool_write_during_inventory_preflight(self):
        await self.load()
        saved = self.preferences()
        prior_writes = self.calls("tools/set_active")
        post = CASES["tools-set-active"]["post"]
        pause = self.root / "pause-inventory"
        pause.touch()
        pending = asyncio.create_task(self.client.request(post["method"], {
            "sessionId": self.session, "bindingId": "b-22", "payload": post["payload"],
        }))
        try:
            await self.wait_until(lambda: (self.root / "inventory-blocked").exists())
            with self.assertRaisesRegex(RuntimeError, "Session is busy"):
                await self.client.request("session/close", {"sessionId": self.session})
        finally:
            pause.unlink(missing_ok=True)
        with self.assertRaises(RuntimeError) as failure:
            await pending
        self.assertEqual(json.loads(str(failure.exception))["code"], -32004)
        self.assertEqual(self.calls("tools/set_active"), prior_writes)
        self.assertEqual(self.preferences(), saved)

    async def assert_retired_tool_write_is_unknown(self, pause_name, blocked_name):
        await self.load()
        saved = self.preferences()
        self.assertEqual(saved["activeTools"], [])
        post = CASES["tools-set-active"]["post"]
        pause = self.root / pause_name
        pause.touch()
        pending = asyncio.create_task(self.client.request(post["method"], {
            "sessionId": self.session, "bindingId": "b-22", "payload": post["payload"],
        }))
        with self.assertRaises(RuntimeError) as error:
            await self.retire_before_release(pending, pause, self.root / blocked_name,
                                             busy_message="Session is busy")
        self.assertEqual(json.loads(str(error.exception))["code"], -33100)
        self.assertEqual(self.calls("tools/set_active")[-1], {"names": ["read"]})
        self.assertEqual(self.preferences(), saved)
        self.assertEqual(await self.client.request("fixture/receipt", {
            "sessionId": self.session, "requestId": post["payload"]["requestId"],
        }), CASES["lookup-unknown"]["result"])
        with self.assertRaisesRegex(RuntimeError, "Reopen the session"):
            await self.client.request("session/prompt", {
                "sessionId": self.session, "prompt": [{"type": "text", "text": "/tools"}],
            })
        self.assertFalse(self.calls("prompt"))

    async def test_tools_retired_after_native_apply_before_acknowledgement(self):
        await self.assert_retired_tool_write_is_unknown("pause-tool-ack", "tool-ack-blocked")

    async def test_tools_retired_before_sqlite_commit_admission(self):
        await self.assert_retired_tool_write_is_unknown("pause-commit", "commit-blocked")
