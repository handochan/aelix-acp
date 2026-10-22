#!/usr/bin/env python3
"""Real-runtime PoC verification. Grants only the named synthetic test tools.

Uses the user's existing Aelix provider configuration; prompts contain only
synthetic fixture data. Artifacts and full local transcripts go to .local/.
"""
import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, profile, workspace, reject=False):
        self.profile, self.workspace, self.reject = profile, workspace, reject
        self.events = []
        self.counter = 0
        self.proc = None

    async def start(self):
        args = [str(ROOT / "bin/aelix-acp"), "--profile", self.profile,
                "--state-dir", str(ROOT / ".local/state")]
        if self.profile == "analytics":
            args += ["--analytics-extension", str(ROOT.parent / "aelix-analytics/extension/aelix_analytics.py")]
        self.log = open(ROOT / f".local/{self.profile}-stderr.log", "a")
        self.proc = await asyncio.create_subprocess_exec(*args, stdin=-1, stdout=-1, stderr=self.log)
        await self.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def request(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n").encode())
        await self.proc.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 180)
            if not line:
                raise RuntimeError("Adapter EOF")
            event = json.loads(line)
            self.events.append(event)
            if event.get("method") == "session/request_permission":
                call = event["params"]["toolCall"]
                args = call.get("rawInput") or {}
                name = call["title"]
                # Never grant arbitrary model-chosen shell commands in this smoke test.
                allowed = name in {"write", "read", "ls", "analytics_status", "analytics_init", "analytics_run"}
                if name in {"read", "write", "ls"}:
                    path = Path(args.get("path", "")).expanduser()
                    if not path.is_absolute():
                        path = self.workspace / path
                    allowed = path.resolve().is_relative_to(self.workspace.resolve())
                allowed = allowed and not self.reject
                answer = {"jsonrpc": "2.0", "id": event["id"], "result": {"outcome": {
                    "outcome": "selected", "optionId": "allow-once" if allowed else "reject-once"}}}
                self.proc.stdin.write((json.dumps(answer) + "\n").encode())
                await self.proc.stdin.drain()
                print(f"permission {name}: {'allow' if allowed else 'reject'}", flush=True)
            if event.get("id") == request_id:
                if "error" in event:
                    raise RuntimeError(event["error"])
                return event["result"]

    async def close(self):
        if self.proc:
            self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), 15)
        self.log.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["coding", "analytics"], default="coding")
    args = parser.parse_args()
    run_key = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    workspace = ROOT / ".local" / "smoke" / args.profile / run_key
    workspace.mkdir(parents=True, exist_ok=True)
    client = Client(args.profile, workspace)
    await client.start()
    try:
        session = await client.request("session/new", {"cwd": str(workspace), "mcpServers": []})
        sid = session["sessionId"]
        if args.profile == "coding":
            prompt = ('This is a synthetic ACP integration test. Use ONLY the write tool to create '
                      'coding-preview.html in the current directory containing exactly '
                      "<html><body><h1>Aelix Coding ACP OK</h1><p>fixture 731</p></body></html>. "
                      'Do not use bash. Then reply with CODING_POC_OK and an absolute Markdown file link. '
                      'Remember the synthetic session marker is SAPPHIRE-731.')
        else:
            project = workspace / 'recorded-analysis'
            prompt = (f'This is a synthetic ACP integration test. The user authorizes a new analysis project at {project}. '
                      f'First call analytics_init with exactly that path, then analytics_status. '
                      'Call analytics_run with name=poc_mean, title=Synthetic mean check, code that imports '
                      'statistics, prints the mean of [10,20,30], builds a plotly bar figure over those three values, '
                      'and calls aelix_analytics.visualize(fig, "synthetic-values", title="Synthetic values", '
                      'caption="Synthetic fixture only", data=polars.DataFrame({"x":[1,2,3],"value":[10,20,30]})). '
                      'Import aelix_analytics and polars properly. Do not use bash. '
                      'Then use ONLY the write tool to create '
                      'analytics-preview.html in the current directory containing exactly '
                      '<html><body><h1>Aelix Analytics ACP OK</h1><p>Synthetic values: 10,20,30. Mean: 20.</p></body></html>. '
                      'Do not use bash. Reply ANALYTICS_POC_OK with an absolute Markdown file link. '
                      'Remember synthetic session marker is SAPPHIRE-731.')
        print('session', sid, flush=True)
        result = await client.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]})
        print('prompt', result, flush=True)
        assert (workspace / f"{args.profile}-preview.html").is_file(), 'Preview file not created'
        if args.profile == 'analytics':
            successes = [e.get('params', {}).get('update', {}) for e in client.events]
            assert any(u.get('status') == 'completed' and
                       'analytics_run: ok' in json.dumps(u) for u in successes), 'No successful recorded analysis run'
            assert any('AionUi 분석 미리보기' in json.dumps(e, ensure_ascii=False) for e in client.events), 'No standalone preview link'
        await client.close()
        # A new adapter process must recover both the profile and actual conversation.
        prior = client.events
        client = Client(args.profile, workspace)
        await client.start()
        await client.request("session/load", {"sessionId": sid, "cwd": str(workspace), "mcpServers": []})
        assert any('SAPPHIRE-731' in json.dumps(e) for e in client.events), 'History was not replayed'
        await client.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text",
            "text": "What synthetic session marker did I give you? Reply with the marker only; no tools."}]})
        print('restart and history replay OK', flush=True)
        # Reject a write and prove the file was never created.
        client.reject = True
        target = workspace / 'must-not-exist.txt'
        assert not target.exists(), 'Use a fresh fixture workspace for the rejection check'
        await client.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text",
            "text": "Permission rejection test: try the write tool exactly once to write must-not-exist.txt with text BLOCK_TEST. If declined, stop and report the refusal. No other tools or retries."}]})
        assert not target.exists(), 'Rejected write ran!'
        permissions = [e for e in client.events if e.get('method') == 'session/request_permission']
        assert permissions, 'No permission request was observed'
        print('rejected write did not execute OK', flush=True)
        output = ROOT / f'.local/{args.profile}-transcript.json'
        output.write_text(json.dumps(prior + client.events, ensure_ascii=False, indent=2))
        print('transcript', output, flush=True)
    finally:
        if client.proc and client.proc.returncode is None:
            await client.close()


if __name__ == '__main__':
    asyncio.run(main())
