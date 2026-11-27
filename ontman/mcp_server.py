"""MCP server exposing the same tools as ontman.tools to any MCP client.

    ontman serve                                  # stdio, for Claude Desktop / Claude Code
    ontman serve --transport streamable-http      # http://127.0.0.1:8000/mcp, for shared use
    ontman serve --endpoint http://ontop:8080/sparql   # query live data instead of fixtures

Requires the optional dependency:  pip install -e ".[mcp]"
"""

from __future__ import annotations

from .tools import OntologyTools

INSTRUCTIONS = """\
Tools for answering questions with a reasoning-enabled ontology.
Workflow: search_terms to map words to terms -> describe_term for details ->
find_instances for exact IRIs of named things -> write SPARQL -> run_sparql
(it validates first; fix any error and retry). The query engine applies reasoning:
query general classes and transitive properties directly. Read-only.
"""


def build_server(tools: OntologyTools):
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise SystemExit('The MCP SDK is not installed. Run: pip install -e ".[mcp]"') from exc

    server = MCPServer("ontology", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    # Thin typed wrappers around tools.call: MCP clients get exactly the same validation,
    # limits and error text as agents that import ontman.tools directly.
    def search_terms(text: str) -> str:
        return tools.call("search_terms", {"text": text})

    def describe_term(term: str) -> str:
        return tools.call("describe_term", {"term": term})

    def find_instances(cls: str, text: str = "") -> str:
        return tools.call("find_instances", {"cls": cls, "text": text})

    def validate_sparql(query: str) -> str:
        return tools.call("validate_sparql", {"query": query})

    def run_sparql(query: str) -> str:
        return tools.call("run_sparql", {"query": query})

    def get_schema() -> str:
        return tools.call("get_schema", {})

    wrappers = {f.__name__: f for f in (search_terms, describe_term, find_instances,
                                        validate_sparql, run_sparql, get_schema)}
    for spec in tools.SPECS:
        server.add_tool(wrappers[spec.name], name=spec.name, description=spec.description,
                        annotations=read_only)

    @server.resource("ontology://schema", name="schema", mime_type="text/markdown",
                     description="The whole ontology as LLM-ready Markdown")
    def schema() -> str:
        return tools.get_schema()

    return server


def serve(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8000,
          endpoint: str | None = None) -> None:
    tools = OntologyTools.from_config(endpoint=endpoint)
    server = build_server(tools)
    if transport == "streamable-http":
        server.run("streamable-http", host=host, port=port)
    else:
        server.run("stdio")
