"""D16 decision 2 through ACP stdio and a synthetic guarded native worker."""
import asyncio
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

GUARDED_CHILD = r'''
import hashlib
import json
import os
from pathlib import Path
import sys

root = Path.cwd()
Path(os.environ["AELIX_ACP_READY_FILE"]).write_text(os.environ["AELIX_ACP_NONCE"])
mode = sys.argv[sys.argv.index("--permission-mode") + 1]
methods = ["agents/show", "agents/use", "permissions/get", "permissions/set", "tools/set_active", "resources/list"]
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
        result = {"version": 1, "methods": methods, "profileRefGuard": 1, "activeAgent": active}
    elif name == "permissions/get":
        result = {"mode": mode}
    elif name == "permissions/set":
        mode = params["mode"]
        result = {"mode": mode}
    elif name == "resources/list":
        result = {"tools": [{"name": "read"}], "activeTools": tools}
    elif name == "tools/set_active":
        tools = params["names"]
        result = {"activeTools": tools}
    elif name == "agents/show":
        try:
            ref, profile = read_profile()
            result = {"profile": {**profile, "ref": ref}}
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
            result = {"message": "Agent profile selected", "activeRef": ref}
    response = {"type": "response", "command": frame["type"], "success": success, "id": frame["id"], "data": result}
    if not success:
        response["error"] = "profile not found"
    print(json.dumps(response), flush=True)
'''


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
        script = self.root / "guarded.py"
        script.write_text(GUARDED_CHILD, encoding="utf-8")
        self.client = await AcpClient.start(
            self.root, sys.executable, prefix=[str(script)], capabilities=initialize_offer()["clientCapabilities"],
            entry=("-c", "from aelix_acp.runtime import profile_reselection as r;"
                   "r.APPROVAL_SECONDS=0.3;from aelix_acp.server import main;main()"),
        )
        self.addAsyncCleanup(self.client.close)
        self.session = (await self.client.request("session/new", self.open_params("b-21")))["sessionId"]
        self.original = self.write_record()

    def write_profile(self, path, index, suffix=b""):
        vector = CASES["profile-ref"]["vectors"][index]
        raw = vector["rawUtf8"].encode() + suffix
        path.write_bytes(raw)
        scope = vector["ref"]["scope"]
        prefix = ("studio-profile/1\0" + scope + "\0" + path.as_uri() + "\0").encode()
        return {**vector["ref"], "revision": "sha256:" + hashlib.sha256(prefix + raw).hexdigest()}

    def preferences(self, update=None):
        with sqlite3.connect(self.root / "state/sessions.sqlite3") as db:
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
        self.assertTrue(any(message["method"] == cancelled["method"] and
                            message["params"]["requestId"] == self.client.requests[0]["id"] for message in self.client.updates))
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
