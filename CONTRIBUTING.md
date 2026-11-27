# Changing the ontology

The full cycle for one change, e.g. "add HELOC as a kind of loan".

## 1. Branch

```bash
git checkout main && git pull
git checkout -b feat/add-heloc
```

## 2. Edit

Use the CLI:

```bash
ontman add-class Heloc --parent Loan \
  --label "HELOC" \
  --comment "Home equity line of credit, secured by the borrower's home."
```

You can also edit `src/ontology.ttl` in Protégé or a text editor. Either way:

- Every term needs `rdfs:label` and a plain-language `rdfs:comment`.
- **Never delete a term.** Run `ontman deprecate OldTerm --replaced-by NewTerm`.
- Put explanations in `rdfs:comment`. `#` comments are removed by normalization.

## 3. Check locally

```bash
ontman check                    # all gates
ontman reason                   # did the inference you expected happen?
ontman diff --base main         # what changed in meaning, and the suggested bump
```

If the change makes a new kind of question possible, add a competency question in `tests/competency/`, plus sample data in `tests/fixtures/` so it has something to answer from.

## 4. Changelog

Add a line under `## [Unreleased]` in `CHANGELOG.md`:

```markdown
## [Unreleased]
### Added
- `Heloc` class for home equity lines of credit.
```

## 5. Commit and open a PR

```bash
git commit -am "Add Heloc class"
git push -u origin feat/add-heloc
```

The pre-commit hook normalizes the file. If it changed anything, `git add` and commit again. CI runs the gates and HermiT, then posts the semantic diff on the PR.

A **domain owner** approves the *meaning*. CI checks the *logic*.

## 6. Release (maintainers)

After merging one or more changes:

```bash
git checkout main && git pull
ontman bump minor               # use the bump CI suggested; major beats minor beats patch
git commit -am "Release v0.2.0"
git tag v0.2.0 && git push --follow-tags
```
