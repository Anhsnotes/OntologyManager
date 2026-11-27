"""OWL 2 RL reasoning with owlrl: inferred facts and consistency errors.

This is a fast local check. CI also runs a full OWL DL reasoner (HermiT via
ROBOT), which catches problems OWL RL cannot see, such as unsatisfiable classes.
"""

from __future__ import annotations

from dataclasses import dataclass

import owlrl
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

AGENT = Namespace("http://www.daml.org/2002/03/agents/agent-ont#")


@dataclass
class ReasoningResult:
    closure: Graph
    errors: list[str]


def reason(*graphs: Graph) -> ReasoningResult:
    merged = Graph()
    for g in graphs:
        for prefix, ns in g.namespaces():
            merged.bind(prefix, ns, override=False)
        merged += g
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(merged)

    errors = sorted(str(o) for o in merged.objects(None, AGENT.error))
    errors += sorted(
        f"{s} is inferred to be owl:Nothing (it cannot exist)"
        for s in merged.subjects(RDF.type, OWL.Nothing)
        if isinstance(s, URIRef)
    )
    return ReasoningResult(closure=merged, errors=errors)


def new_facts(asserted: Graph, closure: Graph, namespace: str) -> list[tuple]:
    """Inferred triples about our own terms that nobody wrote down.

    Drops the noise OWL RL produces (owl:Thing types, x sameAs x, the built-in
    vocabulary) so what is left is worth showing to a person.
    """
    def ours(t) -> bool:
        return isinstance(t, URIRef) and str(t).startswith(namespace)

    out = []
    for s, p, o in closure:
        if (s, p, o) in asserted or s == o:
            continue
        # keep only facts linking two of our own terms, e.g.
        # :loan1 rdf:type :SecuredLoan  or  :Chicago :locatedIn :Midwest
        if not (ours(s) and ours(o)):
            continue
        if p not in (RDF.type, RDFS.subClassOf, RDFS.subPropertyOf, OWL.equivalentClass) and not ours(p):
            continue
        out.append((s, p, o))
    return sorted(out)
