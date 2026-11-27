"""Ontology query tools for LLM agents.

One implementation, three ways to use it:
  1. Import directly in Python:          tools = OntologyTools.from_config(); tools.search_terms("loan")
  2. Function calling in your own agent:  tools.anthropic_tools() + tools.call(name, args)
  3. MCP for any client:                  `ontman serve` (see ontman/mcp_server.py)

Backends:
  - local:    the ontology + fixtures in this repo, with OWL RL reasoning. Good for
              building and testing an agent before any database is connected.
  - endpoint: any SPARQL endpoint, e.g. Ontop over SQL. Set `endpoint` in ontman.toml.

Guardrails (applied whichever way the tools are called):
  - read-only: SELECT / ASK / CONSTRUCT / DESCRIBE only, never UPDATE
  - validation before running: syntax, and every term checked against the ontology,
    with "did you mean" suggestions for typos
  - a row cap and a timeout on every query
  - errors returned as plain text the model can act on, never stack traces
"""

from __future__ import annotations

import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.paths import Path
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate

from . import graphs
from .config import Config, load_config
from .export import _Renderer
from .reasoning import reason

DEFAULT_MAX_ROWS = 200
DEFAULT_TIMEOUT_S = 30
READ_FORMS = ("SelectQuery", "AskQuery", "ConstructQuery", "DescribeQuery")


class ToolError(ValueError):
    """An error worth showing to the model as-is."""


# ---------------------------------------------------------------- backends

class LocalBackend:
    """Ontology + fixture data in memory, with OWL RL reasoning applied once."""

    name = "local"

    def __init__(self, onto: Graph, data: list[Graph]):
        self.graph = reason(onto, *data).closure

    def select(self, query: str, timeout: float) -> tuple[list[str], list[dict]]:
        res = self.graph.query(query)
        if res.type == "ASK":
            return ["result"], [{"result": bool(res.askAnswer)}]
        if res.type in ("CONSTRUCT", "DESCRIBE"):
            return ["subject", "predicate", "object"], [
                {"subject": s, "predicate": p, "object": o} for s, p, o in res.graph]
        cols = [str(v) for v in res.vars]
        return cols, [{c: row[c] for c in cols} for row in res]

    def exists(self, iri: URIRef) -> bool:
        return (iri, None, None) in self.graph or (None, None, iri) in self.graph


