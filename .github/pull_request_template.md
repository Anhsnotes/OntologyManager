## What changes in the ontology?
<!-- e.g. "Add HELOC as a kind of Loan" -->

## Why?
<!-- Business need, or the competency question this supports -->

## Checklist
- [ ] Every new term has an `rdfs:label` and a plain-language `rdfs:comment`
- [ ] Nothing was deleted: removed terms are deprecated with `--replaced-by`
- [ ] Added a competency question in `tests/competency/` if this enables a new kind of question
- [ ] Added a line under `## [Unreleased]` in `CHANGELOG.md`
- [ ] Domain owner reviewed the meaning (CI reviews the logic)

The bot comment below shows the semantic diff and the suggested version bump.
