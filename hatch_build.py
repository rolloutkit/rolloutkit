"""Freeze the source revision into the distribution while it is still knowable.

`provenance.rolloutkit_commit()` answers at run time, from `ROLLOUTKIT_COMMIT`
or from an enclosing Git checkout. A wheel on somebody else's machine has
neither, so every report an installed rolloutkit wrote named its own revision
`unknown` — and that field is the one the whole evidence chain is indexed by.
`docs/field-notes.md` requires every measurement to name the harness commit;
until this hook existed, only a copy running from the repository could.

The revision is knowable exactly once: here, while the checkout that produced
the artefact is still around. So the build writes it down.

Two rules, both of which exist to keep a build from lying:

* A build that cannot resolve a revision stamps nothing rather than stamping a
  guess. `uv build` from an exported tarball with no Git still has to produce a
  distribution, and the reports it writes go on saying `unknown`, which is true.
* A stamp already present is never overwritten. An sdist built from a checkout
  carries one; the wheel built from that sdist has no Git, and overwriting would
  replace a known revision with an unknown one.

The hook does not look for the revision itself. It calls the same function the
runtime calls, so there is one rule about what a revision is, evaluated twice —
once at build time and once at run time — rather than two rules that can drift.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

STAMP = "_build_info.py"

_TEMPLATE = '''\
"""The revision this distribution was built from.

Written by `hatch_build.py` at build time and read by `rolloutkit.provenance`.
A source checkout has no such module, and provenance asks Git instead.
"""

from __future__ import annotations

COMMIT = "{commit}"
'''


class RolloutkitBuildHook(BuildHookInterface):
    """Stamp `rolloutkit/_build_info.py` into the artefact being built."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            # An editable install points back at the working tree, whose
            # revision moves with every commit. A stamp would freeze whichever
            # revision `uv sync` last saw and quietly outrank the checkout, so
            # a developer's own runs would name a revision they had left behind.
            return

        source = Path(self.root) / "src" / "rolloutkit"
        if (source / STAMP).exists():
            return

        commit = _commit(source / "provenance.py")
        if commit == "unknown":
            return

        self._staged = Path(tempfile.mkdtemp(prefix="rolloutkit-stamp-"))
        stamp = self._staged / STAMP
        stamp.write_text(_TEMPLATE.format(commit=commit), encoding="utf-8")

        # The wheel is rooted at the package; the sdist mirrors the project.
        prefix = "rolloutkit" if self.target_name == "wheel" else "src/rolloutkit"
        build_data["force_include"][str(stamp)] = f"{prefix}/{STAMP}"

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        staged = getattr(self, "_staged", None)
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)
            self._staged = None


def _commit(provenance: Path) -> str:
    """Ask the runtime's own resolver, loading it from the tree being built.

    By file path rather than by `import rolloutkit.provenance`: a build
    environment is allowed to contain an installed rolloutkit, and importing
    the name would ask that copy about this checkout.
    """
    if not provenance.is_file():
        return "unknown"

    spec = importlib.util.spec_from_file_location("_rolloutkit_provenance", provenance)
    if spec is None or spec.loader is None:
        return "unknown"

    # A `None` entry makes the import fail instead of resolving. Without it, an
    # installed rolloutkit in the build environment would answer with the stamp
    # from whenever *it* was built, and this artefact would carry that revision.
    blocked = "rolloutkit._build_info"
    sentinel = object()
    previous = sys.modules.get(blocked, sentinel)
    sys.modules[blocked] = None  # type: ignore[assignment]
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.rolloutkit_commit())
    finally:
        if previous is sentinel:
            del sys.modules[blocked]
        else:
            sys.modules[blocked] = previous
