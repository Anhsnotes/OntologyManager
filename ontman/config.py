"""Project configuration, read from ontman.toml at the repo root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "ontman.toml"


@dataclass
class Config:
    root: Path
    ontology: Path
    namespace: str | None = None
    fixtures: list[Path] = field(default_factory=list)
    violations_dir: Path | None = None
    competency_dir: Path | None = None
    changelog: Path | None = None
    docs_md: Path | None = None
    endpoint: str | None = None


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` until a directory containing ontman.toml is found."""
    here = (start or Path.cwd()).resolve()
    for d in [here, *here.parents]:
        if (d / CONFIG_FILE).exists():
            return d
    return here


def load_config(start: Path | None = None) -> Config:
    root = find_root(start)
    raw: dict = {}
    if (root / CONFIG_FILE).exists():
        raw = tomllib.loads((root / CONFIG_FILE).read_text())

    def p(key: str, default: str | None) -> Path | None:
        val = raw.get(key, default)
        return root / val if val else None

    return Config(
        root=root,
        ontology=p("ontology", "src/ontology.ttl"),
        namespace=raw.get("namespace"),
        fixtures=[root / f for f in raw.get("fixtures", [])],
        violations_dir=p("violations_dir", "tests/violations"),
        competency_dir=p("competency_dir", "tests/competency"),
        changelog=p("changelog", "CHANGELOG.md"),
        docs_md=p("docs_md", None),
        endpoint=raw.get("endpoint"),
    )
