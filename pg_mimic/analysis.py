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

from functools import wraps

from sqlglot import exp
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify

from .describe import resolve_column_names, size_integer_literals, written_column_names


def _cached(method):
    """Memoize a no-argument method on the instance it was called on.

    Callers keep method syntax, which is the point: `analyzed.annotated()` reads
    as work being done, where a property of the same name would hide a qualify()
    and a full type annotation behind an attribute access. The class keeps no
    `self._x = None` bookkeeping either -- the value lives under a private key in
    the instance's own `__dict__`.

    Not `functools.cache`, which keys on `self` in a cache belonging to the
    *function*: an AnalyzedQuery is built per describe() and per query(), so every
    query a server ever answered would be held for the life of the process.
    """
    key = f"_cached_{method.__name__}"

    @wraps(method)
    def memoized(self):
        try:
            return self.__dict__[key]
        except KeyError:
            pass
        # Outside the handler on purpose. Called within it, anything `method` raises
        # is chained onto the miss -- and qualify() raising is a normal path here,
        # so every unresolvable column would report a KeyError on the cache key
        # first and the real error second.
        self.__dict__[key] = value = method(self)
        return value

    return memoized


class AnalyzedQuery:
    """One query, in each of the forms the rest of the package asks it about.

    Each form is derived on first use and kept, and each derives from the one
    before, so asking for `annotated()` qualifies on the way and asking for
    `qualified()` afterwards costs nothing.

    Every form owns its own tree, because sqlglot's passes rewrite in place and a
    shared one would make an accessor's answer depend on what had been called
    before it. So `qualified()` hands `qualify()` a copy of the raw tree, and
    `annotated()` sizes and annotates a copy of that.

    Keeping `raw()` whole is what the rest of it is for: it is the only record of
    what the query said, which is where Postgres decides column names, and every
    later form has destroyed some part of that.

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

    def raw(self) -> exp.Expression:
        """The query as written, after parsing and nothing else."""
        return self._raw

    def qualified(self) -> exp.Expression:
        """Names resolved, `*` expanded, every column attributed to a table."""
        return self._qualify()[0]

    def column_names(self) -> list[str]:
        """What Postgres calls each output column, decided on `raw()` rather than on
        any later form -- see `describe.written_column_names` for why that matters."""
        return self._qualify()[1]

    @_cached
    def _qualify(self) -> tuple[exp.Expression, list[str]]:
        """Qualifying and naming are one step because naming is only answerable
        across it: the names are read off the copy before qualify() rewrites it, and
        matched back up straight after, because the passes in annotated() replace the
        very nodes that matching keys on."""
        working = self._raw.copy()
        written = written_column_names(working)
        qualified = qualify(
            working,
            schema=self._schema,
            dialect=self._dialect,
            canonicalize_table_aliases=self._canonicalize_table_aliases,
        )
        return qualified, resolve_column_names(qualified, written)

    @_cached
    def annotated(self) -> exp.Expression:
        """Qualified, with integer literals widened as Postgres widens them and a
        type on every node. Sizing runs first so the annotator reads the width back
        off the widened literal rather than typing every constant INT.

        Works on its own copy, because both passes rewrite in place. Sharing the
        tree would make `qualified()` mean different things before and after this
        was called -- it would answer `SELECT 3000000000` first and
        `SELECT CAST(3000000000 AS BIGINT)` afterwards -- and a container whose
        accessors depend on call order is worse than no container.
        """
        working = self.qualified().copy()
        size_integer_literals(working)
        return annotate_types(working, schema=self._schema, dialect=self._dialect)