class EndpointBackend:
    """A remote SPARQL endpoint (Ontop, GraphDB, Fuseki, Stardog...). Read-only."""

    name = "endpoint"

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = headers or {}

    def _post(self, query: str, timeout: float, accept: str) -> bytes:
        body = urllib.parse.urlencode({"query": query}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Accept": accept, **self.headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise ToolError(f"Endpoint rejected the query (HTTP {exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ToolError(f"Could not reach the SPARQL endpoint {self.url}: {exc}") from exc

    def select(self, query: str, timeout: float) -> tuple[list[str], list[dict]]:
        form = _query_form(query)
        if form in ("ConstructQuery", "DescribeQuery"):
            g = Graph().parse(data=self._post(query, timeout, "text/turtle"), format="turtle")
            return ["subject", "predicate", "object"], [
                {"subject": s, "predicate": p, "object": o} for s, p, o in g]
        data = json.loads(self._post(query, timeout, "application/sparql-results+json"))
        if "boolean" in data:
            return ["result"], [{"result": data["boolean"]}]
        cols = data["head"]["vars"]
        rows = []
        for b in data["results"]["bindings"]:
            row = {}
            for c in cols:
                v = b.get(c)
                if v is None:
                    row[c] = None
                elif v["type"] == "uri":
                    row[c] = URIRef(v["value"])
                elif v["type"] == "bnode":
                    row[c] = BNode(v["value"])
                else:
                    row[c] = Literal(v["value"], lang=v.get("xml:lang"),
                                     datatype=v.get("datatype"))
            rows.append(row)
        return cols, rows

    def exists(self, iri: URIRef) -> bool:
        q = f"ASK {{ {{ <{iri}> ?p ?o }} UNION {{ ?s ?p <{iri}> }} }}"
        return self.select(q, DEFAULT_TIMEOUT_S)[1][0]["result"]


# ---------------------------------------------------------------- helpers

def _query_form(query: str) -> str:
    return parseQuery(query)[1].name


_LIMIT = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict[str, dict]
    required: list[str]


# ---------------------------------------------------------------- the tools

class OntologyTools:
    """The tool set an agent uses to explore the ontology and query data."""

    def __init__(self, cfg: Config, backend: LocalBackend | EndpointBackend | None = None,
                 max_rows: int = DEFAULT_MAX_ROWS, timeout: float = DEFAULT_TIMEOUT_S):
        self.cfg = cfg
        self.onto = graphs.load(cfg.ontology)
        self.ns = graphs.base_namespace(self.onto, cfg.namespace)
        self.max_rows = max_rows
        self.timeout = timeout
        self.backend = backend or LocalBackend(self.onto, [graphs.load(f) for f in cfg.fixtures])
        self._r = _Renderer(self.onto, self.ns)
        self._terms = graphs.declared_terms(self.onto)
        self._deprecated = set(self.onto.subjects(OWL.deprecated, Literal(True)))

    @classmethod
    def from_config(cls, start=None, endpoint: str | None = None, **kw) -> "OntologyTools":
        cfg = load_config(start)
        url = endpoint or cfg.endpoint
        backend = EndpointBackend(url) if url else None
        return cls(cfg, backend=backend, **kw)

    # ------------------------------------------------------------ naming
    def _short(self, t) -> Any:
        if isinstance(t, URIRef):
            s = str(t)
            if s.startswith(self.ns):
                return ":" + s[len(self.ns):]
            return t.n3(self.onto.namespace_manager)
        if isinstance(t, Literal):
            return t.toPython() if t.datatype else str(t)
        if t is None or isinstance(t, (bool, int, float)):
            return t
        return str(t)

    def _iri(self, term: str) -> URIRef:
        term = term.strip()
        if term.startswith("<") and term.endswith(">"):
            return URIRef(term[1:-1])
        if term.startswith("http://") or term.startswith("https://"):
            return URIRef(term)
        return URIRef(self.ns + term.lstrip(":"))

    def _suggest(self, name: str, pool: set) -> list[str]:
        local = {str(t)[len(self.ns):]: t for t in pool if str(t).startswith(self.ns)}
        hits = difflib.get_close_matches(name.lstrip(":"), list(local), n=3, cutoff=0.6)
        return [":" + h for h in hits]

    def _kind(self, t) -> str:
        types = set(self.onto.objects(t, RDF.type))
        for typ, word in ((OWL.Class, "class"), (OWL.ObjectProperty, "relationship"),
                          (OWL.DatatypeProperty, "attribute"), (OWL.AnnotationProperty, "annotation")):
            if typ in types:
                return word
        return "individual"

    # ------------------------------------------------------------ tool 1
    def search_terms(self, text: str, limit: int = 10) -> list[dict]:
        """Find ontology classes and properties by name, label or definition."""
        needle = text.lower().strip()
        if not needle:
            raise ToolError("Give some text to search for.")
        scored = []
        for t in self._terms:
            if not str(t).startswith(self.ns):
                continue
            name = str(t)[len(self.ns):].lower()
            label = (self._r.label(t) or "").lower()
            comment = self._r.comment(t).lower()
            if needle == name or needle == label:
                score = 0
            elif needle in name or needle in label:
                score = 1
            elif needle in comment:
                score = 2
            else:
                continue
            scored.append((score, t in self._deprecated, str(t), t))
        if not scored:
            sugg = self._suggest(needle.replace(" ", ""), self._terms)
            return [{"note": f"No terms match '{text}'.", "did_you_mean": sugg}] if sugg else []
        out = []
        for _, dep, _, t in sorted(scored)[:limit]:
            item = {"term": self._short(t), "kind": self._kind(t),
                    "label": self._r.label(t), "definition": self._r.comment(t)}
            if dep:
                repl = [self._short(x) for x in self.onto.objects(t, graphs.DCTERMS.isReplacedBy)]
                item["deprecated"] = True
                item["use_instead"] = repl
            out.append(item)
        return out

    # ------------------------------------------------------------ tool 2
    def describe_term(self, term: str) -> dict:
        """Everything about one class or property: definition, hierarchy, rules, usage."""
        t = self._iri(term)
        if t not in self._terms:
            sugg = self._suggest(str(t)[len(self.ns):], self._terms)
            raise ToolError(f"Unknown term {self._short(t)}."
                            + (f" Did you mean {', '.join(sugg)}?" if sugg else ""))
        g, r = self.onto, self._r
        kind = self._kind(t)
        out: dict[str, Any] = {"term": self._short(t), "kind": kind, "label": r.label(t),
                               "definition": r.comment(t)}
        if t in self._deprecated:
            out["deprecated"] = True
            out["use_instead"] = [self._short(x) for x in g.objects(t, graphs.DCTERMS.isReplacedBy)]
        if kind == "class":
            closure = self.backend.graph if isinstance(self.backend, LocalBackend) else g
            out["parents"] = sorted(self._short(p) for p in g.objects(t, RDFS.subClassOf)
                                    if isinstance(p, URIRef))
            out["all_ancestors"] = sorted({self._short(p) for p in closure.objects(t, RDFS.subClassOf)
                                           if isinstance(p, URIRef) and p != t
                                           and str(p).startswith(self.ns)})
            out["children"] = sorted(self._short(c) for c in g.subjects(RDFS.subClassOf, t)
                                     if isinstance(c, URIRef))
            out["defined_as"] = [r.expr(e).replace("`", "").replace("**", "")
                                 for e in g.objects(t, OWL.equivalentClass)]
            out["disjoint_with"] = sorted({self._short(x) for x in g.objects(t, OWL.disjointWith)}
                                          | {self._short(x) for x in g.subjects(OWL.disjointWith, t)})
            ancestors = {t} | {p for p in closure.objects(t, RDFS.subClassOf) if isinstance(p, URIRef)}
            out["properties_from_here"] = sorted({self._short(p) for a in ancestors
                                                  for p in g.subjects(RDFS.domain, a)})
            out["properties_pointing_here"] = sorted({self._short(p) for a in ancestors
                                                      for p in g.subjects(RDFS.range, a)})
            if out["defined_as"]:
                out["note"] = "Defined class: membership is inferred. Query it with `?x a " \
                              f"{self._short(t)}`; data never asserts it."
        elif kind in ("relationship", "attribute"):
            out["domain"] = [self._short(d) for d in g.objects(t, RDFS.domain)] or ["any"]
            out["range"] = [self._short(x) for x in g.objects(t, RDFS.range)] or ["any"]
            types = set(g.objects(t, RDF.type))
            out["transitive"] = OWL.TransitiveProperty in types
            inv = set(g.objects(t, OWL.inverseOf)) | set(g.subjects(OWL.inverseOf, t))
            out["inverse_of"] = [self._short(i) for i in inv]
            if out["transitive"]:
                out["note"] = "Transitive: the reasoner follows chains, so query it directly " \
                              "without property paths."
        return {k: v for k, v in out.items() if v not in ([], None)}

    # ------------------------------------------------------------ tool 3
    def find_instances(self, cls: str, text: str = "", limit: int = 20) -> list[dict]:
        """Find the exact IRIs of data instances of a class, optionally matching text.

        Use this before writing a query that mentions a specific thing (a state, a branch,
        a customer), so the query uses the real IRI instead of a guess.
        """
        c = self._iri(cls)
        if c not in self._terms:
            sugg = self._suggest(str(c)[len(self.ns):], self._terms)
            raise ToolError(f"Unknown class {self._short(c)}."
                            + (f" Did you mean {', '.join(sugg)}?" if sugg else ""))
        flt = ""
        if text:
            safe = text.replace("\\", "").replace('"', "").lower()
            flt = (f'FILTER(CONTAINS(LCASE(STR(?x)), "{safe}") || '
                   f'CONTAINS(LCASE(STR(COALESCE(?label, ""))), "{safe}"))')
        q = (f"PREFIX rdfs: <{RDFS}>\nSELECT DISTINCT ?x ?label WHERE {{ ?x a <{c}> . "
             f"OPTIONAL {{ ?x rdfs:label ?label }} {flt} }} ORDER BY ?x LIMIT {int(limit)}")
        _, rows = self.backend.select(q, self.timeout)
        return [{"iri": self._short(r["x"]), **({"label": str(r["label"])} if r.get("label") else {})}
                for r in rows]

    # ------------------------------------------------------------ tool 4
    def validate_sparql(self, query: str) -> dict:
        """Check a SPARQL query before running it: syntax, read-only, and every term exists."""
        errors: list[str] = []
        warnings: list[str] = []
        try:
            form = _query_form(query)
        except Exception as exc:  # noqa: BLE001 - parser errors vary
            try:
                parseUpdate(query)
                return {"valid": False, "errors": ["Updates are not allowed. Only SELECT, ASK, "
                                                   "CONSTRUCT and DESCRIBE queries can be run."]}
            except Exception:  # noqa: BLE001
                pass
            msg = str(exc).splitlines()[0][:300]
            return {"valid": False, "errors": [f"SPARQL syntax error: {msg}"]}
        if form not in READ_FORMS:
            return {"valid": False, "errors": [f"{form} is not allowed; read-only queries only."]}
        try:
            prepared = prepareQuery(query)
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "errors": [f"Query could not be prepared: {str(exc)[:300]}"]}

        # Every IRI in our namespace: schema terms must be declared, others must exist in data.
        iris = {t for t in _iris_in(prepared.algebra) if str(t).startswith(self.ns)}
        predicates = set(_predicates_in(prepared.algebra))
        classes = set(_type_objects_in(prepared.algebra))
        for t in sorted(iris, key=str):
            short = self._short(t)
            if t in self._terms:
                if t in self._deprecated:
                    repl = [self._short(x) for x in self.onto.objects(t, graphs.DCTERMS.isReplacedBy)]
                    warnings.append(f"{short} is deprecated" + (f"; use {', '.join(repl)}" if repl else ""))
                continue
            if t in predicates or t in classes:
                sugg = self._suggest(short, self._terms)
                errors.append(f"Unknown {'property' if t in predicates else 'class'} {short}."
                              + (f" Did you mean {', '.join(sugg)}?" if sugg else ""))
            elif not self.backend.exists(t):
                warnings.append(f"{short} does not appear in the data. Use find_instances to get "
                                "the exact IRI.")
        return {"valid": not errors, "query_type": form.replace("Query", "").upper(),
                **({"errors": errors} if errors else {}),
                **({"warnings": warnings} if warnings else {})}

    # ------------------------------------------------------------ tool 5
    def run_sparql(self, query: str) -> dict:
        """Validate, then run a read-only SPARQL query with reasoning. Returns rows."""
        check = self.validate_sparql(query)
        if not check["valid"]:
            return {"ok": False, **check}
        q = query
        m = _LIMIT.search(q)
        if check["query_type"] == "SELECT":
            if m and int(m.group(1)) > self.max_rows:
                q = _LIMIT.sub(f"LIMIT {self.max_rows}", q, count=1)
            elif not m:
                q = q.rstrip() + f"\nLIMIT {self.max_rows + 1}"
        try:
            cols, rows = self.backend.select(q, self.timeout)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Query failed: {str(exc)[:300]}") from exc
        truncated = len(rows) > self.max_rows
        rows = rows[: self.max_rows]
        out = {"ok": True, "columns": cols, "row_count": len(rows),
               "rows": [{c: self._short(r.get(c)) for c in cols} for r in rows]}
        if truncated:
            out["truncated"] = f"Only the first {self.max_rows} rows are shown. Add filters or LIMIT."
        if not rows:
            out["hint"] = ("No rows. Check instance IRIs with find_instances, and query general "
                           "classes; the reasoner handles subclasses and transitive links.")
        if check.get("warnings"):
            out["warnings"] = check["warnings"]
        return out

    # ------------------------------------------------------------ tool 6
    def get_schema(self) -> str:
        """The whole ontology as LLM-ready Markdown (classes, properties, rules, examples)."""
        from .checks import render_docs
        return render_docs(self.cfg, self.onto)

    # ------------------------------------------------------------ adapters
    SPECS = [
        ToolSpec("search_terms",
                 "Find ontology classes and properties by keyword (matches name, label and "
                 "definition). Use first to map words in the question to ontology terms.",
                 {"text": {"type": "string", "description": "Keyword, e.g. 'mortgage' or 'located'"}},
                 ["text"]),
        ToolSpec("describe_term",
                 "Full details of one class or property: definition, parents, children, "
                 "rules, domain/range, which properties apply. Use before writing a query.",
                 {"term": {"type": "string", "description": "Term like ':Loan' or 'hasBorrower'"}},
                 ["term"]),
        ToolSpec("find_instances",
                 "Look up exact IRIs of data instances of a class, optionally filtered by text. "
                 "Use whenever the question names a specific thing (a state, branch, person).",
                 {"cls": {"type": "string", "description": "Class like ':State'"},
                  "text": {"type": "string", "description": "Optional text to match, e.g. 'illinois'"}},
                 ["cls"]),
        ToolSpec("validate_sparql",
                 "Check a SPARQL query without running it: syntax, read-only, and that every "
                 "term exists (with suggestions for typos).",
                 {"query": {"type": "string", "description": "The SPARQL query"}},
                 ["query"]),
        ToolSpec("run_sparql",
                 "Run a read-only SPARQL query with reasoning applied and return rows. The query "
                 "is validated first; errors come back as text to fix and retry.",
                 {"query": {"type": "string", "description": "The SPARQL query"}},
                 ["query"]),
        ToolSpec("get_schema",
                 "Return the whole ontology as Markdown. Use when you need an overview.",
                 {}, []),
    ]

    def functions(self) -> dict[str, Callable]:
        return {s.name: getattr(self, s.name) for s in self.SPECS}

    def anthropic_tools(self) -> list[dict]:
        """Tool definitions for the Anthropic Messages API `tools` parameter."""
        return [{"name": s.name, "description": s.description,
                 "input_schema": {"type": "object", "properties": s.params, "required": s.required}}
                for s in self.SPECS]

    def openai_tools(self) -> list[dict]:
        """Tool definitions in OpenAI-compatible function-calling format."""
        return [{"type": "function", "function": {
            "name": s.name, "description": s.description,
            "parameters": {"type": "object", "properties": s.params, "required": s.required}}}
            for s in self.SPECS]

    def call(self, name: str, args: dict | None = None) -> str:
        """Run a tool by name and return text for the model. Never raises."""
        fn = self.functions().get(name)
        if fn is None:
            return f"Error: unknown tool '{name}'."
        try:
            result = fn(**(args or {}))
        except ToolError as exc:
            return f"Error: {exc}"
        except TypeError as exc:
            return f"Error: bad arguments for {name}: {exc}"
        except Exception as exc:  # noqa: BLE001 - the model must always get an answer
            return f"Error: {name} failed: {str(exc)[:300]}"
        return result if isinstance(result, str) else json.dumps(result, indent=1, default=str)


# ---------------------------------------------------------------- algebra walkers

def _iris(node, _seen=None):
    """Every IRI anywhere in a parsed query: triples, property paths, VALUES, filters."""
    seen = _seen if _seen is not None else set()
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, URIRef):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _iris(v, seen)
    elif isinstance(node, (list, tuple, set)):
        for v in node:
            yield from _iris(v, seen)
    elif isinstance(node, Path):
        for attr in ("args", "arg", "path"):
            if hasattr(node, attr):
                yield from _iris(getattr(node, attr), seen)


def _triples(node, _seen=None):
    seen = _seen if _seen is not None else set()
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, dict):
        if getattr(node, "name", None) == "BGP":
            yield from node.get("triples", [])
        for v in node.values():
            yield from _triples(v, seen)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _triples(v, seen)


def _iris_in(algebra):
    return set(_iris(algebra))


def _predicates_in(algebra):
    for _, p, _ in _triples(algebra):
        yield from _iris(p)


def _type_objects_in(algebra):
    for _, p, o in _triples(algebra):
        if p == RDF.type and isinstance(o, URIRef):
            yield o
