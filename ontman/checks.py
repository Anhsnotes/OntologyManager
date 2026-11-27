"""All quality gates for an ontology change. `ontman check` runs every one."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import OWL, RDF, Graph, Literal, URIRef

from . import graphs
from .config import Config
from .reasoning import reason


@dataclass
class Finding:
    check: str
    message: str
    severity: str = "error"  # "error" fails the build, "warning" does not


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    def add(self, check: str, message: str, severity: str = "error") -> None:
        self.findings.append(Finding(check, message, severity))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


# ---------------------------------------------------------------- helpers

_HEADER = re.compile(r"^#\s*(\w[\w-]*)\s*:\s*(.+?)\s*$")


def query_headers(text: str) -> dict[str, str]:
    """Read `# key: value` lines at the top of a .rq file."""
    headers: dict[str, str] = {}
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            headers[m.group(1).lower()] = m.group(2)
        elif line.strip() and not line.startswith("#"):
            break
    return headers


def _sparql_files(directory: Path | None) -> list[Path]:
    if not directory or not directory.exists():
        return []
    return sorted(directory.glob("*.rq"))


def _fmt_row(row) -> str:
    return ", ".join(str(v) for v in row if v is not None)


def _expect_ok(expect: str, n: int) -> bool:
    expect = expect.replace(" ", "")
    for op in (">=", "<=", ">", "<", "=="):
        if expect.startswith(op):
            target = int(expect[len(op):])
            return {">=": n >= target, "<=": n <= target, ">": n > target,
                    "<": n < target, "==": n == target}[op]
    return n == int(expect)


# ---------------------------------------------------------------- checks

def check_format(cfg: Config, report: Report) -> None:
    name = "format"
    if graphs.is_normalized(cfg.ontology):
        report.passed.append(name)
    else:
        report.add(name, f"{cfg.ontology.name} is not normalized. Run `ontman normalize`.")


def check_violations(cfg: Config, onto: Graph, report: Report) -> None:
    """Every .rq in the violations folder must return zero rows."""
    for path in _sparql_files(cfg.violations_dir):
        text = path.read_text()
        h = query_headers(text)
        name = f"violation:{path.stem}"
        rows = list(onto.query(text))
        if not rows:
            report.passed.append(name)
            continue
        desc = h.get("description", path.stem)
        sev = h.get("severity", "error")
        for row in rows:
            report.add(name, f"{desc}: {_fmt_row(row)}", sev)


def check_undefined_terms(cfg: Config, onto: Graph, ns: str, report: Report) -> None:
    """Catch typos like :Morgage: our-namespace IRIs used but never declared."""
    name = "undefined-terms"
    declared = graphs.declared_terms(onto)
    found = False

    def scan(g: Graph, label: str, positions: str) -> None:
        nonlocal found
        used: set[URIRef] = set()
        for s, p, o in g:
            if "p" in positions:
                used.add(p)
            if "c" in positions and p == RDF.type:
                used.add(o)
            if "o" in positions:
                used.add(o)
            if "s" in positions:
                used.add(s)
        for iri in sorted(used):
            if isinstance(iri, URIRef) and str(iri).startswith(ns) and iri not in declared:
                found = True
                report.add(name, f"{label} uses undeclared term {graphs.local_name(iri, ns)}")

    scan(onto, cfg.ontology.name, "spo")
    for f in cfg.fixtures:
        # fixtures may mint individuals freely; only classes and properties must exist
        scan(graphs.load(f), f.name, "pc")
    if not found:
        report.passed.append(name)


def check_deprecations(cfg: Config, onto: Graph, ns: str, report: Report) -> None:
    name = "deprecations"
    deprecated = {s for s in onto.subjects(OWL.deprecated, Literal(True))}
    ok = True
    for term in sorted(deprecated):
        if not list(onto.objects(term, graphs.DCTERMS.isReplacedBy)):
            ok = False
            report.add(name, f"{graphs.local_name(term, ns)} is deprecated but has no dcterms:isReplacedBy",
                       "warning")
    for f in cfg.fixtures:
        g = graphs.load(f)
        for s, p, o in g:
            for t in (p, o):
                if t in deprecated:
                    ok = False
                    report.add(name, f"{f.name} still uses deprecated term {graphs.local_name(t, ns)}",
                               "warning")
    if ok:
        report.passed.append(name)


def check_consistency_and_competency(cfg: Config, onto: Graph, report: Report) -> None:
    """Reason over ontology + fixtures, then run the competency questions."""
    data = [graphs.load(f) for f in cfg.fixtures]
    result = reason(onto, *data)

    if result.errors:
        for e in result.errors:
            report.add("consistency", e)
    else:
        report.passed.append("consistency")

    for path in _sparql_files(cfg.competency_dir):
        text = path.read_text()
        h = query_headers(text)
        name = f"competency:{path.stem}"
        n = len(list(result.closure.query(text)))
        expect = h.get("expect", ">=1")
        if _expect_ok(expect, n):
            report.passed.append(name)
        else:
            q = h.get("question", path.stem)
            report.add(name, f'"{q}" returned {n} rows, expected {expect}')


def run_all(cfg: Config, skip_format: bool = False) -> Report:
    report = Report()
    try:
        onto = graphs.load(cfg.ontology)
    except Exception as exc:  # noqa: BLE001 - surface any parse error as a finding
        report.add("parse", f"{cfg.ontology.name} does not parse: {exc}")
        return report
    report.passed.append("parse")
    ns = graphs.base_namespace(onto, cfg.namespace)

    if not skip_format:
        check_format(cfg, report)
    check_violations(cfg, onto, report)
    check_undefined_terms(cfg, onto, ns, report)
    check_deprecations(cfg, onto, ns, report)
    check_consistency_and_competency(cfg, onto, report)
    return report
