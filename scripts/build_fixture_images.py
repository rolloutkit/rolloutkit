"""Build every fixture image the matrix declares.

The measurement set needs the same images on three machines. The test suite
already builds them, but only as a side effect of running the Docker matrix,
which costs five minutes and answers a different question. Splitting the build
out means a Linux server, a macOS laptop and a CI runner can be brought to the
same starting line with one command each.

The image list is read from `fixtures/matrix.yaml` rather than repeated here.
A second copy would be a second thing to forget: the matrix is already the file
that decides which images exist, and a batch run against a stale hand-written
list would measure a different target than the one the row claims.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def main() -> int:
    matrix = yaml.safe_load((FIXTURES / "matrix.yaml").read_text())
    images = matrix.get("images") or []
    if not images:
        print("build_fixture_images: matrix.yaml declares no images", file=sys.stderr)
        return 2

    for image in images:
        context = FIXTURES / image["context"]
        command = ["docker", "build", "-t", image["name"]]
        if "dockerfile" in image:
            command += ["-f", str(context / image["dockerfile"])]
        command.append(str(context))
        print(f"building {image['name']} from {image['context']}", flush=True)
        build = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if build.returncode != 0:
            print(
                f"build_fixture_images: {image['name']} failed:\n{build.stderr[-3000:]}",
                file=sys.stderr,
            )
            return 1

    print(f"build_fixture_images: {len(images)} image(s) ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
