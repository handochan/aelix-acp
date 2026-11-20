import argparse
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aelix_acp.runtime.managed_extensions import META, read_launch
from aelix_acp.runtime.managed_module import GenerationFinder
from aelix_acp.server import Server
from aelix_acp.wire.errors import ProtocolError


class ManagedLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_advertises_only_when_supplied(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "launch.json"
            path.write_text('{"version":1,"generation":"g1","extensions":[]}', encoding="utf-8")
            snapshot = read_launch(str(path))
            path.write_text('{"version":2}', encoding="utf-8")
            for current in (None, snapshot):
                with self.subTest(managed=current is not None):
                    server = Server(SimpleNamespace(state_dir=temp, profile="coding", managed_extensions=current))
                    try:
                        result = await server.dispatch("initialize", {"protocolVersion": 1})
                        self.assertEqual(result["_meta"].get(META), {"version": 1} if current else None)
                        if current:
                            self.assertEqual(await current.status(SimpleNamespace(extension_overrides={})),
                                             {"generation": "g1", "extensions": []})
                    finally:
                        server.db.close()

    def test_rejects_ambiguous_or_invalid_snapshot(self):
        entry = {"id": "demo:hello", "defaultEnabled": True, "reference": "demo:setup"}
        for invalid in (
            {"version": 2, "generation": "g1", "extensions": []},
            {"version": True, "generation": "g1", "extensions": []},
            {"version": 1, "generation": "g1", "extensions": [entry, entry]},
            {"version": 1, "generation": "g1", "extensions": [entry, dict(entry, id="another")]},
            {"version": 1, "generation": "g1", "extensions": [dict(entry, defaultEnabled=1)]},
            {"version": 1, "generation": "g1", "extensions": [dict(entry, reference="demo")]},
            {"version": 1, "generation": "g1", "extensions": [dict(entry, reference="demo:a.setup")]},
            {"version": 1, "generation": "g1", "extensions": [dict(entry, reference="démó:setup")]},
            {"version": 1, "generation": "g1", "extensions": [dict(entry, reference="/not-a-module")]},
        ):
            with self.subTest(snapshot=invalid), tempfile.TemporaryDirectory() as temp:
                path = Path(temp).resolve() / "snapshot.json"
                path.write_text(json.dumps(dict(invalid, sitePackages=str(path.parent))), encoding="utf-8")
                with self.assertRaises(argparse.ArgumentTypeError):
                    read_launch(str(path))

    def test_disabled_module_conflicts_with_operator_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "snapshot.json"
            path.write_text(json.dumps({"version": 1, "generation": "g1",
                                        "sitePackages": str(path.parent), "extensions": [
                {"id": "demo:hello", "defaultEnabled": False, "reference": "demo:setup"},
            ]}), encoding="utf-8")
            snapshot = read_launch(str(path))
            self.assertEqual(snapshot.arguments({}, path.parent), [])
            with self.assertRaises(ProtocolError) as caught:
                snapshot.check_external(["demo"])
            self.assertEqual(caught.exception.data["kind"], "managed_extension_conflict")
            self.assertEqual(snapshot.migrate_session_references(["demo", "other:setup"], {}),
                             (["other:setup"], {"demo:hello": "on"}))
            self.assertEqual(snapshot.migrate_session_references(["demo:setup"], {"demo:hello": "off"}),
                             ([], {"demo:hello": "off"}))

    def test_modules_require_generation_import_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "snapshot.json"
            entry = {"id": "demo:hello", "defaultEnabled": False, "reference": "demo:setup"}
            snapshot = {"version": 1, "generation": "g1", "extensions": [entry]}
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaisesRegex(argparse.ArgumentTypeError, "sitePackages is required"):
                read_launch(str(path))

            path.write_text(json.dumps(dict(snapshot, sitePackages=str(path))), encoding="utf-8")
            with self.assertRaisesRegex(argparse.ArgumentTypeError, "sitePackages must be an absolute"):
                read_launch(str(path))

    def test_module_finder_refuses_fallback_to_another_import_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            other = Path(temp).resolve()
            site = other / "site-packages"
            site.mkdir()
            (other / "acp_generation_only.py").write_text("def setup(api): pass\n", encoding="utf-8")
            finder = GenerationFinder(str(site), "acp_generation_only")
            with patch.object(sys, "path", [str(other), *sys.path]):
                with self.assertRaisesRegex(ImportError, "unavailable in the selected generation"):
                    finder.find_spec("acp_generation_only")
