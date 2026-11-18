"""The fuzzer's entry point.

    python -m tools.fuzz --dsn "postgres://localhost/pg_mimic_fuzz" --count 5000

Three phases, because shrinking is expensive and most failures are duplicates:

1. *Sweep.* Generate `count` queries, run each against Postgres and each target,
   and record a cheap pre-bucket key per failure -- the normalised message for a
   refusal, or the severity plus the set of SQL constructs in the query for a
   wrong answer. Queries Postgres itself rejects are discarded.
2. *Shrink.* One representative per pre-bucket, reduced until it stops getting
   smaller. This is where the budget goes: a few hundred evaluations per bucket
   rather than per failure.
3. *Report.* Re-bucket by the minimised SQL, which merges pre-buckets that turned
   out to be the same bug, and print each with the count of raw failures behind
   it and whether it also reproduces in raw sqlglot.

Exit status is 1 when anything was found, so this can gate a nightly job.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time

import sqlglot
from sqlglot import exp

from . import compare, engines, generate
from .engines import Failed


class Finding:
    def __init__(self, kind: str, detail: str, sql: str, engine: str):
        self.kind = kind
        self.detail = detail
        self.sql = sql
        self.engine = engine
        self.count = 1
        self.minimised = sql
        self.also_raw: bool | None = None

    @property
    def verdict(self) -> tuple[str, str]:
        return (self.kind, engines.signature(self.detail) if self.kind == "REFUSED" else "")


def canonical_aliases(sql: str) -> str:
    """The same query with generated table aliases renumbered from one.

    Purely so identical bugs de-duplicate. Two seeds that both minimise to
    `SELECT AVG(x.n) FROM t AS x` differ only in whether the generator had reached
    a298 or a346 by then, and without this they are reported as two findings.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:
        return sql
    mapping: dict[str, str] = {}
    for node in tree.walk():
        if isinstance(node, exp.Identifier) and re.fullmatch(r"a\d+", node.name):
            mapping.setdefault(node.name, f"x{len(mapping) + 1}")
            node.set("this", mapping[node.name])
    try:
        return tree.sql(dialect="postgres")
    except Exception:
        return sql


def _constructs(sql: str) -> str:
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:
        return "unparsed"
    names = {type(node).__name__ for node in tree.walk() if not isinstance(node, (exp.Identifier, exp.Literal, exp.Column))}
    return ",".join(sorted(names))


def covers_every_column(sql: str, width: int) -> bool:
    """Whether `sql` ends in an ORDER BY over ordinals 1..width, so that comparing
    Postgres' row *sequence* against the executor's is legitimate.

    Two rows that tie on every output column are equal tuples, so any tie-break
    gives the same sequence -- but only if the sort keys cover every column. The
    generator emits exactly that, and the shrinker is supposed to preserve it;
    checking here rather than trusting either of them is what keeps a dropped sort
    key from being reported as a wrong row order.
    """
    if width == 0:
        return True
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:
        return False
    order = tree.args.get("order")
    if order is None:
        return False
    ordinals = set()
    for item in order.expressions:
        key = item.this
        if not isinstance(key, exp.Literal) or key.is_string:
            return False
        ordinals.add(int(key.name))
    return ordinals == set(range(1, width + 1))


def _evaluate(sql: str, oracle: engines.Postgres, target, ordered: bool) -> tuple[str, str] | None:
    """None if the target agrees with Postgres, else (kind, detail).

    A query Postgres refuses raises Rejected, which the caller counts as a
    discarded sample rather than a finding -- it means the generator or the
    shrinker built something invalid, and only Postgres gets to say so.
    """
    try:
        expected = oracle.run(sql)
    except Failed as exc:
        raise Rejected(exc.message) from None
    try:
        actual = target.run(sql)
    except Failed as exc:
        return ("REFUSED", exc.message)
    total_order = ordered and covers_every_column(sql, len(expected[0]) if expected else 0)
    difference = compare.compare(expected, actual, ordered=total_order)
    return None if difference is None else difference


