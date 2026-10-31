"""Run the black-box ACP suite using one checked-out native workspace."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--check-ref", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    core = args.core.resolve()
    if args.check_ref:
        expected = json.loads((root / "runtime-reference.json").read_text())["commit"]
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=core, text=True).strip()
        if actual != expected:
            raise SystemExit(f"Native runtime mismatch: expected {expected}, found {actual}")
        print(f"Native runtime: aelix-ai {actual} (runtime-reference.json)", flush=True)
    binaries = core / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    python = binaries / ("python.exe" if os.name == "nt" else "python")
    aelix = binaries / ("aelix.exe" if os.name == "nt" else "aelix")
    if not python.is_file() or not aelix.is_file():
        raise SystemExit("Run uv sync --all-packages in the native workspace first")
    env = dict(os.environ, PYTHONPATH=str(root / "src"), AELIX_ACP_TEST_AELIX=str(aelix))
    raise SystemExit(subprocess.call([str(python), "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"], cwd=root, env=env))


if __name__ == "__main__":
    main()
