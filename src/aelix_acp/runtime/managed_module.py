"""Copied into an ACP-owned worker directory for each managed module load."""
from importlib.machinery import PathFinder
from pathlib import Path
import sys


class GenerationFinder:
    """Resolve a managed package and its submodules through one generation."""

    def __init__(self, site, module):
        self.site = Path(site)
        self.package = module.partition(".")[0]

    def contains(self, path):
        return isinstance(path, str) and Path(path).is_absolute() and Path(path).resolve().is_relative_to(self.site)

    def owns(self, spec):
        if spec is None:
            return False
        paths = spec.submodule_search_locations
        # A namespace package has search locations rather than a source file.
        return (self.contains(spec.origin) if spec.origin is not None else bool(paths)) and (
            paths is None or all(self.contains(path) for path in paths)
        )

    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] != self.package:
            return None
        search = [str(self.site)] if path is None else [entry for entry in path if self.contains(entry)]
        spec = PathFinder.find_spec(fullname, search)
        if not self.owns(spec):
            raise ImportError(f"Managed module {fullname!r} is unavailable in the selected generation")
        return spec


def load_factory(site, reference):
    # This shim runs in the pinned native worker. Keep native's workspace
    # import exclusions in force for dependencies imported by the module.
    from aelix_coding_agent.extensions.loader import _import_module_outside_cwd, _import_path_without_cwd

    module_name, _, factory_name = reference.partition(":")
    finder = GenerationFinder(site, module_name)
    parts = module_name.split(".")
    for end in range(1, len(parts) + 1):
        name = ".".join(parts[:end])
        if name in sys.modules and not finder.owns(getattr(sys.modules[name], "__spec__", None)):
            raise ImportError(f"Managed module {name!r} was already imported outside the selected generation")
    sys.meta_path.insert(0, finder)
    try:
        module = _import_module_outside_cwd(module_name, cwd=None)
        if not finder.owns(module.__spec__):
            raise ImportError(f"Managed module {module_name!r} is unavailable in the selected generation")
        # A package can import the factory on demand from __getattr__.
        # Resolve it while the generation finder and native cwd guard apply.
        with _import_path_without_cwd(None):
            return getattr(module, factory_name)
    finally:
        sys.meta_path.remove(finder)
