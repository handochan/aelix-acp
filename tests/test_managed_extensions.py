import argparse
from functools import partial
from importlib.util import module_from_spec, spec_from_file_location
import json
import math
from pathlib import Path
import sys
import tempfile
from types import FunctionType, SimpleNamespace
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

    def test_factory_verifies_cached_managed_dependency_and_file_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            outside = Path(temp).resolve()
            site = outside / "site-packages"
            site.mkdir()
            local_file = site / "entry.py"
            local_file.write_text(
                "def setup(api): pass\nclass Fixture:\n    def register(self, api): pass\n",
                encoding="utf-8",
            )
            foreign_file = outside / "sibling.py"
            foreign_file.touch()
            entry_spec = spec_from_file_location("entry", local_file)
            entry = module_from_spec(entry_spec)
            entry_spec.loader.exec_module(entry)
            foreign = module_from_spec(spec_from_file_location("catalogued.sibling", foreign_file))
            finder = GenerationFinder(str(site), "entry")
            with patch.dict(sys.modules, {"entry": entry, "catalogued.sibling": foreign}):
                finder.verify_factory(entry.setup, ())
                finder.verify_factory(entry.Fixture().register, ())
                finder.verify_factory(partial(entry.setup), ())
                with self.assertRaisesRegex(ImportError, "must be a Python function"):
                    finder.verify_factory(entry.Fixture, ())
                with self.assertRaisesRegex(ImportError, "catalogued.sibling"):
                    finder.verify_factory(entry.setup, ("catalogued",))
                entry.__file__ = str(foreign_file)
                with self.assertRaisesRegex(ImportError, "verified origin"):
                    finder.verify_factory(entry.setup, ())
                entry.__file__ = str(local_file)
                del entry.__file__
                with self.assertRaisesRegex(ImportError, "verified origin"):
                    finder.verify_factory(entry.setup, ())
            with self.assertRaisesRegex(ImportError, "must be a Python function"):
                finder.verify_factory(math.sqrt, ())

    def test_module_finder_refuses_symlink_outside_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            outside = Path(temp).resolve()
            site = outside / "site-packages"
            site.mkdir()
            foreign = outside / "module.py"
            foreign.touch()
            link = site / "acp_symlink_fixture.py"
            try:
                link.symlink_to(foreign)
            except OSError:
                self.skipTest("file symlink unavailable")
            name = "acp_symlink_fixture"
            finder = GenerationFinder(str(site), name)
            with self.assertRaisesRegex(ImportError, "unavailable in the selected generation"):
                finder.find_spec(name)
            module = module_from_spec(spec_from_file_location(name, link))
            with self.assertRaisesRegex(ImportError, "no verified origin"):
                finder.verify_module(name, module)

    def test_factory_rejects_callable_objects_with_an_apparent_function_type(self):
        class Fixture:
            @property
            def __class__(self):
                return FunctionType

            def __call__(self, api):
                raise AssertionError("factory must not be called")

        with tempfile.TemporaryDirectory() as temp:
            finder = GenerationFinder(str(Path(temp).resolve()), "entry")
            with self.assertRaisesRegex(ImportError, "must be a Python function"):
                finder.verify_factory(Fixture(), ())
