"""Semantic diff between two versions of an ontology, with a semver suggestion.

Rules for the suggested version bump:
  major  a term was removed, or a logical axiom was removed or changed
         (existing queries or inferences may break)
  minor  terms or logical axioms were added, or a term was deprecated
  patch  only labels, comments and other annotations changed
  none   no change in meaning
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.compare import graph_diff, to_isomorphic

from . import graphs

LOGICAL = {
    RDF.type, RDFS.subClassOf, RDFS.subPropertyOf, RDFS.domain, RDFS.range,
    OWL.equivalentClass, OWL.equivalentProperty, OWL.disjointWith, OWL.inverseOf,
    OWL.propertyChainAxiom,
}
IGNORED = {OWL.versionInfo}
LEVELS = ["none", "patch", "minor", "major"]


@dataclass
class TermChange:
    term: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


@dataclass
class DiffResult:
    added_terms: list[str] = field(default_factory=list)
    removed_terms: list[str] = field(default_factory=list)
    deprecated_terms: list[str] = field(default_factory=list)
    changed: list[TermChange] = field(default_factory=list)
    bump: str = "none"
    reasons: list[str] = field(default_factory=list)
    old_version: str | None = None
    new_version: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.bump == "none"


def _owner(g: Graph, node, seen=None):
    """Find the named term a blank node belongs to (e.g. a restriction's class)."""
    seen = seen or set()
    while isinstance(node, BNode) and node not in seen:
        seen.add(node)
        parents = list(g.subjects(None, node))
        if not parents:
            return None
        node = parents[0]
    return node if isinstance(node, URIRef) else None


def _short(term, ns: str) -> str:
    if isinstance(term, Literal):
        s = str(term)
        return f'"{s[:60]}…"' if len(s) > 60 else f'"{s}"'
    if isinstance(term, BNode):
        return "[…]"
    s = str(term)
    if s.startswith(ns):
        return ":" + s[len(ns):]
    for prefix, uri in (("rdf", RDF), ("rdfs", RDFS), ("owl", OWL)):
        if s.startswith(str(uri)):
            return f"{prefix}:{s[len(str(uri)):]}"
    if s.startswith(str(graphs.DCTERMS)):
        return "dcterms:" + s[len(str(graphs.DCTERMS)):]
    return f"<{s}>"


def _version(g: Graph) -> str | None:
    iri = graphs.ontology_iri(g)
    v = g.value(iri, OWL.versionInfo) if iri else None
    return str(v) if v else None


def diff(old: Graph, new: Graph, ns: str) -> DiffResult:
    res = DiffResult(old_version=_version(old), new_version=_version(new))
    _, only_old, only_new = graph_diff(to_isomorphic(old), to_isomorphic(new))

    old_terms, new_terms = graphs.declared_terms(old), graphs.declared_terms(new)
    res.added_terms = sorted(_short(t, ns) for t in new_terms - old_terms)
    res.removed_terms = sorted(_short(t, ns) for t in old_terms - new_terms)
    was_dep = set(old.subjects(OWL.deprecated, Literal(True)))
    res.deprecated_terms = sorted(
        _short(t, ns) for t in set(new.subjects(OWL.deprecated, Literal(True))) - was_dep
    )

    level = 0
    by_term: dict[str, TermChange] = defaultdict(lambda: TermChange(""))

    def record(triples: Graph, source: Graph, kind: str) -> None:
        nonlocal level
        for s, p, o in triples:
            if p in IGNORED:
                continue
            owner = _owner(source, s) if isinstance(s, BNode) else s
            key = _short(owner, ns) if owner is not None else "(anonymous)"
            tc = by_term[key]
            tc.term = key
            obj = "[complex axiom]" if isinstance(o, BNode) or isinstance(s, BNode) else _short(o, ns)
            line = f"{_short(p, ns)} {obj}"
            (tc.added if kind == "added" else tc.removed).append(line)

            logical = p in LOGICAL or isinstance(s, BNode) or isinstance(o, BNode)
            if kind == "removed" and logical:
                level = max(level, 3)
                res.reasons.append(f"removed logical axiom on {key}: {line}")
            elif logical or p == OWL.deprecated:
                level = max(level, 2)
            else:
                level = max(level, 1)

    record(only_old, old, "removed")
    record(only_new, new, "added")

    if res.removed_terms:
        level = 3
        res.reasons.insert(0, f"removed terms: {', '.join(res.removed_terms)}")
    if res.added_terms:
        level = max(level, 2)
        res.reasons.append(f"added terms: {', '.join(res.added_terms)}")
    if res.deprecated_terms:
        res.reasons.append(f"deprecated: {', '.join(res.deprecated_terms)}")

    # Terms that were wholly added or removed are already listed; skip their details.
    skip = set(res.added_terms) | set(res.removed_terms)
    res.changed = sorted((tc for k, tc in by_term.items() if k not in skip), key=lambda t: t.term)
    for tc in res.changed:
        tc.added.sort()
        tc.removed.sort()
    res.bump = LEVELS[level]
    # de-duplicate reasons, keep order
    res.reasons = list(dict.fromkeys(res.reasons))
    return res


def version_was_bumped(res: DiffResult) -> bool:
    return res.old_version != res.new_version


def to_markdown(res: DiffResult) -> str:
    if res.is_empty:
        return "### Ontology diff\n\nNo change in meaning.\n"
    out = ["### Ontology diff", ""]
    out.append(f"**Suggested version bump: `{res.bump}`**  ")
    out.append(f"Version: `{res.old_version}` → `{res.new_version}`")
    if not version_was_bumped(res):
        out.append("")
        out.append("> ⚠️ `owl:versionInfo` was not changed. Run `ontman bump " + res.bump + "`.")
    out.append("")
    for title, items in (("Added terms", res.added_terms), ("Removed terms ⚠️ breaking", res.removed_terms),
                         ("Newly deprecated", res.deprecated_terms)):
        if items:
            out.append(f"**{title}:** " + ", ".join(f"`{i}`" for i in items))
    if res.changed:
        out.append("")
        out.append("| Term | Change |")
        out.append("|---|---|")
        for tc in res.changed:
            for line in tc.removed:
                out.append(f"| `{tc.term}` | ➖ `{line}` |")
            for line in tc.added:
                out.append(f"| `{tc.term}` | ➕ `{line}` |")
    return "\n".join(out) + "\n"


def to_text(res: DiffResult) -> str:
    if res.is_empty:
        return "No change in meaning."
    out = [f"Suggested version bump: {res.bump}   (version {res.old_version} -> {res.new_version})"]
    if not version_was_bumped(res):
        out.append(f"  ! owl:versionInfo not changed. Run `ontman bump {res.bump}`.")
    for title, items in (("Added terms", res.added_terms), ("Removed terms (BREAKING)", res.removed_terms),
                         ("Newly deprecated", res.deprecated_terms)):
        if items:
            out.append(f"{title}: {', '.join(items)}")
    for tc in res.changed:
        out.append(f"  {tc.term}")
        out.extend(f"    - {line}" for line in tc.removed)
        out.extend(f"    + {line}" for line in tc.added)
    return "\n".join(out)
