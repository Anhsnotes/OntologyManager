# Ontology Manager

A small toolkit and repo template for managing an OWL ontology in Turtle (`.ttl`) with a git change cycle: **edit → check → review → release**.

It ships with a sample bank-lending ontology so you can see each step working.

```
edit (ontman / Protégé / by hand)
   │  pre-commit: ontman normalize      → clean, one-line git diffs
   ▼
pull request
   │  CI: ontman check                  → rules, typos, consistency, competency questions
   │  CI: HermiT via ROBOT              → full OWL DL reasoning
   │  CI: ontman diff                   → semantic diff + suggested version bump on the PR
   ▼
merge → ontman bump → git tag vX.Y.Z    → GitHub release with the versioned .ttl + .md
```

## Quick start

```bash
pip install -e ".[dev]"
pre-commit install --hook-type pre-commit --hook-type pre-push

ontman check     # run every quality gate
ontman reason    # see what the ontology infers from the sample data
```

## Repo layout

```
src/ontology.ttl              the ontology (the only file you edit for meaning)
tests/fixtures/*.ttl          small sample data used to test inference
docs/ontology.md              generated LLM-ready Markdown (ontman export-md)
tests/violations/*.rq         rules: each query must return 0 rows
tests/competency/*.rq         questions the ontology must be able to answer
tests/test_ontman.py          tests for the toolkit itself
tests/toolkit/sample_project/ frozen copy of the sample project those tests use
ontman/                       the CLI, agent tools (tools.py) and MCP server (mcp_server.py)
examples/agent.py             a minimal front-end agent that uses the tools
ontman.toml                   project settings (paths, namespace)
CHANGELOG.md                  human-readable history, Keep a Changelog format
```

## Commands

| Command | What it does |
|---|---|
| `ontman normalize [--check]` | Rewrites `.ttl` in canonical form: same triples → same text, so diffs only show real changes |
| `ontman check [--strict]` | Runs every gate (below). Non-zero exit on errors |
| `ontman reason [-o out.ttl]` | Lists facts inferred from the fixtures that nobody wrote down |
| `ontman diff --base origin/main` | Semantic diff vs a git ref, with suggested semver bump |
| `ontman add-class Heloc --parent Loan --label "HELOC" --comment "..."` | Adds a class safely (parent must exist, name must be new) |
| `ontman add-property hasCoBorrower --domain Loan --range Person --label ... --comment ...` | Adds an object or data property (`--kind data --range decimal`) |
| `ontman deprecate AutoLoan --replaced-by Loan --reason "..."` | Deprecates instead of deleting, so existing queries keep working |
| `ontman export-md [--check]` | Generates `docs/ontology.md`, the LLM-ready version of the ontology (see below) |
| `ontman tool run_sparql '{"query":"..."}'` | Calls one agent tool from the shell and shows exactly what the model would see |
| `ontman serve [--transport streamable-http]` | Runs the agent tools as an MCP server |
| `ontman bump minor` | Bumps `owl:versionInfo` and moves Unreleased changelog notes under the new version |

You can still edit `src/ontology.ttl` in Protégé or a text editor. The pre-commit hook normalizes the file either way.

## What `ontman check` verifies

| Gate | Catches |
|---|---|
| parse | Turtle syntax errors |
| format | File not normalized |
| violation:* | Your SPARQL rules: missing labels/definitions, duplicate labels, naming conventions |
| undefined-terms | Typos like `:Morgage`: terms in our namespace that are used but never declared |
| deprecations | Deprecated terms with no replacement, or still used in fixtures (warning) |
| consistency | OWL 2 RL contradictions in ontology + fixtures, e.g. something both a Person and an Organization |
| competency:* | Questions the ontology must answer, run *after reasoning* |

