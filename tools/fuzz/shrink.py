"""Reducing a failing query to the smallest one that still fails the same way.

Without this the tool is useless. A run of five thousand queries produces
hundreds of failures which are perhaps a dozen distinct bugs, each buried under
forty lines of generated arithmetic. Shrinking turns
``SELECT (ABS(a1.n) * CASE WHEN ... END) FROM t AS a1 LEFT JOIN u ...`` into
``SELECT a1.s || 'x' FROM t AS a1`` -- which is a bug report, and small enough
that identical bugs from unrelated seeds collapse onto the same text and
de-duplicate themselves.

The reduction moves are deliberately reckless: hoist any node to its own child,
replace any node with a literal, drop any clause. Most of what that proposes is
invalid SQL, and that is fine, because *Postgres is the validity filter* -- a
variant it refuses is simply rejected, exactly like a variant that stops
reproducing. Being allowed to propose nonsense is what keeps the move set to
thirty lines instead of a typed rewriter as large as the generator.

A variant is accepted when it is smaller and fails the same way, where "the same
way" is the severity for a wrong answer and the normalised message for a refusal.
Requiring the *same* failure rather than merely *a* failure is what stops the
shrinker from sliding off a subtle wrong-answer bug onto some trivial unsupported
function it happens to pass through.
"""

from __future__ import annotations

from typing import Callable

import sqlglot
from sqlglot import exp

_LITERALS = ["1", "'x'", "TRUE", "NULL"]
_DROPPABLE = ["where", "having", "group", "limit", "offset", "distinct", "qualify", "with", "joins", "order"]

# Arg positions that name something rather than compute something. Postgres will
# happily accept `SELECT btrim('a%b') TRUE` -- TRUE is a legal bare column alias --
# so it is no use as a filter here, and a shrinker left to rewrite aliases and
# type names spends its budget renaming columns instead of deleting them.
_STRUCTURAL = {"alias", "alias_column_names", "table", "db", "catalog", "to", "kind", "unit", "format", "collate"}

# Nodes that hold the query together rather than compute a value, and so must not
# be replaced by a literal. Postgres is the validity filter for most reckless
# moves, but not for these: replacing the FROM clause of `SELECT avg(a.n) FROM t
# AS a` with TRUE renders `SELECT AVG(a.n)TRUE`, which Postgres accepts -- as a
# FROM-less select with a column aliased "true". Valid, reproducing, and about a
# different query than the one being shrunk. Nothing is lost by refusing them:
# whole clauses are already removable by arg key through _DROPPABLE.
_SCAFFOLDING = (
    exp.Identifier,
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.Star,
    exp.DataType,
    exp.From,
    exp.Join,
    exp.Table,
    exp.TableAlias,
    exp.Group,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    exp.Offset,
    exp.Where,
    exp.Having,
    exp.Distinct,
    exp.With,
    exp.CTE,
    exp.Subquery,
    exp.Lateral,
)

Verdict = tuple[str, str]
Check = Callable[[str], Verdict | None]


