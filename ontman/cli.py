"""ontman: command-line interface for the ontology change cycle."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, checks, edit, graphs
from .config import load_config
from .diff import diff, to_markdown, to_text, version_was_bumped
from .reasoning import new_facts, reason

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = RED = YELLOW = DIM = RESET = ""


def _ns(cfg, g):
    return graphs.base_namespace(g, cfg.namespace)


# ---------------------------------------------------------------- commands

def cmd_normalize(args, cfg) -> int:
    files = [Path(f) for f in args.files] or [cfg.ontology]
    bad = 0
    for f in files:
        if args.check:
            if graphs.is_normalized(f):
                print(f"{GREEN}ok{RESET}   {f}")
            else:
                print(f"{RED}FAIL{RESET} {f} is not normalized (run `ontman normalize`)")
                bad += 1
        else:
            before = f.read_text()
            graphs.save(graphs.load(f), f)
            changed = before != f.read_text()
            print(f"{'normalized' if changed else 'unchanged '} {f}")
    return 1 if bad else 0


def cmd_check(args, cfg) -> int:
    report = checks.run_all(cfg, skip_format=args.skip_format)
    for name in report.passed:
        print(f"{GREEN}✓{RESET} {name}")
    for f in report.warnings:
        print(f"{YELLOW}!{RESET} {f.check}: {f.message}")
    for f in report.errors:
        print(f"{RED}✗{RESET} {f.check}: {f.message}")
    print()
    summary = f"{len(report.passed)} passed, {len(report.warnings)} warnings, {len(report.errors)} errors"
    print(f"{RED if report.errors else GREEN}{summary}{RESET}")
    if args.strict and report.warnings:
        return 1
    return 1 if report.errors else 0


def cmd_reason(args, cfg) -> int:
    onto = graphs.load(cfg.ontology)
    ns = _ns(cfg, onto)
    data = [graphs.load(f) for f in (args.data or cfg.fixtures)]
    asserted = graphs.load(cfg.ontology)
    for d in data:
        asserted += d
    result = reason(onto, *data)
    if result.errors:
        print(f"{RED}Inconsistent:{RESET}")
        for e in result.errors:
            print(f"  ✗ {e}")
    facts = new_facts(asserted, result.closure, ns)
    print(f"{len(facts)} facts inferred that nobody wrote down:\n")
    for s, p, o in facts:
        print(f"  {graphs.local_name(s, ns):<20} {DIM}{p.n3(onto.namespace_manager)}{RESET}  "
              f"{graphs.local_name(o, ns)}")
    if args.output:
        out = graphs.Graph()
        for prefix, uri in onto.namespaces():
            out.bind(prefix, uri)
        for t in facts:
            out.add(t)
        graphs.save(out, args.output)
        print(f"\nWrote {args.output}")
    return 1 if result.errors else 0


def cmd_diff(args, cfg) -> int:
    if args.base:
        old = graphs.load_from_git(args.base, cfg.ontology, cfg.root)
        new = graphs.load(args.new or cfg.ontology)
    else:
        if not args.old:
            print("Give OLD [NEW] files, or --base <git-ref>.", file=sys.stderr)
            return 2
        old = graphs.load(args.old)
        new = graphs.load(args.new or cfg.ontology)
    res = diff(old, new, _ns(cfg, new))
    md = to_markdown(res)
    print(md if args.format == "markdown" else to_text(res))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(md)
    if args.require_bump and not res.is_empty and not version_was_bumped(res):
        print(f"\n{RED}owl:versionInfo must be bumped ({res.bump}) for this change.{RESET}", file=sys.stderr)
        return 1
    return 0


def _refresh_docs(cfg) -> None:
    """Keep the generated Markdown in step with every scripted change."""
    if cfg.docs_md:
        text = checks.render_docs(cfg, graphs.load(cfg.ontology))
        if not cfg.docs_md.exists() or cfg.docs_md.read_text() != text:
            cfg.docs_md.parent.mkdir(parents=True, exist_ok=True)
            cfg.docs_md.write_text(text)
            print(f"{GREEN}✓{RESET} regenerated {cfg.docs_md.name}")


def _edit(cfg, fn) -> int:
    g = graphs.load(cfg.ontology)
    try:
        msg = fn(g, _ns(cfg, g))
    except edit.EditError as exc:
        print(f"{RED}error:{RESET} {exc}", file=sys.stderr)
        return 1
    graphs.save(g, cfg.ontology)
    print(f"{GREEN}✓{RESET} {msg}")
    _refresh_docs(cfg)
    print(f"{DIM}Next: ontman check && git diff{RESET}")
    return 0


def cmd_add_class(args, cfg) -> int:
    return _edit(cfg, lambda g, ns: (
        edit.add_class(g, ns, args.name, args.label, args.comment, args.parent, args.disjoint_with),
        f"added class :{args.name}")[1])


def cmd_add_property(args, cfg) -> int:
    return _edit(cfg, lambda g, ns: (
        edit.add_property(g, ns, args.name, args.label, args.comment, args.kind, args.domain,
                          args.range, args.transitive, args.inverse_of),
        f"added {args.kind} property :{args.name}")[1])


def cmd_deprecate(args, cfg) -> int:
    return _edit(cfg, lambda g, ns: (
        edit.deprecate(g, ns, args.name, args.replaced_by, args.reason),
        f"deprecated :{args.name}" + (f" (replaced by :{args.replaced_by})" if args.replaced_by else ""))[1])


def cmd_export_md(args, cfg) -> int:
    out = Path(args.output) if args.output else cfg.docs_md
    if out is None:
        print("No output path: pass -o FILE or set docs_md in ontman.toml.", file=sys.stderr)
        return 2
    onto = graphs.load(cfg.ontology)
    if args.no_examples:
        from .export import export_markdown
        src = cfg.ontology.resolve().relative_to(cfg.root.resolve()).as_posix()
        text = export_markdown(onto, _ns(cfg, onto), None, source_name=src, examples=False)
    else:
        text = checks.render_docs(cfg, onto)
    if args.check:
        if out.exists() and out.read_text() == text:
            print(f"{GREEN}ok{RESET}   {out} is up to date")
            return 0
        print(f"{RED}FAIL{RESET} {out} is out of date (run `ontman export-md`)")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    changed = not out.exists() or out.read_text() != text
    out.write_text(text)
    print(f"{'wrote    ' if changed else 'unchanged'} {out} ({len(text.split())} words)")
    return 0


def cmd_serve(args, cfg) -> int:
    from .mcp_server import serve
    serve(args.transport, args.host, args.port, args.endpoint)
    return 0


def cmd_tool(args, cfg) -> int:
    """Call one agent tool from the shell, e.g. to see what the model will see."""
    import json as _json

    from .tools import OntologyTools
    tools = OntologyTools.from_config(endpoint=args.endpoint)
    params = _json.loads(args.args) if args.args else {}
    print(tools.call(args.name, params))
    return 0


def cmd_bump(args, cfg) -> int:
    g = graphs.load(cfg.ontology)
    try:
        old, new = edit.bump(g, args.part)
    except edit.EditError as exc:
        print(f"{RED}error:{RESET} {exc}", file=sys.stderr)
        return 1
    graphs.save(g, cfg.ontology)
    print(f"{GREEN}✓{RESET} version {old} → {new}")
    _refresh_docs(cfg)
    if cfg.changelog and edit.release_changelog(cfg.changelog, new):
        print(f"{GREEN}✓{RESET} {cfg.changelog.name}: Unreleased notes moved under [{new}]")
    print(f"{DIM}Next: git commit -am 'Release v{new}' && git tag v{new}{RESET}")
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ontman", description="Ontology change-cycle toolkit.")
    p.add_argument("--version", action="version", version=f"ontman {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("normalize", help="rewrite .ttl files in canonical form (clean git diffs)")
    s.add_argument("files", nargs="*", help="files to normalize (default: the ontology)")
    s.add_argument("--check", action="store_true", help="only report, do not rewrite")
    s.set_defaults(func=cmd_normalize)

    s = sub.add_parser("check", help="run every quality gate")
    s.add_argument("--strict", action="store_true", help="treat warnings as errors")
    s.add_argument("--skip-format", action="store_true", help="skip the normalization check")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("reason", help="show what the ontology infers from the sample data")
    s.add_argument("--data", nargs="*", type=Path, help="data files (default: fixtures)")
    s.add_argument("-o", "--output", type=Path, help="write inferred triples to a .ttl file")
    s.set_defaults(func=cmd_reason)

    s = sub.add_parser("diff", help="semantic diff + suggested version bump")
    s.add_argument("old", nargs="?", type=Path)
    s.add_argument("new", nargs="?", type=Path)
    s.add_argument("--base", help="compare against the ontology at this git ref, e.g. origin/main")
    s.add_argument("--format", choices=["text", "markdown"], default="text")
    s.add_argument("--require-bump", action="store_true",
                   help="fail if meaning changed but owl:versionInfo did not")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("add-class", help="add a new class")
    s.add_argument("name")
    s.add_argument("--label", required=True)
    s.add_argument("--comment", required=True, help="definition in plain language")
    s.add_argument("--parent")
    s.add_argument("--disjoint-with", nargs="*", default=[])
    s.set_defaults(func=cmd_add_class)

    s = sub.add_parser("add-property", help="add a new object or data property")
    s.add_argument("name")
    s.add_argument("--kind", choices=["object", "data"], default="object")
    s.add_argument("--label", required=True)
    s.add_argument("--comment", required=True)
    s.add_argument("--domain")
    s.add_argument("--range", help="class name, or a datatype for data properties: "
                                   + ", ".join(edit.DATATYPES))
    s.add_argument("--transitive", action="store_true")
    s.add_argument("--inverse-of")
    s.set_defaults(func=cmd_add_property)

    s = sub.add_parser("deprecate", help="deprecate a term instead of deleting it")
    s.add_argument("name")
    s.add_argument("--replaced-by")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_deprecate)

    s = sub.add_parser("export-md", help="generate LLM-ready Markdown from the ontology")
    s.add_argument("-o", "--output", help="output file (default: docs_md in ontman.toml)")
    s.add_argument("--check", action="store_true", help="only report whether the file is up to date")
    s.add_argument("--no-examples", action="store_true", help="leave out the competency SPARQL examples")
    s.set_defaults(func=cmd_export_md)

    s = sub.add_parser("serve", help="run the agent tools as an MCP server")
    s.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--endpoint", help="SPARQL endpoint for data (default: local fixtures)")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("tool", help="call one agent tool, e.g. ontman tool search_terms '{\"text\":\"loan\"}'")
    s.add_argument("name", help="search_terms, describe_term, find_instances, validate_sparql, "
                                "run_sparql, get_schema")
    s.add_argument("args", nargs="?", help="JSON object of arguments")
    s.add_argument("--endpoint", help="SPARQL endpoint for data (default: local fixtures)")
    s.set_defaults(func=cmd_tool)

    s = sub.add_parser("bump", help="bump owl:versionInfo and roll the changelog")
    s.add_argument("part", choices=["major", "minor", "patch"])
    s.set_defaults(func=cmd_bump)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
