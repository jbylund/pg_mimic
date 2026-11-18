"""A random query generator that emits SQL Postgres will actually accept.

The hard part of fuzzing SQL is not randomness, it is *validity*: a generator that
produces garbage 90% of the time spends its budget on Postgres syntax errors and
finds nothing. So this one is typed -- every production knows whether it is
building a number, a string, a boolean or a timestamp -- and it is careful about
the handful of things Postgres rejects at runtime rather than at parse time:

- division and mod by zero, avoided by only ever dividing by a non-zero literal or
  by ``NULLIF(x, 0)``;
- ``mod()`` and two-argument ``round()``, which Postgres has for numeric but not
  for double precision, so both operands get a NUMERIC cast;
- negative lengths to ``substring``.

The remaining invalid queries are simply discarded when Postgres refuses them,
which also means the shrinker in shrink.py can propose reckless variants and let
Postgres filter them.

Features are named and weighted so a run can drop one with ``--without``. That
matters more than it sounds: a single broken construct that the generator reaches
often -- ``||``, say, before sqlglot implemented it -- otherwise accounts for most
of the failures and hides everything underneath it.
"""

from __future__ import annotations

import random

from . import dataset
from .dataset import BOOL, NUM, TEXT, TS, Kind

FEATURES = [
    "arithmetic",
    "division",
    "mod",
    "round",
    "numeric_functions",
    "case",
    "cast",
    "coalesce",
    "nullif",
    "least_greatest",
    "concat",
    "string_functions",
    "substring",
    "like",
    "in_list",
    "between",
    "is_distinct_from",
    "logical",
    "timestamp",
    "date_trunc",
    "extract",
    "aggregate",
    "count_distinct",
    "group_by",
    "having",
    "distinct",
    "join",
    "outer_join",
    "cross_join",
    "self_join",
    "set_operation",
    "cte",
    "derived_table",
    "exists_subquery",
    "in_subquery",
    "scalar_subquery",
    "limit",
]

_TEXT_LITERALS = ["'abc'", "'ABC'", "''", "'a%b'", "'zzz'", "'x'", "'Bump version'"]
_TEXT_PATTERNS = ["'abc'", "'a%'", "'%b%'", "'_bc'", "'a\\%b'", "'%'", "'A%'"]
_NUM_LITERALS = ["0", "1", "2", "-1", "5", "2.5", "-0.5", "100", "0.0"]
_NONZERO_LITERALS = ["1", "2", "-1", "5", "2.5", "-0.5"]
_TS_LITERALS = ["TIMESTAMP '2024-03-15 00:00:00'", "TIMESTAMP '2024-01-15 10:30:00'"]
_DATE_PARTS = ["'year'", "'month'", "'day'", "'hour'"]
_EXTRACT_PARTS = ["YEAR", "MONTH", "DAY", "HOUR", "DOW"]


class Source:
    """One entry in a FROM clause: a relation, the alias it is under, and its columns."""

    def __init__(self, alias: str, table: dataset.Table, sql: str | None = None):
        self.alias = alias
        self.table = table
        self.sql = sql or table.name

    def columns_of(self, kind: Kind) -> list[str]:
        return [f"{self.alias}.{column.name}" for column in self.table.of_kind(kind)]

    @property
    def key(self) -> str:
        return f"{self.alias}.{self.table.columns[0].name}"

    @property
    def from_sql(self) -> str:
        return self.sql if self.sql == self.alias else f"{self.sql} AS {self.alias}"