def _variants(expression: exp.Expression, keep_order: bool) -> list[exp.Expression]:
    candidates: list[exp.Expression] = []

    droppable = [key for key in _DROPPABLE if not (keep_order and key == "order")]
    for key in droppable:
        if expression.args.get(key):
            variant = expression.copy()
            variant.set(key, None)
            candidates.append(variant)

    selects = expression.args.get("expressions") or []
    if isinstance(expression, exp.Select) and len(selects) > 1:
        for index in range(len(selects)):
            variant = expression.copy()
            kept = [item for position, item in enumerate(variant.args["expressions"]) if position != index]
            variant.set("expressions", kept)
            if keep_order and variant.args.get("order"):
                # ORDER BY is by ordinal and must keep covering every output
                # column, or the sequence comparison stops being legitimate.
                variant.set("order", sqlglot.parse_one(_ordinal_order(len(kept)), dialect="postgres").args["order"])
            if variant.args.get("group"):
                continue
            candidates.append(variant)

    # A set operation or a CTE reduced to one of its parts.
    if isinstance(expression, exp.SetOperation):
        candidates.extend([expression.left.copy(), expression.right.copy()])

    for node in list(expression.walk())[:400]:
        if isinstance(node, exp.Subquery) and node.arg_key in ("this", "from") and node.this.args.get("from"):
            # `FROM (SELECT ... FROM t) AS a` reduced to `FROM t AS a`. The generic
            # hoist cannot do this one -- it would leave a bare SELECT in a FROM
            # clause -- and a derived table that survives shrinking is the
            # difference between two reports and one.
            inner = node.this.args["from"].this
            if isinstance(inner, exp.Table):
                relation = inner.copy()
                relation.set("alias", node.args.get("alias"))
                candidates.append(_replaced(expression, node, relation))
        if node is expression or isinstance(node, _SCAFFOLDING) or node.arg_key in _STRUCTURAL:
            continue
        for child in node.iter_expressions():
            if isinstance(child, (exp.Identifier, exp.TableAlias, exp.DataType)) or child.arg_key in _STRUCTURAL:
                continue
            candidates.append(_replaced(expression, node, child.copy()))
        for literal in _LITERALS:
            candidates.append(_replaced(expression, node, sqlglot.parse_one(literal, dialect="postgres")))

    return [candidate for candidate in candidates if candidate is not None]


def _rendered(tree: exp.Expression) -> str | None:
    """The candidate as SQL, or None if it is not renderable or not reparseable.

    A reckless move set produces trees sqlglot's own generator refuses -- hoisting
    a child into a position whose parent expects something else -- and one of those
    raising ValueError halfway through a run would lose every finding after it. The
    reparse is the same guard one step further out: a tree that renders to SQL
    which does not read back as the same shape would have the shrinker chasing a
    query nobody can reproduce.
    """
    try:
        sql = tree.sql(dialect="postgres")
        sqlglot.parse_one(sql, dialect="postgres")
    except Exception:
        return None
    return sql


def _ordinal_order(count: int) -> str:
    return "SELECT 1 ORDER BY " + ", ".join(str(position) for position in range(1, count + 1))


def _replaced(root: exp.Expression, node: exp.Expression, replacement: exp.Expression) -> exp.Expression | None:
    path = _path_to(root, node)
    if path is None:
        return None
    variant = root.copy()
    target = variant
    for key, index in path:
        value = target.args[key]
        target = value[index] if index is not None else value
    parent, (key, index) = target.parent, path[-1]
    if parent is None:
        return None
    if index is None:
        parent.set(key, replacement)
    else:
        items = list(parent.args[key])
        items[index] = replacement
        parent.set(key, items)
    return variant


def _path_to(root: exp.Expression, node: exp.Expression) -> list[tuple[str, int | None]] | None:
    """The arg keys leading from `root` to `node`, so the same position can be
    addressed inside a fresh copy of the tree."""
    for key, value in root.args.items():
        if isinstance(value, exp.Expression):
            if value is node:
                return [(key, None)]
            deeper = _path_to(value, node)
            if deeper is not None:
                return [(key, None), *deeper]
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, exp.Expression):
                    continue
                if item is node:
                    return [(key, index)]
                deeper = _path_to(item, node)
                if deeper is not None:
                    return [(key, index), *deeper]
    return None


def shrink(sql: str, verdict: Verdict, check: Check, keep_order: bool, budget: int = 600) -> tuple[str, int]:
    """The smallest query found that still produces `verdict`, and the evaluations spent."""
    try:
        best = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:
        return sql, 0
    best_sql, spent = sql, 0

    improved = True
    while improved and spent < budget:
        improved = False
        rendered = [(tree, _rendered(tree)) for tree in _variants(best, keep_order)]
        smaller = sorted(
            ((tree, sql) for tree, sql in rendered if sql is not None and len(sql) < len(best_sql)),
            key=lambda pair: len(pair[1]),
        )
        for candidate, candidate_sql in smaller:
            if spent >= budget:
                break
            spent += 1
            if check(candidate_sql) == verdict:
                best, best_sql, improved = candidate, candidate_sql, True
                break
    return best_sql, spent
