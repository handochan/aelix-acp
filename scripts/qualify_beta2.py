"""Qualify unmodified public beta.2 wheels and an installed ACP entrypoint.

Run inside a disposable native wheel venv. Uses only synthetic loopback model
responses and temporary auth/config/session roots; does not read user auth.
"""
import argparse
import asyncio
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig
import tempfile

from beta2_identity import verify
from beta2_wire import isolated_environment, probe_adapter, probe_native

ROOT = Path(__file__).resolve().parents[1]


def native_help(executable):
    # Public beta.2 loads cwd .env before handling --help.
    with tempfile.TemporaryDirectory(prefix="beta2-help-") as temporary:
        command = list(executable) if isinstance(executable, (list, tuple)) else [str(executable)]
        return subprocess.check_output([*command, "--help"], text=True, encoding="utf-8", timeout=20,
                                       cwd=temporary, env=isolated_environment(Path(temporary)))


def adapter_receipt(entrypoint, compare_checkout=True):
    python = entrypoint.parent / ("python.exe" if os.name == "nt" else "python")
    if not python.is_file():
        raise ValueError("ACP entrypoint must belong to a disposable installed-wheel venv")
    code = '''import hashlib,importlib.metadata as m,json,pathlib,sys
d=m.distribution("aelix-acp")
direct=json.loads(d.read_text("direct_url.json") or "{}")
assert not any(k in direct for k in ("dir_info","vcs_info")), "Install the ACP wheel, not a checkout"
files={str(p):hashlib.sha256(d.locate_file(p).read_bytes()).hexdigest() for p in d.files
 if str(p).startswith("aelix_acp/") and "__pycache__" not in p.parts}
assert files and pathlib.Path(d.locate_file("")).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
print(json.dumps({"version":d.version,"packageFiles":files}))'''
    receipt = json.loads(subprocess.check_output([str(python), "-I", "-c", code], text=True, timeout=20))
    if compare_checkout:
        expected = {"aelix_acp/" + str(p.relative_to(ROOT / "src/aelix_acp")).replace(os.sep, "/"):
                    hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "src/aelix_acp").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts}
        if expected != receipt["packageFiles"]:
            raise ValueError("Installed ACP payload does not match the qualification checkout")
    return receipt


def main():
    if not __debug__:
        raise RuntimeError("Qualification assertions require Python without -O")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, type=Path, help="Installed ACP CLI; source/editable entrypoints are not accepted")
    parser.add_argument("--candidate", type=Path, help="Optional immutable PR #1 installed CLI; asserts its control-v1 refusal")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".local/beta2-wheels")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = ROOT / "docs/beta2/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    report = {"schemaVersion": 1, "scope": "public-wheel and synthetic-model qualification; no real-provider/host/installer acceptance",
              "recordedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "platform": platform.system() + "/" + platform.machine(), "python": platform.python_version(),
              "nativeBaselineManifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "nativeSourceTag": manifest["sourceTag"], "productNativePatches": [], "status": "failed"}
    report["harnessSourceSha256"] = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                     for name in ("qualify_beta2.py", "beta2_identity.py", "beta2_probe.py", "beta2_wire.py")}
    if os.environ.get("GITHUB_SHA"):
        report["ciSourceCommit"] = os.environ["GITHUB_SHA"]
    try:
        report["stage"] = "identity_before"
        report["identityBefore"] = verify(manifest, args.artifacts)
        native = Path(sysconfig.get_path("scripts")) / ("aelix.exe" if os.name == "nt" else "aelix")
        adapter = args.adapter.resolve()
        if not native.is_file() or not adapter.is_file():
            raise ValueError("Installed native and ACP entrypoints are required")
        report["installedAdapter"] = adapter_receipt(adapter)
        help_text = native_help(native)
        for flag in manifest["cliFlags"]:
            if flag not in help_text:
                raise ValueError("Missing public CLI flag: " + flag)
        report["cliFlags"] = manifest["cliFlags"]
        report["stage"] = "native_wire"
        report["native"] = asyncio.run(probe_native(native, manifest))
        report["stage"] = "installed_acp_wire"
        report["adapter"] = asyncio.run(probe_adapter(adapter, native))
        if args.candidate:
            report["stage"] = "installed_pr_candidate"
            report["installedCandidate"] = adapter_receipt(args.candidate.resolve(), compare_checkout=False)
            report["pr1"] = asyncio.run(probe_adapter(args.candidate.resolve(), native, candidate=True))
        report["stage"] = "identity_after"
        report["identityAfter"] = verify(manifest, args.artifacts)
        report["stage"] = "complete"
        report["status"] = "passed"
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "nativeChecks": len(report["native"]["checksPassed"]),
                      "adapterChecks": len(report["adapter"]["checksPassed"]), "report": str(args.report)}))


if __name__ == "__main__":
    main()
