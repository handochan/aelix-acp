"""Copied into an ACP-owned worker directory for each managed module load."""
from functools import partial
from importlib.machinery import ModuleSpec, NamespaceLoader, PathFinder
from pathlib import Path
import sys
from types import FunctionType, MethodType, ModuleType


def _module_metadata(module):
    if issubclass(type(module), ModuleType):
        return ModuleType.__dict__["__dict__"].__get__(module)
    return {}


class GenerationFinder:
    """Resolve and verify managed Python modules against one generation."""

    def __init__(self, site, module):
        self.site = Path(site)
        self.package = module.partition(".")[0]

    def location(self, path):
        if not isinstance(path, str) or not Path(path).is_absolute():
            return None
        try:
            actual = Path(path).resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            return None
        return actual if actual.is_relative_to(self.site) else None

    def owns_paths(self, paths):
        locations = tuple(paths) if paths is not None else ()
        return bool(locations) and all(
            (location := self.location(path)) is not None and location.is_dir() for path in locations
        )

    def owns(self, spec):
        if type(spec) is not ModuleSpec:
            return False
        paths = spec.submodule_search_locations
        if spec.origin is None:
            return self.owns_paths(paths)
        origin = self.location(spec.origin)
        return origin is not None and origin.is_file() and (paths is None or self.owns_paths(paths))

    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] != self.package:
            return None
        search = [str(self.site)] if path is None else [entry for entry in path if self.location(entry) is not None]
        spec = PathFinder.find_spec(fullname, search)
        if not self.owns(spec):
            raise ImportError(f"Managed module {fullname!r} is unavailable in the selected generation")
        return spec

    def verify_module(self, name, module):
        metadata = _module_metadata(module)
        spec = metadata.get("__spec__")
        if self.owns(spec):
            if spec.origin is None:
                valid = type(spec.loader) is NamespaceLoader and metadata.get("__file__") is None
            else:
                origin = self.location(spec.origin)
                valid = origin is not None and origin == self.location(metadata.get("__file__"))
            if valid and (spec.submodule_search_locations is None or self.owns_paths(metadata.get("__path__"))):
                return
        raise ImportError(f"Managed module {name!r} has no verified origin in the selected generation")

    def verify_factory(self, factory, managed_packages):
        # A plain partial forwards to its function. A subclass or other
        # callable object might execute an unverified __call__ instead.
        function = factory
        while type(function) is partial:
            function = function.func
        if type(function) is MethodType:
            function = function.__func__
        if type(function) is not FunctionType:
            raise ImportError("Managed factory must be a Python function or a verified wrapper around one")

        # __module__ can be copied by a decorator. The actual function
        # globals identify its defining module even after such a copy.
        owner = function.__globals__.get("__name__")
        owner_module = sys.modules.get(owner) if isinstance(owner, str) else None
        metadata = _module_metadata(owner_module)
        if metadata is not function.__globals__:
            raise ImportError("Managed factory has no verified defining module")
        self.verify_module(owner, owner_module)
        if metadata.get("__file__") is None:
            raise ImportError("Managed factory must be defined in a file-backed module")
        code = self.location(function.__code__.co_filename)
        if code is None or not code.is_file():
            raise ImportError("Managed factory code resolves outside the selected generation")

        # Imports served from sys.modules bypass find_spec. Check the
        # realized module graph, including the factory's own package even
        # when it was re-exported from another distribution.
        packages = {*managed_packages, self.package, owner.partition(".")[0]}
        for name, module in tuple(sys.modules.items()):
            if module is not None and name.partition(".")[0] in packages:
                self.verify_module(name, module)


def load_factory(site, reference, managed_packages):
    # This shim runs in the pinned native worker. Keep native's workspace
    # import exclusions in force for dependencies imported by the module.
    from aelix_coding_agent.extensions.loader import _import_module_outside_cwd, _import_path_without_cwd

    module_name, _, factory_name = reference.partition(":")
    finder = GenerationFinder(site, module_name)
    parts = module_name.split(".")
    for end in range(1, len(parts) + 1):
        name = ".".join(parts[:end])
        if name in sys.modules:
            finder.verify_module(name, sys.modules[name])
    sys.meta_path.insert(0, finder)
    try:
        module = _import_module_outside_cwd(module_name, cwd=None)
        finder.verify_module(module_name, module)
        with _import_path_without_cwd(None):
            factory = getattr(module, factory_name)
        finder.verify_factory(factory, managed_packages)
        return factory
    finally:
        sys.meta_path.remove(finder)
