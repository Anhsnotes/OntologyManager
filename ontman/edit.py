"""Safe, scripted edits: add terms, deprecate terms, bump the version.

Every edit loads the ontology, changes the graph, and saves it in canonical
form, so diffs stay clean no matter who makes the change.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from . import graphs


class EditError(ValueError):
    pass


def _iri(ns: str, name: str) -> URIRef:
    name = name.lstrip(":")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-]*", name):
        raise EditError(f"'{name}' is not a valid local name (letters, digits, _ or -).")
    return URIRef(ns + name)


def _require(g: Graph, iri: URIRef, ns: str, what: str = "term") -> None:
    if iri not in graphs.declared_terms(g):
        raise EditError(f"{what} '{graphs.local_name(iri, ns)}' does not exist in the ontology.")


def _forbid(g: Graph, iri: URIRef, ns: str) -> None:
    if iri in graphs.declared_terms(g):
        raise EditError(f"'{graphs.local_name(iri, ns)}' already exists.")


def add_class(g: Graph, ns: str, name: str, label: str, comment: str,
              parent: str | None = None, disjoint_with: list[str] | None = None) -> URIRef:
    cls = _iri(ns, name)
    _forbid(g, cls, ns)
    g.add((cls, RDF.type, OWL.Class))
    g.add((cls, RDFS.label, Literal(label)))
    g.add((cls, RDFS.comment, Literal(comment)))
    if parent:
        p = _iri(ns, parent)
        _require(g, p, ns, "parent class")
        g.add((cls, RDFS.subClassOf, p))
    for d in disjoint_with or []:
        di = _iri(ns, d)
        _require(g, di, ns, "disjoint class")
        g.add((cls, OWL.disjointWith, di))
    return cls


DATATYPES = {"string": XSD.string, "decimal": XSD.decimal, "integer": XSD.integer,
             "date": XSD.date, "dateTime": XSD.dateTime, "boolean": XSD.boolean}


def add_property(g: Graph, ns: str, name: str, label: str, comment: str, kind: str = "object",
                 domain: str | None = None, range_: str | None = None,
                 transitive: bool = False, inverse_of: str | None = None) -> URIRef:
    prop = _iri(ns, name)
    _forbid(g, prop, ns)
    if kind == "object":
        g.add((prop, RDF.type, OWL.ObjectProperty))
    elif kind == "data":
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        if transitive or inverse_of:
            raise EditError("Data properties cannot be transitive or have an inverse.")
    else:
        raise EditError("kind must be 'object' or 'data'.")
    g.add((prop, RDFS.label, Literal(label)))
    g.add((prop, RDFS.comment, Literal(comment)))
    if domain:
        d = _iri(ns, domain)
        _require(g, d, ns, "domain class")
        g.add((prop, RDFS.domain, d))
    if range_:
        if kind == "data":
            if range_ not in DATATYPES:
                raise EditError(f"Data range must be one of: {', '.join(DATATYPES)}")
            g.add((prop, RDFS.range, DATATYPES[range_]))
        else:
            r = _iri(ns, range_)
            _require(g, r, ns, "range class")
            g.add((prop, RDFS.range, r))
    if transitive:
        g.add((prop, RDF.type, OWL.TransitiveProperty))
    if inverse_of:
        inv = _iri(ns, inverse_of)
        _require(g, inv, ns, "inverse property")
        g.add((prop, OWL.inverseOf, inv))
    return prop


def deprecate(g: Graph, ns: str, name: str, replaced_by: str | None, reason: str | None = None) -> URIRef:
    """Mark a term deprecated instead of deleting it, so existing queries keep working."""
    term = _iri(ns, name)
    _require(g, term, ns)
    if (term, OWL.deprecated, Literal(True)) in g:
        raise EditError(f"'{name}' is already deprecated.")
    g.add((term, OWL.deprecated, Literal(True)))
    # OWL DL requires annotation properties to be declared.
    g.add((graphs.DCTERMS.isReplacedBy, RDF.type, OWL.AnnotationProperty))
    if replaced_by:
        new = _iri(ns, replaced_by)
        _require(g, new, ns, "replacement")
        g.add((term, graphs.DCTERMS.isReplacedBy, new))
    note = "Deprecated" + (f": {reason}" if reason else "") + (f" Use {replaced_by} instead." if replaced_by else "")
    g.add((term, RDFS.comment, Literal(note.strip())))
    return term


# ---------------------------------------------------------------- versioning

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def next_version(current: str, part: str) -> str:
    m = _SEMVER.match(current or "")
    if not m:
        raise EditError(f"owl:versionInfo '{current}' is not MAJOR.MINOR.PATCH.")
    major, minor, patch = map(int, m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise EditError("part must be major, minor or patch.")


def bump(g: Graph, part: str) -> tuple[str, str]:
    iri = graphs.ontology_iri(g)
    if iri is None:
        raise EditError("No owl:Ontology header found.")
    current = str(g.value(iri, OWL.versionInfo) or "0.0.0")
    new = next_version(current, part)
    g.set((iri, OWL.versionInfo, Literal(new)))
    return current, new


def release_changelog(path: Path, version: str, today: dt.date | None = None) -> bool:
    """Turn the '## [Unreleased]' section into '## [version] - date'."""
    if not path.exists():
        return False
    text = path.read_text()
    marker = "## [Unreleased]"
    if marker not in text:
        return False
    date = (today or dt.date.today()).isoformat()
    text = text.replace(marker, f"{marker}\n\n## [{version}] - {date}", 1)
    path.write_text(text)
    return True
