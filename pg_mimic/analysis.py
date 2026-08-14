"""The query, held in each form something needs to ask it about.

`pg_mimic.describe`'s module docstring spells the pipeline out in prose -- parse,
qualify, size, annotate, and the order matters -- because until now that was all
it could be: three callers each reassembled the steps themselves, and the order
was theirs to get right. `examples/git_sql.py` had already drifted once (#88).

This module is that prose as code. `AnalyzedQuery` owns the order, derives each
form once, and hands back whichever one a question is actually defined against:
column *names* come from the query as written, column *types* from the annotated
tree, and execution from whatever the caller rewrites on top (#111).

Above `describe` in the layering and below the sessions: it imports `describe`
and nothing else of ours, so `tables`, `catalog` and a session written outside
the package can all depend on it without a cycle.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify

from .describe import resolve_column_names, size_integer_literals, written_column_names


class AnalyzedQuery:
    """One query, in each of the forms the rest of the package asks it about.

    Each form is derived on first use and kept, and each derives from the one
    before, so asking for `annotated()` qualifies on the way and asking for
    `qualified()` afterwards costs nothing.

    `raw()` is never mutated. Everything past it works on a copy, because
    sqlglot's passes rewrite in place: `qualified()` hands `qualify()` a copy of
    the raw tree, and `annotated()` sizes and annotates that same copy. Keeping
    the raw tree whole is the point -- it is the only record of what the query
    said, which is where Postgres decides column names, and every later form has
    destroyed some part of that.

    Errors stay with the caller. `qualify()` raises differently depending on
    whether a name is unresolvable or the query is simply beyond us, and each
    caller already has its own answer to that -- a PgError with a particular
    SQLSTATE, or falling back rather than failing.
    """

    def __init__(
        self,
        expression: exp.Expression,
        *,
        schema: dict,
        dialect: str = "postgres",
        canonicalize_table_aliases: bool = False,
    ) -> None:
        self._raw = expression
        self._schema = schema
        self._dialect = dialect
        self._canonicalize_table_aliases = canonicalize_table_aliases
        self._qualified: exp.Expression | None = None
        self._annotated: exp.Expression | None = None
        self._column_names: list[str] | None = None

    def raw(self) -> exp.Expression:
        """The query as written, after parsing and nothing else."""
        return self._raw

    def qualified(self) -> exp.Expression:
        """Names resolved, `*` expanded, every column attributed to a table."""
        if self._qualified is None:
            # The copy is what gets rewritten, so raw() survives. Names are read off
            # it first, because qualify() is where the query's own naming stops being
            # recoverable, and matched back up straight after, because the passes in
            # annotated() replace the very nodes that matching keys on.
            working = self._raw.copy()
            written = written_column_names(working)
            self._qualified = qualify(
                working,
                schema=self._schema,
                dialect=self._dialect,
                canonicalize_table_aliases=self._canonicalize_table_aliases,
            )
            self._column_names = resolve_column_names(self._qualified, written)
        return self._qualified

    def column_names(self) -> list[str]:
        """What Postgres calls each output column, decided on `raw()` rather than on
        any later form -- see `describe.written_column_names` for why that matters."""
        if self._column_names is None:
            self.qualified()
        assert self._column_names is not None
        return self._column_names

    def annotated(self) -> exp.Expression:
        """Qualified, with integer literals widened as Postgres widens them and a
        type on every node. Sizing runs first so the annotator reads the width back
        off the widened literal rather than typing every constant INT."""
        if self._annotated is None:
            qualified = self.qualified()
            size_integer_literals(qualified)
            self._annotated = annotate_types(qualified, schema=self._schema, dialect=self._dialect)
        return self._annotated
