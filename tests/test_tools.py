"""Tests for the agent tools (ontman.tools), the MCP wrapper and the example agent loop.

Like test_ontman.py, these use the frozen sample project so they never depend on the
live ontology.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from ontman.config import load_config
from ontman.tools import EndpointBackend, LocalBackend, OntologyTools

SAMPLE = Path(__file__).resolve().parent / "toolkit" / "sample_project"
P = "PREFIX : <http://example.org/bank#>\n"


@pytest.fixture(scope="module")
def tools() -> OntologyTools:
    return OntologyTools(load_config(SAMPLE))


# ---------------------------------------------------------------- discovery

def test_search_finds_terms_and_flags_deprecated(tools):
    hits = {h["term"]: h for h in tools.search_terms("loan")}
    assert {":Loan", ":Mortgage", ":SecuredLoan"} <= set(hits)
    assert hits[":HomeLoan"]["deprecated"] and hits[":HomeLoan"]["use_instead"] == [":Mortgage"]


def test_search_suggests_on_typo(tools):
    assert tools.search_terms("morgage")[0]["did_you_mean"] == [":Mortgage"]


def test_describe_class_shows_definition_and_usable_properties(tools):
    d = tools.describe_term(":SecuredLoan")
    assert d["defined_as"] == [":Loan and (:hasCollateral some :Collateral)"]
    assert ":Loan" in d["all_ancestors"]
    assert ":hasCollateral" in d["properties_from_here"]


def test_describe_property(tools):
    d = tools.describe_term("locatedIn")
    assert d["transitive"] is True
    assert tools.describe_term("hasBorrower")["inverse_of"] == [":borrowerOf"]


def test_find_instances_returns_exact_iris(tools):
    assert tools.find_instances(":State", "illin") == [{"iri": ":Illinois"}]
    # reasoning: branches are Organizations, so they show up as instances of Party
    assert {"iri": ":ChicagoLoop"} in tools.find_instances(":Party")


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("query, expected", [
    (P + "SELECT ?l WHERE { ?l a :SecureLoan }", "Did you mean :SecuredLoan"),
    (P + "SELECT ?l WHERE { ?l :hasBorrowr ?p }", "Unknown property :hasBorrowr"),
    (P + "SELECT ?l WHERE { ?l ^:hasBorrowr ?p }", "Unknown property :hasBorrowr"),   # path
    (P + "INSERT DATA { :a a :Loan }", "Updates are not allowed"),
    ("SELEC ?x WHERE { ?x ?p ?o }", "syntax error"),
])
def test_invalid_queries_are_rejected_with_actionable_errors(tools, query, expected):
    res = tools.validate_sparql(query)
    assert res["valid"] is False
    assert any(expected in e for e in res["errors"])


def test_unknown_individual_and_deprecated_term_are_warnings(tools):
    res = tools.validate_sparql(P + "SELECT ?l WHERE { ?l a :HomeLoan ; :originatedAt :NoSuchBranch }")
    assert res["valid"] is True
    text = " ".join(res["warnings"])
    assert ":HomeLoan is deprecated; use :Mortgage" in text
    assert ":NoSuchBranch does not appear in the data" in text


# ---------------------------------------------------------------- running

def test_run_applies_reasoning(tools):
    q = P + """SELECT (SUM(?amt) AS ?total) (COUNT(?loan) AS ?n) WHERE {
        ?loan a :SecuredLoan ; :originatedAt ?b ; :amount ?amt . ?b :locatedIn :Illinois . }"""
    res = tools.run_sparql(q)
    assert res["ok"] and res["rows"] == [{"total": 280000, "n": 2}]


def test_run_ask_returns_boolean(tools):
    assert tools.run_sparql(P + "ASK { :loan1 a :SecuredLoan }")["rows"] == [{"result": True}]


def test_run_caps_rows(tools):
    small = OntologyTools(load_config(SAMPLE), backend=tools.backend, max_rows=1)
    res = small.run_sparql(P + "SELECT ?l WHERE { ?l a :Loan }")
    assert res["row_count"] == 1 and "truncated" in res
    res = small.run_sparql(P + "SELECT ?l WHERE { ?l a :Loan } LIMIT 1000")
    assert res["row_count"] == 1


def test_run_empty_result_gives_hint(tools):
    res = tools.run_sparql(P + "SELECT ?l WHERE { ?l :originatedAt :Nowhere }")
    assert res["row_count"] == 0 and "find_instances" in res["hint"]


def test_call_never_raises(tools):
    assert tools.call("describe_term", {"term": "Lone"}).startswith("Error: Unknown term :Lone. Did you mean :Loan")
    assert tools.call("nope", {}).startswith("Error: unknown tool")
    assert tools.call("search_terms", {"wrong": 1}).startswith("Error: bad arguments")
    assert json.loads(tools.call("run_sparql", {"query": P + "ASK { :loan1 a :Loan }"}))["ok"]


def test_tool_specs_match_functions(tools):
    names = {t["name"] for t in tools.anthropic_tools()}
    assert names == set(tools.functions()) == {f["function"]["name"] for f in tools.openai_tools()}
    for t in tools.anthropic_tools():
        assert t["description"] and t["input_schema"]["type"] == "object"


# ---------------------------------------------------------------- remote endpoint backend

@pytest.fixture(scope="module")
def endpoint(tools):
    """A tiny SPARQL endpoint over the reasoned sample graph, speaking the HTTP protocol."""
    graph = tools.backend.graph

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            q = urllib.parse.parse_qs(body)["query"][0]
            res = graph.query(q)
            if res.type in ("CONSTRUCT", "DESCRIBE"):
                out, ctype = res.graph.serialize(format="turtle").encode(), "text/turtle"
            else:
                out, ctype = res.serialize(format="json"), "application/sparql-results+json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/sparql"
    server.shutdown()


def test_endpoint_backend_gives_same_answers(tools, endpoint):
    remote = OntologyTools(load_config(SAMPLE), backend=EndpointBackend(endpoint))
    q = P + "SELECT ?loan WHERE { ?loan a :Loan ; :originatedAt/:locatedIn :Midwest } ORDER BY ?loan"
    assert remote.run_sparql(q)["rows"] == tools.run_sparql(q)["rows"]
    assert remote.find_instances(":State", "illin") == [{"iri": ":Illinois"}]
    assert any("does not appear" in w for w in
               remote.validate_sparql(P + "SELECT * WHERE { ?x :locatedIn :Atlantis }")["warnings"])
    assert remote.run_sparql(P + "ASK { :loan1 a :SecuredLoan }")["rows"] == [{"result": True}]


def test_unreachable_endpoint_is_a_clean_error():
    t = OntologyTools(load_config(SAMPLE), backend=EndpointBackend("http://127.0.0.1:9/sparql"))
    assert t.call("run_sparql", {"query": P + "SELECT ?x WHERE { ?x a :Loan }"}).startswith(
        "Error: Could not reach the SPARQL endpoint")


# ---------------------------------------------------------------- MCP wrapper

def test_mcp_server_exposes_same_tools(tools):
    pytest.importorskip("mcp")
    from ontman.mcp_server import build_server

    server = build_server(tools)

    async def go():
        listed = await server.list_tools()
        assert {t.name for t in listed} == set(tools.functions())
        assert all(t.annotations.read_only_hint for t in listed)
        res = await server.call_tool("run_sparql", {"query": P + "SELECT ?l WHERE { ?l a :SecureLoan }"})
        assert "Did you mean :SecuredLoan" in res.content[0].text
        contents = list(await server.read_resource("ontology://schema"))
        assert "## Classes" in contents[0].content

    asyncio.run(go())


# ---------------------------------------------------------------- example agent loop

def test_example_agent_runs_tools_and_returns_answer(tools):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    import agent

    class StubClient:
        """Plays the model: one tool call, then an answer built from the tool result."""

        def __init__(self):
            self.messages = SimpleNamespace(create=self.create)
            self.calls = 0

        def create(self, **kw):
            self.calls += 1
            assert kw["tools"] == tools.anthropic_tools()
            if self.calls == 1:
                return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
                    type="tool_use", id="t1", name="find_instances",
                    input={"cls": ":State", "text": "illinois"})])
            result = kw["messages"][-1]["content"][0]
            assert result["tool_use_id"] == "t1" and ":Illinois" in result["content"]
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text="Found :Illinois")])

    client = StubClient()
    assert agent.ask("Which state is Illinois?", tools=tools, client=client, verbose=False) == "Found :Illinois"
    assert client.calls == 2
