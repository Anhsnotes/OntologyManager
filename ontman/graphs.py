"""Loading, saving and normalizing Turtle files."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

from rdflib import OWL, RDF, BNode, Graph, Namespace, URIRef
from rdflib.plugins.serializers.longturtle import LongTurtleSerializer

# rdflib's "longturtle" writer sorts subjects and predicates and nests blank
# nodes, so the same set of triples always produces the same text. That is
# what keeps git diffs limited to real changes.
CANONICAL_FORMAT = "longturtle"

DCTERMS = Namespace("http://purl.org/dc/terms/")

TERM_TYPES = {
    OWL.Class,
    OWL.ObjectProperty,
    OWL.DatatypeProperty,
    OWL.AnnotationProperty,
    OWL.NamedIndividual,
}


def load(path: Path | str) -> Graph:
    g = Graph()
    g.parse(str(path), format="turtle")
    return g


def load_text(text: str) -> Graph:
    g = Graph()
    g.parse(data=text, format="turtle")
    return g


def load_from_git(ref: str, path: Path, root: Path) -> Graph:
    """Load a file as it exists at a git ref, e.g. 'origin/main'."""
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    text = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout
    return load_text(text)


class _StableLongTurtle(LongTurtleSerializer):
    """longturtle with a subject order that never depends on the rest of the file.

    rdflib's default orders subjects partly by how often they are referenced,
    so adding `rdfs:range :Person` somewhere would move the whole :Person block.
    Here the order is: ontology header, classes, object properties, datatype
    properties, other properties, everything else; alphabetical within a group.
    """

    _RANK = [OWL.Ontology, OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty,
             OWL.AnnotationProperty]

    def _rank(self, s) -> int:
        types = set(self.store.objects(s, RDF.type))
        for i, t in enumerate(self._RANK):
            if t in types:
                return i
        return len(self._RANK)

    def orderSubjects(self):  # noqa: N802 - rdflib API name
        subjects = list(self._subjects)
        keyed = [(isinstance(s, BNode), self._rank(s), str(s), s) for s in subjects]
        keyed.sort(key=lambda k: k[:3])
        return [k[3] for k in keyed]


def canonical(g: Graph) -> str:
    buf = io.BytesIO()
    _StableLongTurtle(g).serialize(buf, encoding="utf-8")
    return buf.getvalue().decode("utf-8")


def save(g: Graph, path: Path | str) -> None:
    Path(path).write_text(canonical(g))


def is_normalized(path: Path | str) -> bool:
    text = Path(path).read_text()
    return text == canonical(load_text(text))


def base_namespace(g: Graph, configured: str | None = None) -> str:
    """The namespace our own terms live in: from config, else the ':' prefix."""
    if configured:
        return configured
    for prefix, ns in g.namespaces():
        if prefix == "":
            return str(ns)
    raise ValueError(
        "No namespace configured and no default ':' prefix in the ontology. "
        "Set `namespace` in ontman.toml."
    )


def declared_terms(g: Graph) -> set[URIRef]:
    """IRIs that the ontology declares as classes, properties or individuals."""
    terms: set[URIRef] = set()
    for t in TERM_TYPES:
        terms.update(s for s in g.subjects(RDF.type, t) if isinstance(s, URIRef))
    for t in (OWL.TransitiveProperty, OWL.SymmetricProperty, OWL.FunctionalProperty):
        terms.update(s for s in g.subjects(RDF.type, t) if isinstance(s, URIRef))
    return terms


def ontology_iri(g: Graph) -> URIRef | None:
    for s in g.subjects(RDF.type, OWL.Ontology):
        if isinstance(s, URIRef):
            return s
    return None


def local_name(iri: URIRef | str, ns: str) -> str:
    s = str(iri)
    return s[len(ns):] if s.startswith(ns) else s
