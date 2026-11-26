"""Run the black-box ACP suite against the pinned release or a developer workspace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import urlopen


def verify_asset(directory, url, name, digest):
    path = directory / name
    if not path.exists():
        with urlopen(f"{url}/{name}", timeout=60) as response:
            path.write_bytes(response.read())
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit(f"SHA-256 mismatch for {name}")
    return path


def install_release(core, python, reference, wheel_dir):
    if (core / ".venv").exists():
        raise SystemExit("Use a fresh native checkout for --install-release")
    url = f"{reference['repository']}/releases/download/{reference['tag']}"
    with tempfile.TemporaryDirectory(prefix="acp-native-release-") as directory:
        scratch = Path(directory)
        assets = wheel_dir if wheel_dir is not None else scratch
        sums = verify_asset(assets, url, "SHA256SUMS", reference["sha256sums"])
        published = {name: digest for digest, name in (line.split() for line in sums.read_text().splitlines())}
        wheels = []
        for name, digest in reference["wheels"].items():
            if published.get(name) != digest:
                raise SystemExit(f"Release checksum differs from runtime-reference.json for {name}")
            wheels.append(str(verify_asset(assets, url, name, digest)))
        # Install third-party dependencies from the release tag's lock.
        requirements = scratch / "requirements.txt"
        subprocess.run(["uv", "export", "--project", str(core), "--locked", "--no-default-groups",
                        "--no-emit-workspace", "--extra", "tui", "--quiet", "--output-file", str(requirements)], check=True)
        subprocess.run(["uv", "venv", "--python", sys.executable, str(core / ".venv")], check=True)
        subprocess.run(["uv", "pip", "sync", "--python", str(python), "--require-hashes", str(requirements)], check=True)
        subprocess.run(["uv", "pip", "install", "--python", str(python), "--no-deps", *wheels], check=True)
        subprocess.run(["uv", "pip", "check", "--python", str(python)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--check-ref", action="store_true")
    parser.add_argument("--install-release", action="store_true")
    parser.add_argument("--wheel-dir", type=Path, help="Directory containing the released wheels and SHA256SUMS")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    core = args.core.resolve()
    binaries = core / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    python = binaries / ("python.exe" if os.name == "nt" else "python")
    aelix = binaries / ("aelix.exe" if os.name == "nt" else "aelix")

    if args.check_ref or args.install_release:
        reference = json.loads((root / "runtime-reference.json").read_text())
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=core, text=True).strip()
        if actual != reference["commit"]:
            raise SystemExit(f"Native runtime mismatch: expected {reference['commit']}, found {actual}")
        print(f"Native runtime: aelix-ai {reference['tag']} at {actual} (runtime-reference.json)", flush=True)
    if args.install_release:
        install_release(core, python, reference, args.wheel_dir)
    if not python.is_file() or not aelix.is_file():
        raise SystemExit("Install the pinned release or run uv sync --all-packages in the developer workspace first")
    env = dict(os.environ, PYTHONPATH=str(root / "src"), AELIX_ACP_TEST_AELIX=str(aelix))
    if args.install_release:
        env["AELIX_ACP_REQUIRE_TRANSCRIPT"] = "1"
        env["AELIX_ACP_REQUIRE_LOCK_HOLDER"] = "1"
    raise SystemExit(subprocess.call(
        [str(python), "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"], cwd=root, env=env,
    ))


if __name__ == "__main__":
    main()
