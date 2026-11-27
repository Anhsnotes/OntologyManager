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
merge → ontman bump → git tag vX.Y.Z    → GitHub release with the versioned .ttl
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
tests/violations/*.rq         rules: each query must return 0 rows
tests/competency/*.rq         questions the ontology must be able to answer
tests/test_ontman.py          tests for the toolkit itself
tests/toolkit/sample_project/ frozen copy of the sample project those tests use
ontman/                       the CLI
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

The release workflow checks that the tag matches `owl:versionInfo`, re-runs the gates, and publishes a GitHub release. The release has the versioned `.ttl` attached and the changelog section as its notes.

## Notes

- **Use `rdfs:comment`, not `#` comments.** Normalization rewrites the file, so `#` comments are dropped. `rdfs:comment` also travels with the term into tools and LLM prompts.
- Normalization uses rdflib's `longturtle` writer with a fixed subject order: header, classes, object properties, data properties, alphabetical within each group.
- **Connecting to SQL later (Ontop):** Ontop only uses OWL 2 QL. Add `robot validate-profile --profile QL` to CI, and expect `SecuredLoan` (a defined class) and transitive `locatedIn` to need other handling.
