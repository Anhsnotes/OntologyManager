"""Tests for the ontman toolkit itself (not for the ontology's content).

These tests run against a frozen copy of the sample project in
tests/toolkit/sample_project, so they keep passing however src/ontology.ttl
evolves. Only test_repo_ontology_passes_all_checks looks at the live ontology.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest
from rdflib import Graph

from ontman import checks, edit, graphs
from ontman.config import load_config
from ontman.diff import diff

REPO = Path(__file__).resolve().parents[1]
SAMPLE = Path(__file__).resolve().parent / "toolkit" / "sample_project"
NS = "http://example.org/bank#"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway copy of the frozen sample project."""
    dst = tmp_path / "project"
    shutil.copytree(SAMPLE, dst)
    return dst


def run_check(project: Path) -> checks.Report:
    return checks.run_all(load_config(project))


def errors_for(report: checks.Report, prefix: str) -> list[str]:
    return [f.message for f in report.errors if f.check.startswith(prefix)]


# ---------------------------------------------------------------- normalize

def test_repo_ontology_passes_all_checks():
    report = checks.run_all(load_config(REPO))
    assert report.errors == [], [f.message for f in report.errors]


def test_sample_project_passes_all_checks():
    report = checks.run_all(load_config(SAMPLE))
    assert report.errors == [], [f.message for f in report.errors]


def test_canonical_output_ignores_triple_order():
    g = graphs.load(SAMPLE / "src/ontology.ttl")
    expected = graphs.canonical(g)
    triples = list(g)
    random.Random(42).shuffle(triples)
    shuffled = Graph()
    for prefix, ns in g.namespaces():
        shuffled.bind(prefix, ns)
    for t in triples:
        shuffled.add(t)
    assert graphs.canonical(shuffled) == expected


def test_adding_a_reference_does_not_move_other_blocks(project):
    onto = project / "src/ontology.ttl"
    before = onto.read_text().splitlines()
    g = graphs.load(onto)
    edit.add_property(g, NS, "hasCoBorrower", "has co-borrower", "Joint borrower.",
                      domain="Loan", range_="Person")
    graphs.save(g, onto)
    after = onto.read_text().splitlines()
    removed = [line for line in before if line not in after]
    assert removed == []  # only additions, nothing reshuffled


# ---------------------------------------------------------------- checks

def test_typo_in_fixture_is_caught(project):
    f = project / "tests/fixtures/sample-data.ttl"
    f.write_text(f.read_text().replace("a :Mortgage", "a :Morgage"))
    assert any("Morgage" in m for m in errors_for(run_check(project), "undefined-terms"))


def test_missing_definition_is_caught(project):
    g = graphs.load(project / "src/ontology.ttl")
    from rdflib import OWL, RDF, URIRef
    g.add((URIRef(NS + "Lease"), RDF.type, OWL.Class))
    graphs.save(g, project / "src/ontology.ttl")
    report = run_check(project)
    assert errors_for(report, "violation:missing-definition")
    assert errors_for(report, "violation:missing-label")


def test_disjointness_clash_is_caught(project):
    f = project / "tests/fixtures/sample-data.ttl"
    f.write_text(f.read_text() + "\n:alice a :Organization .\n")
    msgs = errors_for(run_check(project), "consistency")
    assert any("alice" in m for m in msgs)


def test_broken_inference_fails_competency_question(project):
    onto = project / "src/ontology.ttl"
    onto.write_text(onto.read_text().replace("owl:TransitiveProperty", "owl:ObjectProperty"))
    graphs.save(graphs.load(onto), onto)
    assert errors_for(run_check(project), "competency:loans-in-midwest")


def test_unnormalized_file_fails_format(project):
    onto = project / "src/ontology.ttl"
    onto.write_text(graphs.load(onto).serialize(format="turtle"))
    assert errors_for(run_check(project), "format")


# ---------------------------------------------------------------- edits

def test_edit_guards(project):
    g = graphs.load(project / "src/ontology.ttl")
    with pytest.raises(edit.EditError, match="already exists"):
        edit.add_class(g, NS, "Loan", "Loan", "dup")
    with pytest.raises(edit.EditError, match="does not exist"):
        edit.add_class(g, NS, "Heloc", "HELOC", "x", parent="NoSuchClass")
    with pytest.raises(edit.EditError, match="valid local name"):
        edit.add_class(g, NS, "bad name", "x", "y")


def test_version_bumps():
    assert edit.next_version("1.2.3", "patch") == "1.2.4"
    assert edit.next_version("1.2.3", "minor") == "1.3.0"
    assert edit.next_version("1.2.3", "major") == "2.0.0"


# ---------------------------------------------------------------- diff

def _versions(project):
    old = graphs.load(project / "src/ontology.ttl")
    new = graphs.load(project / "src/ontology.ttl")
    return old, new


def test_label_change_is_patch(project):
    old, new = _versions(project)
    from rdflib import RDFS, Literal, URIRef
    new.set((URIRef(NS + "Loan"), RDFS.label, Literal("Credit facility")))
    assert diff(old, new, NS).bump == "patch"


def test_new_class_is_minor(project):
    old, new = _versions(project)
    edit.add_class(new, NS, "Heloc", "HELOC", "Home equity line.", parent="Loan")
    res = diff(old, new, NS)
    assert res.bump == "minor" and ":Heloc" in res.added_terms


def test_deprecation_is_minor(project):
    old, new = _versions(project)
    edit.deprecate(new, NS, "AutoLoan", "Loan")
    res = diff(old, new, NS)
    assert res.bump == "minor" and ":AutoLoan" in res.deprecated_terms


def test_removing_a_term_is_major(project):
    old, new = _versions(project)
    from rdflib import URIRef
    new.remove((URIRef(NS + "Vehicle"), None, None))
    res = diff(old, new, NS)
    assert res.bump == "major" and ":Vehicle" in res.removed_terms


def test_removing_an_axiom_is_major(project):
    old, new = _versions(project)
    from rdflib import RDFS, URIRef
    new.remove((URIRef(NS + "Mortgage"), RDFS.subClassOf, URIRef(NS + "Loan")))
    assert diff(old, new, NS).bump == "major"


def test_no_change_is_none(project):
    old, new = _versions(project)
    assert diff(old, new, NS).is_empty
