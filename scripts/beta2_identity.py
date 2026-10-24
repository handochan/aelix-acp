"""Verify public native wheel bytes before probing their APIs.

This is qualification tooling, not a native compatibility shim.
"""
import hashlib
import importlib
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request
import zipfile


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def check_wheel(path, spec):
    data = Path(path).read_bytes()
    if len(data) != spec["size"] or sha256(data) != spec["sha256"]:
        raise ValueError("Public wheel digest/size mismatch: " + spec["distribution"])


def compare_payload(path, installed_root):
    """Compare original wheel members, including metadata, against the install."""
    root = Path(installed_root).resolve()
    count = 0
    with zipfile.ZipFile(path) as wheel:
        for entry in wheel.infolist():
            if entry.is_dir() or entry.filename.endswith(".dist-info/RECORD"):
                continue  # Installers rewrite RECORD and add entrypoint receipts.
            relative = Path(entry.filename)
            dest = (root / relative).resolve()
            if relative.is_absolute() or not dest.is_relative_to(root) or ".data" in relative.parts:
                raise ValueError("Unexpected wheel member layout")
            if not dest.is_file() or dest.read_bytes() != wheel.read(entry):
                raise ValueError("Installed wheel member differs: " + entry.filename)
            count += 1
    return count


def verify(manifest, artifacts):
    artifacts = Path(artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    site_root = Path(metadata.distribution("aelix").locate_file("")).resolve()
    if not site_root.is_relative_to(Path(sys.prefix).resolve()) or sys.prefix == sys.base_prefix:
        raise ValueError("Run qualification in a disposable virtual environment")
    results = []
    for spec in manifest["wheels"]:
        distribution = metadata.distribution(spec["distribution"])
        if distribution.version != manifest["nativeVersion"]:
            raise ValueError("Unexpected native version: " + spec["distribution"])
        direct = distribution.read_text("direct_url.json")
        if direct and any(k in json.loads(direct) for k in ("dir_info", "vcs_info")):
            raise ValueError("Source/editable native installs are not public-wheel evidence")
        # Fetch the public registry without pip/uv index credentials or ambient config.
        with urllib.request.urlopen(spec["metadataUrl"], timeout=30) as response:
            registry = json.load(response)
        entry = next((e for e in registry["urls"] if e["filename"] == spec["filename"]), None)
        if not entry or entry["digests"]["sha256"] != spec["sha256"] or entry["size"] != spec["size"]:
            raise ValueError("Registry no longer matches the reviewed native baseline")
        url = urllib.parse.urlparse(spec["url"])
        if url.scheme != "https" or url.hostname != "files.pythonhosted.org":
            raise ValueError("Wheel URL must use the public PyPI artifact host")
        path = artifacts / spec["filename"]
        if not path.exists():
            with urllib.request.urlopen(spec["url"], timeout=45) as response:
                path.write_bytes(response.read())
        check_wheel(path, spec)
        count = compare_payload(path, distribution.locate_file(""))
        results.append({"distribution": spec["distribution"], "version": distribution.version,
                        "sha256": spec["sha256"], "wheelBytes": spec["size"],
                        "installedMembersVerified": count})
    imports = []
    for name, symbol in manifest["imports"]:
        module = importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(site_root):
            raise ValueError("Native import escaped the verified install: " + name)
        getattr(module, symbol)
        imports.append(name + ":" + symbol)
    return {"wheels": results, "imports": imports,
            "environment": {d.metadata["Name"]: d.version for d in metadata.distributions()},
            "verificationScope": "Original wheel members except installer-rewritten RECORD; not a dependency or bytecode security attestation"}
