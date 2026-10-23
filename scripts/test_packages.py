"""Install/run/update/remove the native starter wheel in a disposable runtime."""
import argparse
import asyncio
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from support import AcpClient  # noqa: E402


async def exercise(aelix, wheel):
    with tempfile.TemporaryDirectory(prefix="acp-packages-") as directory:
        root = Path(directory)
        (root / "home").mkdir()
        (root / "agent").mkdir()
        client = await AcpClient.start(root, aelix, adapter_args=["--offline"])
        try:
            session = (await client.request("session/new", {"cwd": str(root), "mcpServers": []}, timeout=120))["sessionId"]
            async def manage(action, arguments=None):
                return await client.request("_aelix/manage", {"sessionId": session, "action": action,
                    "arguments": arguments or {}}, timeout=180)
            async def package(action, target):
                result = await manage("extensions/action", {"action": action, "target": target})
                if result.get("exitCode", 0) != 0 or result.get("ready") is False:
                    raise RuntimeError(result)
                print(action, result.get("exitCode", result.get("ready")), flush=True)
            inventory = await manage("extensions/list")
            if any(entry["name"] == "aelix-starter" for entry in inventory["extensions"]):
                raise RuntimeError("Use a disposable runtime without a preinstalled aelix-starter")
            await package("install", str(wheel))
            inventory = await manage("extensions/list")
            extension = next(entry for entry in inventory["extensions"] if entry["name"] == "aelix-starter")
            await package("enable", extension["entry"])
            await client.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "/hello"}]})
            if "hello from the aelix starter extension" not in client.text():
                raise RuntimeError("Installed command did not execute")
            await package("update", extension["distribution"])
            await package("disable", extension["entry"])
            await package("remove", extension["name"])
            if any(entry["name"] == extension["name"] for entry in (await manage("extensions/list"))["extensions"]):
                raise RuntimeError("Removed extension remains in inventory")
            print("PASS: offline wheel install, inventory, enable, command, update, disable, remove")
        finally:
            await client.close()


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Run package acceptance with Python 3.11 or later")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aelix", type=Path, required=True, help="Disposable writable runtime; package changes occur here")
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(exercise(args.aelix.resolve(), args.wheel.resolve()))


if __name__ == "__main__":
    main()