CI also runs **HermiT** (a full OWL DL reasoner) through [ROBOT](http://robot.obolibrary.org/). It catches problems OWL RL can't see, such as unsatisfiable classes.

### Adding a rule

Drop a `.rq` file into `tests/violations/`. Any row it returns is reported:

```sparql
# description: Every Loan subclass needs a definition starting with "A loan"
# severity: warning
SELECT ?c WHERE { ... }
```

### Adding a competency question

Drop a `.rq` file into `tests/competency/`, and state how many rows it must return after reasoning:

```sparql
# question: Which loans were originated in the Midwest region?
# expect: 2          (also: >=1, <5, ==0)
PREFIX : <http://example.org/bank#>
SELECT ?loan WHERE { ?loan a :Loan ; :originatedAt/:locatedIn :Midwest }
```

The sample questions only pass because of inference: transitive `locatedIn`, the defined class `SecuredLoan`, and the inverse property `borrowerOf`. If someone breaks one of those, CI fails.

## Markdown for LLMs (`docs/ontology.md`)

`ontman export-md` turns the ontology into a compact Markdown file you can paste into an LLM prompt, or retrieve from for RAG. The `.ttl` file stays the source of truth: the Markdown is generated from it and never edited by hand.

It contains:
- **Prefixes and usage rules**: use only listed terms, avoid deprecated ones, and let the reasoner do inference
- **Class hierarchy** with each class's plain-language definition (`rdfs:comment`)
- **Class rules** in readable OWL, e.g. `:Loan` **and** (`:hasCollateral` **some** `:Collateral`), plus disjointness
- **Property tables**: domain → range, transitive/inverse, meaning
- **What the reasoner infers**, so the model writes simple SPARQL and doesn't try to reason itself
- **Deprecated terms** and their replacements
- **Worked examples**: every competency question with its SPARQL, for few-shot prompting

It stays in sync automatically:
- `add-class`, `add-property`, `deprecate` and `bump` regenerate it.
- The pre-commit hook regenerates it when the ontology or competency questions change.
- `ontman check` (and so CI) fails if the committed file is stale.
- Each GitHub release attaches `ontology-vX.Y.Z.md` next to the `.ttl`.

Use `ontman export-md -o prompt.md --no-examples` for a shorter version.

Typical text-to-SPARQL prompt:
```
<system>You write SPARQL for the ontology below. Reply with one query only.</system>
{contents of docs/ontology.md}
Question: Which secured loans were originated in Illinois?
```

## Agent tools (`ontman.tools`)

A front-end agent answers questions by exploring the ontology and running SPARQL. These six tools give it a safe way to do that:

| Tool | What the agent uses it for |
|---|---|
| `search_terms(text)` | Map words in the question to ontology terms (typos get "did you mean") |
| `describe_term(term)` | Definition, parents and children, rules, domain/range, which properties apply |
| `find_instances(cls, text)` | Exact IRIs for named things (`:Illinois`, not a guess like `:IL`) |
| `validate_sparql(query)` | Syntax check, read-only check, and every term must exist |
| `run_sparql(query)` | Validates, then runs with reasoning, capped rows and a timeout |
| `get_schema()` | The whole ontology as Markdown |

The guardrails apply however the tools are called: queries are read-only (no UPDATE), unknown terms are rejected with suggestions, guessed instance names trigger a warning, results are capped at 200 rows with a 30-second timeout, and errors come back as plain text the model can fix and retry.

**Where the data comes from**
- By default: the ontology plus `tests/fixtures/`, with OWL RL reasoning. You can build and test an agent before any database is connected.
- Set `endpoint = "http://.../sparql"` in `ontman.toml`, or pass `--endpoint`, to query a live endpoint such as Ontop over SQL.

### Three ways to use the tools

**1. In your own agent (no MCP).** Import the tools and give their definitions to the model:
```python
from ontman.tools import OntologyTools
tools = OntologyTools.from_config()
resp = client.messages.create(model=..., tools=tools.anthropic_tools(), messages=...)
# for each tool_use block:
output = tools.call(block.name, block.input)   # never raises; errors come back as text
```
`examples/agent.py` is a complete agent loop: `pip install -e ".[agent]"`, then `python examples/agent.py "How much do we have in secured loans in Illinois?"`. For OpenAI-compatible frameworks, use `tools.openai_tools()`.

**2. Over MCP, for Claude Desktop, Claude Code, claude.ai or other agents.** Install with `pip install -e ".[mcp]"`, then:
```bash
ontman serve                                   # stdio
claude mcp add ontology -- ontman serve        # register it with Claude Code

ontman serve --transport streamable-http       # shared: http://127.0.0.1:8000/mcp
```
The server exposes the same six tools, marked read-only, plus an `ontology://schema` resource.

**3. From the shell, for debugging.**
```bash
ontman tool search_terms '{"text": "secured"}'
ontman tool run_sparql '{"query": "PREFIX : <http://example.org/bank#> SELECT ?l WHERE { ?l a :SecureLoan }"}'
# → Error: Unknown class :SecureLoan. Did you mean :SecuredLoan?
```

## Versioning rules (`ontman diff`)

| Bump | When |
|---|---|
| **major** | A term was removed, or a logical axiom (subClassOf, domain, range, equivalence, …) was removed or changed |
| **minor** | Terms or axioms were added, or a term was deprecated |
| **patch** | Only labels, comments or other annotations changed |

## Release

```bash
ontman bump minor
git commit -am "Release v0.2.0"
git tag v0.2.0 && git push --follow-tags
```

The release workflow checks that the tag matches `owl:versionInfo`, re-runs the gates, and publishes a GitHub release. The release has the versioned `.ttl` and `.md` attached and the changelog section as its notes.

## Notes

- **Use `rdfs:comment`, not `#` comments.** Normalization rewrites the file, so `#` comments are dropped. `rdfs:comment` also travels with the term into tools and LLM prompts.
- Normalization uses rdflib's `longturtle` writer with a fixed subject order: header, classes, object properties, data properties, alphabetical within each group.
- **Connecting to SQL later (Ontop):** Ontop only uses OWL 2 QL. Add `robot validate-profile --profile QL` to CI, and expect `SecuredLoan` (a defined class) and transitive `locatedIn` to need other handling.
