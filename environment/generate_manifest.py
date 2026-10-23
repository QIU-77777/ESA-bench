#!/usr/bin/env python3
"""Generate the checked-in capability manifest from catalog, handlers and config."""
import json

from .data import ROOT, Dataset
from .service import Environment
from .store import MemoryStore


def main() -> None:
    manifest = Environment(MemoryStore(test_only=True), Dataset()).manifest()
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        "wrote"
        f" {ROOT / 'manifest.json'} with {len(manifest['tools'])} tools"
    )


if __name__ == "__main__":
    main()
