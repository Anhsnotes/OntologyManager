"""Minimal front-end agent: answers questions with the ontology tools, no MCP needed.

    pip install -e ".[agent]"
    export ANTHROPIC_API_KEY=...
    python examples/agent.py "How much do we have in secured loans in Illinois?"

The same tools are available over MCP with `ontman serve`; this file shows the
in-process route (Option 2): import the tools, hand their definitions to the model,
and run every tool call it makes through tools.call().
"""

from __future__ import annotations

import sys

from ontman.tools import OntologyTools

MODEL = "claude-sonnet-5-5"
MAX_TURNS = 12

SYSTEM = """You answer business questions using a reasoning-enabled knowledge graph.

Workflow:
1. search_terms to map the user's words to ontology terms; describe_term for details.
2. find_instances for the exact IRI of any specific thing the question names.
3. Write SPARQL and call run_sparql. It validates first. If it returns an error,
   fix the query and retry.
4. Answer in plain language with the numbers, then show the SPARQL you used.

Rules:
- Use only terms that exist in the ontology. Never guess an IRI.
- The engine applies reasoning: query general classes (e.g. :Loan, :SecuredLoan)
  and transitive properties (e.g. :locatedIn) directly. Do not enumerate subclasses
  or chain locations by hand.
- If the data cannot answer the question, say so.

Ontology usage rules and prefixes:
{schema_head}
"""


def build_system(tools: OntologyTools) -> str:
    schema = tools.get_schema()
    # Keep the prompt small: prefixes + usage rules only. The agent pulls details on demand.
    head = schema.split("## Classes")[0]
    return SYSTEM.format(schema_head=head)


def ask(question: str, tools: OntologyTools | None = None, client=None, verbose: bool = True) -> str:
    tools = tools or OntologyTools.from_config()
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    messages = [{"role": "user", "content": question}]
    system = build_system(tools)
    for _ in range(MAX_TURNS):
        resp = client.messages.create(model=MODEL, max_tokens=2000, system=system,
                                      tools=tools.anthropic_tools(), messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                if verbose:
                    print(f"→ {block.name}({block.input})", file=sys.stderr)
                output = tools.call(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output,
                                "is_error": output.startswith("Error:")})
        messages.append({"role": "user", "content": results})
    return "Stopped: too many tool calls without an answer."


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "How much do we have in secured loans in Illinois?"
    print(ask(q))