class Rejected(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.fuzz", description=__doc__)
    parser.add_argument("--dsn", default="postgres://localhost/pg_mimic_fuzz", help="a Postgres to use as the oracle")
    parser.add_argument("--count", type=int, default=2000, help="queries to generate")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--target", default="mimic", choices=["mimic", "raw"], help="which side of tables.py to fuzz")
    parser.add_argument("--ordered", action="store_true", help="add a total ORDER BY and compare row order and LIMIT too")
    parser.add_argument("--without", default="", help=f"comma-separated features to disable, from: {','.join(generate.FEATURES)}")
    parser.add_argument("--only", default="", help="comma-separated severities to report, e.g. VALUE,COUNT,REFUSED")
    parser.add_argument("--depth", type=int, default=3, help="maximum expression nesting")
    parser.add_argument("--shrink-budget", type=int, default=600, help="evaluations per finding")
    parser.add_argument("--no-shrink", action="store_true")
    parser.add_argument("--json", dest="json_path", default=None, help="also write the findings here")
    arguments = parser.parse_args(argv)

    without = {feature for feature in arguments.without.split(",") if feature}
    unknown = without - set(generate.FEATURES)
    if unknown:
        parser.error(f"unknown feature(s) {sorted(unknown)}")
    wanted = {severity for severity in arguments.only.split(",") if severity}

    oracle = engines.Postgres(arguments.dsn)
    oracle.load()
    target = engines.build([arguments.target])[0]

    generator = generate.Generator(seed=arguments.seed, without=without, ordered=arguments.ordered, max_depth=arguments.depth)

    buckets: dict[tuple, Finding] = {}
    rejected: collections.Counter = collections.Counter()
    ran = 0
    started = time.time()

    for index in range(arguments.count):
        sql = generator.select()
        try:
            difference = _evaluate(sql, oracle, target, arguments.ordered)
        except Rejected as exc:
            rejected[engines.signature(str(exc))] += 1
            continue
        ran += 1
        if difference is None:
            continue
        kind, detail = difference
        if wanted and kind not in wanted:
            continue
        key = (kind, engines.signature(detail) if kind == "REFUSED" else _constructs(sql))
        if key in buckets:
            buckets[key].count += 1
        else:
            buckets[key] = Finding(kind, detail, sql, arguments.target)
        if (index + 1) % 250 == 0:
            print(
                f"  {index + 1}/{arguments.count} generated, {ran} valid, {sum(f.count for f in buckets.values())} failures,"
                f" {len(buckets)} buckets",
                file=sys.stderr,
            )

    print(
        f"\nswept {arguments.count} queries in {time.time() - started:.1f}s: {ran} ran on postgres,"
        f" {arguments.count - ran} rejected as invalid",
        file=sys.stderr,
    )

    findings = list(buckets.values())
    raw = target if arguments.target == "raw" else engines.RawSqlglot()
    if findings and not arguments.no_shrink:
        print(f"shrinking {len(findings)} findings...", file=sys.stderr)
    for finding in findings:

        def check(candidate: str, finding: Finding = finding) -> tuple[str, str] | None:
            try:
                outcome = _evaluate(candidate, oracle, target, arguments.ordered)
            except Rejected:
                return None
            if outcome is None:
                return None
            kind, detail = outcome
            return (kind, engines.signature(detail) if kind == "REFUSED" else "")

        if not arguments.no_shrink:
            minimised, spent = shrink_finding(finding, check, arguments)
            finding.minimised = canonical_aliases(minimised)
            # The detail describes which row and column diverged and what the two
            # values were, and after shrinking it describes a query that is no
            # longer being shown. Re-running the minimised query is what keeps the
            # report's two halves talking about the same thing.
            try:
                refreshed = _evaluate(finding.minimised, oracle, target, arguments.ordered)
            except Rejected:
                refreshed = None
            if refreshed is not None and refreshed[0] == finding.kind:
                finding.detail = refreshed[1]
            else:
                finding.minimised = finding.sql
            print(f"  {spent:4d} evaluations: {finding.minimised}", file=sys.stderr)
        # Whether the same query also diverges without pg_mimic's rewrites in the
        # way, which is what says who owns the bug. Only meaningful when the
        # target is `mimic`; fuzzing raw sqlglot answers it by construction.
        if arguments.target == "raw":
            finding.also_raw = True
        else:
            try:
                finding.also_raw = _evaluate(finding.minimised, oracle, raw, arguments.ordered) is not None
            except Rejected:
                finding.also_raw = None

    merged: dict[tuple, Finding] = {}
    for finding in findings:
        key = (finding.kind, finding.minimised)
        if key in merged:
            merged[key].count += finding.count
        else:
            merged[key] = finding
    report(sorted(merged.values(), key=lambda f: (compare.SEVERITY.index(f.kind) if f.kind in compare.SEVERITY else -1, -f.count)))

    if rejected:
        print("\nmost common reasons postgres rejected a generated query (generator quality, not findings):")
        for message, count in rejected.most_common(5):
            print(f"  {count:5d}  {message}")

    if arguments.json_path:
        with open(arguments.json_path, "w") as handle:
            json.dump(
                [
                    {
                        "kind": finding.kind,
                        "sql": finding.minimised,
                        "original_sql": finding.sql,
                        "detail": finding.detail,
                        "occurrences": finding.count,
                        "also_fails_raw_sqlglot": finding.also_raw,
                    }
                    for finding in merged.values()
                ],
                handle,
                indent=2,
            )
    return 1 if merged else 0


def shrink_finding(finding: Finding, check, arguments) -> tuple[str, int]:
    from .shrink import shrink

    return shrink(finding.sql, finding.verdict, check, keep_order=arguments.ordered, budget=arguments.shrink_budget)


def report(findings: list[Finding]) -> None:
    if not findings:
        print("\nno divergences found.")
        return
    print(f"\n{len(findings)} distinct divergence(s), worst first:\n")
    for number, finding in enumerate(findings, start=1):
        origin = {True: "also fails raw sqlglot", False: "raw sqlglot gets this right", None: "not reproducible standalone"}[
            finding.also_raw
        ]
        print(f"{number}. {finding.kind}  ({finding.count} occurrence(s), {origin})")
        print(f"   {finding.minimised}")
        print(f"   {finding.detail}\n")


if __name__ == "__main__":
    sys.exit(main())