class Generator:
    def __init__(self, seed: int, without: set[str] | None = None, ordered: bool = False, max_depth: int = 3):
        self.random = random.Random(seed)
        self.disabled = without or set()
        self.ordered = ordered
        self.max_depth = max_depth
        self._alias_counter = 0

    # -- plumbing ------------------------------------------------------------

    def on(self, feature: str) -> bool:
        return feature not in self.disabled

    def _pick(self, options: list) -> object:
        return self.random.choice(options)

    def _chance(self, probability: float) -> bool:
        return self.random.random() < probability

    def _alias(self) -> str:
        self._alias_counter += 1
        return f"a{self._alias_counter}"

    def _choices(self, options: list[tuple[str, float]]) -> str:
        """Weighted choice over (feature, weight), skipping disabled features."""
        live = [(name, weight) for name, weight in options if self.on(name) or name == "column"]
        total = sum(weight for _, weight in live)
        threshold = self.random.random() * total
        for name, weight in live:
            threshold -= weight
            if threshold <= 0:
                return name
        return live[-1][0]

    # -- expressions ---------------------------------------------------------

    def expr(self, kind: Kind, sources: list[Source], depth: int) -> str:
        if kind == NUM:
            return self.num(sources, depth)
        if kind == TEXT:
            return self.text(sources, depth)
        if kind == BOOL:
            return self.boolean(sources, depth)
        return self.timestamp(sources, depth)

    def _leaf(self, kind: Kind, sources: list[Source]) -> str:
        available = [column for source in sources for column in source.columns_of(kind)]
        literals = {NUM: _NUM_LITERALS, TEXT: _TEXT_LITERALS, BOOL: ["TRUE", "FALSE", "NULL"], TS: _TS_LITERALS}[kind]
        if not available or self._chance(0.25):
            return str(self._pick(literals))
        return str(self._pick(available))

    def num(self, sources: list[Source], depth: int) -> str:
        if depth <= 0:
            return self._leaf(NUM, sources)
        choice = self._choices(
            [
                ("column", 34),
                ("arithmetic", 14),
                ("division", 5),
                ("mod", 3),
                ("round", 4),
                ("numeric_functions", 8),
                ("case", 5),
                ("cast", 4),
                ("coalesce", 5),
                ("nullif", 3),
                ("least_greatest", 4),
                ("string_functions", 4),
                ("extract", 3),
                ("scalar_subquery", 2),
            ]
        )
        if choice == "column":
            return self._leaf(NUM, sources)
        if choice == "arithmetic":
            operator = self._pick(["+", "-", "*"])
            return f"({self.num(sources, depth - 1)} {operator} {self.num(sources, depth - 1)})"
        if choice == "division":
            return f"({self.num(sources, depth - 1)} / {self._divisor(sources, depth - 1)})"
        if choice == "mod":
            left = f"CAST({self.num(sources, depth - 1)} AS NUMERIC)"
            return f"MOD({left}, CAST({self._divisor(sources, depth - 1)} AS NUMERIC))"
        if choice == "round":
            inner = f"CAST({self.num(sources, depth - 1)} AS NUMERIC)"
            return f"ROUND({inner})" if self._chance(0.5) else f"ROUND({inner}, {self._pick(['0', '1', '2'])})"
        if choice == "numeric_functions":
            function = self._pick(["ABS", "CEIL", "FLOOR", "SIGN", "TRUNC"])
            return f"{function}({self.num(sources, depth - 1)})"
        if choice == "case":
            return (
                f"CASE WHEN {self.boolean(sources, depth - 1)} THEN {self.num(sources, depth - 1)} "
                f"ELSE {self.num(sources, depth - 1)} END"
            )
        if choice == "cast":
            return f"CAST({self.num(sources, depth - 1)} AS {self._pick(['NUMERIC', 'INTEGER', 'DOUBLE PRECISION'])})"
        if choice == "coalesce":
            return f"COALESCE({self.num(sources, depth - 1)}, {self._leaf(NUM, [])})"
        if choice == "nullif":
            return f"NULLIF({self.num(sources, depth - 1)}, {self._leaf(NUM, [])})"
        if choice == "least_greatest":
            function = self._pick(["LEAST", "GREATEST"])
            return f"{function}({self.num(sources, depth - 1)}, {self.num(sources, depth - 1)})"
        if choice == "string_functions":
            return f"LENGTH({self.text(sources, depth - 1)})"
        if choice == "extract":
            return f"EXTRACT({self._pick(_EXTRACT_PARTS)} FROM {self.timestamp(sources, depth - 1)})"
        return self._scalar_subquery(NUM)

    def _divisor(self, sources: list[Source], depth: int) -> str:
        if self._chance(0.6):
            return str(self._pick(_NONZERO_LITERALS))
        return f"NULLIF({self.num(sources, max(depth, 0))}, 0)"

    def text(self, sources: list[Source], depth: int) -> str:
        if depth <= 0:
            return self._leaf(TEXT, sources)
        choice = self._choices(
            [
                ("column", 40),
                ("concat", 14),
                ("string_functions", 14),
                ("substring", 8),
                ("coalesce", 8),
                ("nullif", 4),
                ("case", 6),
                ("least_greatest", 4),
            ]
        )
        if choice == "column":
            return self._leaf(TEXT, sources)
        if choice == "concat":
            return f"({self.text(sources, depth - 1)} || {self.text(sources, depth - 1)})"
        if choice == "string_functions":
            function = self._pick(["LOWER", "UPPER", "BTRIM", "LTRIM", "RTRIM", "REVERSE"])
            return f"{function}({self.text(sources, depth - 1)})"
        if choice == "substring":
            start, length = self._pick(["1", "2", "3"]), self._pick(["1", "2", "5"])
            return f"SUBSTRING({self.text(sources, depth - 1)}, {start}, {length})"
        if choice == "coalesce":
            return f"COALESCE({self.text(sources, depth - 1)}, {self._leaf(TEXT, [])})"
        if choice == "nullif":
            return f"NULLIF({self.text(sources, depth - 1)}, {self._leaf(TEXT, [])})"
        if choice == "case":
            return (
                f"CASE WHEN {self.boolean(sources, depth - 1)} THEN {self.text(sources, depth - 1)} "
                f"ELSE {self.text(sources, depth - 1)} END"
            )
        function = self._pick(["LEAST", "GREATEST"])
        return f"{function}({self.text(sources, depth - 1)}, {self.text(sources, depth - 1)})"

    def timestamp(self, sources: list[Source], depth: int) -> str:
        if depth <= 0 or not self.on("timestamp"):
            return self._leaf(TS, sources)
        choice = self._choices([("column", 60), ("date_trunc", 20), ("coalesce", 10), ("case", 10)])
        if choice == "date_trunc":
            return f"DATE_TRUNC({self._pick(_DATE_PARTS)}, {self.timestamp(sources, depth - 1)})"
        if choice == "coalesce":
            return f"COALESCE({self.timestamp(sources, depth - 1)}, {self._pick(_TS_LITERALS)})"
        if choice == "case":
            return (
                f"CASE WHEN {self.boolean(sources, depth - 1)} THEN {self.timestamp(sources, depth - 1)} "
                f"ELSE {self.timestamp(sources, depth - 1)} END"
            )
        return self._leaf(TS, sources)

    def boolean(self, sources: list[Source], depth: int) -> str:
        if depth <= 0:
            return self._comparison(sources, 0)
        choice = self._choices(
            [
                ("column", 32),
                ("logical", 16),
                ("like", 12),
                ("in_list", 8),
                ("between", 6),
                ("is_distinct_from", 5),
                ("exists_subquery", 5),
                ("in_subquery", 6),
            ]
        )
        if choice == "logical":
            if self._chance(0.25):
                return f"(NOT {self.boolean(sources, depth - 1)})"
            operator = self._pick(["AND", "OR"])
            return f"({self.boolean(sources, depth - 1)} {operator} {self.boolean(sources, depth - 1)})"
        if choice == "like":
            operator = self._pick(["LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE"])
            return f"({self.text(sources, depth - 1)} {operator} {self._pick(_TEXT_PATTERNS)})"
        if choice == "in_list":
            if self._chance(0.5):
                values = ", ".join(str(self._pick(_NUM_LITERALS)) for _ in range(2))
                subject = self.num(sources, depth - 1)
            else:
                values = ", ".join(str(self._pick(_TEXT_LITERALS)) for _ in range(2))
                subject = self.text(sources, depth - 1)
            negation = "NOT " if self._chance(0.4) else ""
            return f"({subject} {negation}IN ({values}))"
        if choice == "between":
            low, high = "0", self._pick(["3", "5", "100"])
            return f"({self.num(sources, depth - 1)} BETWEEN {low} AND {high})"
        if choice == "is_distinct_from":
            negation = "NOT " if self._chance(0.5) else ""
            return f"({self.num(sources, depth - 1)} IS {negation}DISTINCT FROM {self._leaf(NUM, sources)})"
        if choice == "exists_subquery":
            negation = "NOT " if self._chance(0.4) else ""
            inner = self._pick([dataset.T, dataset.U])
            correlation = f" WHERE {self._correlated_predicate(sources, inner)}" if sources and self._chance(0.7) else ""
            return f"({negation}EXISTS (SELECT 1 FROM {inner.name}{correlation}))"
        if choice == "in_subquery":
            negation = "NOT " if self._chance(0.4) else ""
            inner = self._pick([dataset.T, dataset.U])
            column = self._pick(inner.of_kind(NUM)).name
            return f"({self.num(sources, depth - 1)} {negation}IN (SELECT {column} FROM {inner.name}))"
        return self._comparison(sources, depth - 1)

    def _correlated_predicate(self, sources: list[Source], inner: dataset.Table) -> str:
        outer = self._pick([column for source in sources for column in source.columns_of(NUM)] or ["1"])
        return f"{inner.name}.{self._pick(inner.of_kind(NUM)).name} = {outer}"

    def _comparison(self, sources: list[Source], depth: int) -> str:
        kind = self._pick([NUM, NUM, NUM, TEXT, TEXT, BOOL, TS] if self.on("timestamp") else [NUM, NUM, TEXT, BOOL])
        if self._chance(0.2):
            negation = "NOT " if self._chance(0.5) else ""
            return f"({self.expr(kind, sources, depth)} IS {negation}NULL)"
        operator = self._pick(["=", "<>", "<", "<=", ">", ">="])
        return f"({self.expr(kind, sources, depth)} {operator} {self.expr(kind, sources, depth)})"

    def _scalar_subquery(self, kind: Kind) -> str:
        inner = self._pick([dataset.T, dataset.U])
        function = self._pick(["MAX", "MIN", "COUNT"])
        return f"(SELECT {function}({self._pick(inner.of_kind(kind)).name}) FROM {inner.name})"

    # -- aggregates ----------------------------------------------------------

    def aggregate(self, sources: list[Source], depth: int) -> str:
        function = self._pick(["COUNT", "SUM", "AVG", "MIN", "MAX", "BOOL_AND", "BOOL_OR", "STDDEV"])
        if function in ("BOOL_AND", "BOOL_OR"):
            return f"{function}({self.boolean(sources, depth)})"
        if function in ("MIN", "MAX"):
            return f"{function}({self.expr(self._pick([NUM, TEXT]), sources, depth)})"
        return self.numeric_aggregate(sources, depth, function)

    def numeric_aggregate(self, sources: list[Source], depth: int, function: str | None = None) -> str:
        function = function or self._pick(["COUNT", "SUM", "AVG", "MIN", "MAX", "STDDEV"])
        if function == "COUNT":
            if self._chance(0.3):
                return "COUNT(*)"
            distinct = "DISTINCT " if self.on("count_distinct") and self._chance(0.3) else ""
            return f"COUNT({distinct}{self.expr(self._pick([NUM, TEXT]), sources, depth)})"
        if function in ("MIN", "MAX"):
            return f"{function}({self.num(sources, depth)})"
        return f"{function}({self.num(sources, depth)})"

    # -- queries -------------------------------------------------------------

    def _from_clause(self) -> tuple[str, list[Source]]:
        base = self._pick(dataset.TABLES)
        sources = [Source(self._alias(), base)]
        clause = sources[0].from_sql

        if self.on("derived_table") and self._chance(0.1):
            alias = self._alias()
            columns = ", ".join(f"{column.name}" for column in base.columns)
            sources = [Source(alias, base, sql=f"(SELECT {columns} FROM {base.name})")]
            clause = sources[0].from_sql

        if self.on("join") and self._chance(0.45):
            partner = base if self.on("self_join") and self._chance(0.3) else self._pick(dataset.TABLES)
            joined = Source(self._alias(), partner)
            kinds = ["INNER"]
            if self.on("outer_join"):
                kinds += ["LEFT", "RIGHT", "FULL"]
            if self.on("cross_join"):
                kinds += ["CROSS"]
            join_kind = self._pick(kinds)
            if join_kind == "CROSS":
                clause += f" CROSS JOIN {joined.from_sql}"
            else:
                condition = self._join_condition(sources[0], joined, join_kind)
                clause += f" {join_kind} JOIN {joined.from_sql} ON {condition}"
            sources.append(joined)
        return clause, sources

    def _join_condition(self, left: Source, right: Source, join_kind: str) -> str:
        """An equality between the two sides, sometimes narrowed by a predicate.

        Not merely realistic -- necessary. Postgres refuses a FULL JOIN whose
        condition is not merge- or hash-joinable, so a generator that hands it an
        arbitrary boolean throws away every FULL JOIN sample it produces, which is
        the join type most worth fuzzing. An equality on two columns qualifies;
        FULL gets nothing else bolted on, the rest may be narrowed.
        """
        kind = self._pick([NUM, NUM, TEXT])
        pairs = [(a, b) for a in left.columns_of(kind) for b in right.columns_of(kind)]
        equality = "{} = {}".format(*self._pick(pairs)) if pairs else f"{left.key} = {right.key}"
        if join_kind == "FULL" or not self._chance(0.35):
            return equality
        return f"{equality} AND {self.boolean([left, right], 1)}"

    def _order_and_limit(self, column_count: int) -> str:
        """A total order over the whole output, or nothing at all.

        Ordering by *every* output column is what makes a sequence comparison
        against Postgres legitimate: two rows that tie on all of them are equal
        tuples, so any tie-break gives the same sequence. Ordering by a subset
        would make half the reported "wrong order" findings artifacts of an
        unspecified order, which is the classic way a SQL fuzzer wastes its
        owner's afternoon.

        Without --ordered there is no ORDER BY and rows are compared as a
        multiset -- and then LIMIT is suppressed too, since `LIMIT` without
        `ORDER BY` picks an arbitrary subset in Postgres and would report a
        divergence on every run.
        """
        if not self.ordered:
            return ""
        keys = []
        for position in range(1, column_count + 1):
            direction = " DESC" if self._chance(0.35) else ""
            nulls = ""
            if self._chance(0.3):
                nulls = f" NULLS {self._pick(['FIRST', 'LAST'])}"
            keys.append(f"{position}{direction}{nulls}")
        clause = " ORDER BY " + ", ".join(keys)
        if self.on("limit") and self._chance(0.25):
            clause += f" LIMIT {self._pick(['1', '2', '3'])}"
            if self._chance(0.4):
                clause += f" OFFSET {self._pick(['1', '2'])}"
        return clause

    def select(self, depth: int | None = None, top_level: bool = True) -> str:
        depth = self.max_depth if depth is None else depth

        if top_level and self.on("cte") and self._chance(0.07):
            inner = self.select(depth=depth - 1, top_level=False)
            return f"WITH c AS ({inner}) SELECT * FROM c" + self._order_and_limit(self._arity(inner))

        if top_level and self.on("set_operation") and self._chance(0.08):
            # Both branches from one list of kinds: Postgres refuses to match a
            # numeric column against a text one, and a set operation whose arms
            # disagree is a sample spent on the generator's own bug.
            kinds = [self._pick([NUM, NUM, TEXT, BOOL]) for _ in range(self.random.randint(1, 2))]
            left = self._plain_select(depth, len(kinds), with_tail=False, kinds=kinds)
            right = self._plain_select(depth, len(kinds), with_tail=False, kinds=kinds)
            operator = self._pick(["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
            return f"({left}) {operator} ({right})" + self._order_and_limit(len(kinds))

        return self._plain_select(depth, self.random.randint(1, 3), with_tail=top_level)

    def _plain_select(self, depth: int, arity: int, with_tail: bool = True, kinds: list[Kind] | None = None) -> str:
        clause, sources = self._from_clause()
        # A set-operation branch has its column types dictated to it, so it takes
        # the plain projection path and nothing else.
        grouped = kinds is None and self.on("group_by") and self._chance(0.22)
        aggregated = kinds is None and not grouped and self.on("aggregate") and self._chance(0.12)
        distinct = not grouped and not aggregated and self.on("distinct") and self._chance(0.12)

        if grouped:
            group_count = min(arity, self.random.randint(1, 2))
            group_exprs = [self.expr(self._pick([NUM, NUM, TEXT, BOOL]), sources, depth - 1) for _ in range(group_count)]
            projections = list(group_exprs)
            while len(projections) < arity:
                projections.append(self.aggregate(sources, depth - 1))
            select_list = ", ".join(projections)
            tail = " GROUP BY " + ", ".join(str(index + 1) for index in range(group_count))
            if self.on("having") and self._chance(0.3):
                # A numeric aggregate specifically: BOOL_AND(x) = 0 is a type error
                # in Postgres, and one that costs a whole sample every time.
                comparison = f"{self._pick(['>', '<', '='])} {self._pick(_NUM_LITERALS)}"
                tail += f" HAVING {self.numeric_aggregate(sources, depth - 1)} {comparison}"
        elif aggregated:
            select_list = ", ".join(self.aggregate(sources, depth - 1) for _ in range(arity))
            tail = ""
        else:
            kinds = kinds or [self._pick([NUM, NUM, NUM, TEXT, TEXT, BOOL, TS]) for _ in range(arity)]
            select_list = ", ".join(self.expr(kind, sources, depth) for kind in kinds)
            select_list = ("DISTINCT " if distinct else "") + select_list
            tail = ""

        where = f" WHERE {self.boolean(sources, depth)}" if self._chance(0.55) else ""
        query = f"SELECT {select_list} FROM {clause}{where}{tail}"
        return query + self._order_and_limit(arity) if with_tail else query

    def _arity(self, inner_sql: str) -> int:
        # The CTE wrapper selects *, so its width is the inner query's -- and the
        # inner query was built with a known arity, but only its text came back.
        # Counting top-level commas in the select list is enough here because no
        # production emits a bare comma outside of parentheses.
        head = inner_sql[len("SELECT ") : inner_sql.find(" FROM ")]
        depth_counter, count = 0, 1
        for character in head:
            if character == "(":
                depth_counter += 1
            elif character == ")":
                depth_counter -= 1
            elif character == "," and depth_counter == 0:
                count += 1
        return count
