"""Probe an unchanged AionCore host using installed ACP/beta.2, isolated data and no model turn."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--acp", type=Path, required=True)
    parser.add_argument("--aelix", type=Path, required=True)
    args = parser.parse_args()
    backend, acp, aelix = (path.absolute() for path in (args.backend, args.acp, args.aelix))
    with tempfile.TemporaryDirectory(prefix="acp-studio-beta2-") as temporary:
        root = Path(temporary)
        agent = root / "agent"
        agent.mkdir()
        (agent / "models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": "http://127.0.0.1:9/v1", "api": "openai-completions", "apiKey": "synthetic-probe-key",
            "models": [{"id": name, "input": ["text"], "contextWindow": 32768, "maxTokens": 2048}
                       for name in ("one", "two")]}}}))
        (agent / "settings.json").write_text(json.dumps({"defaultProvider": "fixture", "defaultModel": "one"}))
        (agent / "auth.json").write_text(json.dumps({"fixture": {"type": "api_key", "key": "synthetic-probe-key"}}))
        env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if name in os.environ}
        env.update(HOME=temporary, USERPROFILE=temporary, APPDATA=str(root / "appdata"), LOCALAPPDATA=str(root / "local"),
                   AELIX_CODING_AGENT_DIR=str(agent), AELIX_SETTINGS_PATH=str(agent / "settings.json"),
                   AELIX_AUTH_PATH=str(agent / "auth.json"), PYTHONDONTWRITEBYTECODE="1")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        def api(route, method="GET", body=None):
            request = urllib.request.Request(base + route, method=method,
                data=json.dumps(body if body is not None else {}).encode() if method != "GET" else None,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    value = json.loads(response.read())
                    return value.get("data", value)
            except urllib.error.HTTPError as error:
                raise RuntimeError(f"{method} {route}: {error.code} {error.read().decode()}") from None

        report = {"evidence": "unchanged local AionCore + installed ACP wheel + published beta.2; no model turn"}
        conversations = []
        team = None
        with (root / "backend.log").open("w") as log:
            process = subprocess.Popen([str(backend), "--local", "--host", "127.0.0.1", "--port", str(port),
                "--data-dir", str(root / "data"), "--work-dir", str(root), "--log-dir", str(root / "logs"),
                "--managed-resources-mode", "bundled", "--log-level", "error"],
                env=env, cwd=backend.parent, stdout=log, stderr=log,
                **({"start_new_session": True} if os.name != "nt" else {}))
            try:
                for _ in range(150):
                    if process.poll() is not None:
                        raise RuntimeError("Isolated backend exited: " + (root / "backend.log").read_text()[-2000:])
                    try:
                        api("/health")
                        break
                    except (OSError, RuntimeError):
                        time.sleep(.1)
                else:
                    raise RuntimeError("Isolated backend did not become ready")
                bodies = []
                for index in range(2):
                    body = {"name": f"ACP beta2 probe {index}", "command": str(acp),
                            "args": ["--aelix", str(aelix), "--state-dir", str(root / f"acp-{index}")], "env": [],
                            "advanced": {"description": "Disposable beta2 host probe", "behavior_policy": {"supports_team": True}}}
                    created = api("/api/agents/custom", "POST", body)
                    bodies.append((created["id"], body))
                assistants = [item for item in api("/api/assistants") if item["name"].startswith("ACP beta2 probe")]
                assert len(assistants) == 2
                workspace = root / "workspace"
                workspace.mkdir()
                conversation = api("/api/conversations", "POST", {"name": "Preview compatibility", "assistant": {"id": assistants[0]["id"]},
                    "extra": {"workspace": str(workspace), "custom_workspace": True}})
                conversations.append(conversation["id"])
                route = f"/api/conversations/{conversation['id']}"
                runtime = api(route + "/runtime/ensure", "POST")
                values = {option["id"]: option["current_value"] for option in runtime["config_options"]}
                assert {"thinking_level", "permission_mode", "steeringMode", "followUpMode", "autoCompactionEnabled", "autoRetryEnabled"} <= values.keys()
                model_option = next((option for option in runtime["config_options"] if option["id"] == "model"), None)
                assert model_option and model_option.get("options"), "The beta.2 extension bridge must expose real model choices"
                model_changed = api(route + "/config-options/model", "PUT", {"value": "fixture/two"})
                assert model_changed["confirmation"] == "observed"
                assert next(option["current_value"] for option in model_changed["config_options"] if option["id"] == "model") == "fixture/two"
                changed = api(route + "/config-options/autoRetryEnabled", "PUT", {"value": "false"})
                assert changed["confirmation"] == "observed"
                assert next(option["current_value"] for option in changed["config_options"] if option["id"] == "autoRetryEnabled") == "false"
                report.update(standardSession=True, configOptions=list(values), settingConfirmation="observed",
                              modelSelector="public-extension-bridge", modelConfirmation="observed", selectedModel="fixture/two")
                try:
                    team = api("/api/teams", "POST", {"name": "Preview team probe", "workspace": str(workspace),
                        "agents": [{"assistant_id": item["id"], "name": item["name"], "role": "lead" if index == 0 else "teammate", "model": "default"}
                                   for index, item in enumerate(assistants)]})
                    api(f"/api/teams/{team['id']}/session", "POST")
                    teammate = next(item for item in team["assistants"] if item["role"] == "teammate")
                    api(f"/api/teams/{team['id']}/agents/{teammate['slot_id']}/attach", "POST")
                    report["teamSession"] = True
                except RuntimeError as error:
                    report["teamSession"] = False
                    report["teamError"] = str(error)
                for identifier, body in bodies:
                    body["advanced"]["behavior_policy"]["supports_team"] = False
                    api(f"/api/agents/custom/{identifier}", "PUT", body)
                report["teamCanBeDisabledByMetadata"] = all(not item.get("team_selectable") for item in api("/api/assistants")
                    if item["name"].startswith("ACP beta2 probe"))
                print(json.dumps(report, ensure_ascii=False, indent=2))
            finally:
                for identifier in conversations:
                    try:
                        api(f"/api/conversations/{identifier}", "DELETE")
                    except (RuntimeError, OSError):
                        pass
                if team:
                    for suffix in ("/session", ""):
                        try:
                            api(f"/api/teams/{team['id']}" + suffix, "DELETE")
                        except (RuntimeError, OSError):
                            pass
                if process.poll() is None:
                    if os.name == "nt":
                        executable = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe"
                        subprocess.run([str(executable), "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
