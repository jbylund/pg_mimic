"""What a session declares about its tables: `Table`, `Schema`, and `resolve()`.

`Session.schema()` used to return `{table_name: {column_name: type_str}}`, which
describes columns and nothing else -- so there was nowhere to say that `commits.sha`
is a primary key or that `commit_files.sha` references it, and therefore nowhere for
`pg_constraint` and `pg_index` to get rows from. These two objects are that
somewhere. See https://github.com/jbylund/pg_mimic/issues/126.

A leaf on purpose. `catalog`, `copy` and `tables` all import this, so it imports
nothing from the package in return -- #92 records the one real import cycle
pg_mimic already has and this is not joining it.

`resolve()` is the only thing that should read `schema()`'s return value, because it
is the one place that knows every shape a session is allowed to hand back: a
`Schema`, a sequence of `Table`, the historical nested dict, or None.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

__all__ = ["Schema", "Table", "resolve"]


@dataclass(frozen=True)
class Table:
    """One table as the catalog should describe it.

    `columns` maps a column name to the SQL spelling of its type -- `{"sha": "text"}`
    -- which is exactly what `Session.schema()` has always declared, and what
    `pg_mimic.describe.oid_for_declared_type` reads.

    **Column order is load-bearing.** It is `information_schema.ordinal_position` and
    `pg_attribute.attnum`, both taken straight from the order this mapping iterates
    in, so re-ordering it renumbers the columns of a table the client may already have
    introspected.

    Frozen because `session_factory` runs once per client: a module-level `Table` is
    shared by every connection, and mutable metadata there lets one connection alter
    another's catalog. `frozen=True` alone only blocks rebinding an attribute, not
    `table.columns["sha"] = "integer"`, so the mapping is copied into a read-only view
    on the way in.

    Not promised to be hashable -- the generated `__hash__` hashes the field tuple and
    a mapping is not hashable, so hashing raises. Nothing needs it.

    A table with no columns at all is legal, because Postgres allows
    `CREATE TABLE footable ()`, permits queries over it, and keeps its row count.

    `primary_key` names this table's own columns, in key order, and is normalised to a
    tuple so nothing downstream has to branch on the spelling::

        Table("commits", {"sha": "text", ...}, primary_key="sha")
        Table("commit_files", {"sha": "text", "path": "text"}, primary_key=("sha", "path"))

    A tuple rather than a set because a composite key's order is part of it -- it is
    what `pg_constraint.conkey` records and what psql prints. pg_mimic enforces
    nothing: nothing here stores rows, so a declared key is what the catalog reports,
    not a uniqueness check anything applies.

    `unique` is any number of further keys, each spelled the same way::

        Table("commits", {...}, primary_key="sha", unique=["author_email", ("a", "b")])

    A primary key and a unique constraint differ in three ways that matter here: there
    is at most one primary key and any number of unique ones, only a primary key's
    columns are implicitly NOT NULL, and they are named and rendered differently
    (`t_pkey` / `PRIMARY KEY (...)` against `t_a_b_key` / `UNIQUE (...)`).
    """

    name: str
    columns: Mapping[str, str]
    primary_key: str | Sequence[str] = ()
    unique: Sequence[str | Sequence[str]] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"a table name must be a non-empty string, not {self.name!r}")
        # Names are the identifier *as written*, the way a CREATE TABLE reads one:
        # pg_mimic folds the *query* (see tables.py's normalize_identifiers call) and
        # matches it against declared names untouched, which is why {"Users": ...}
        # answers `FROM "Users"` and not `FROM Users`. Everything lives in `public`,
        # so a name carries no schema qualifier.
        columns = dict(self.columns)
        for column, type_name in columns.items():
            if not isinstance(column, str) or not column:
                raise ValueError(f"a column name must be a non-empty string, not {column!r} in table {self.name!r}")
            if not isinstance(type_name, str) or not type_name:
                raise ValueError(f"{self.name!r}.{column} declares {type_name!r}, which is not a type name")
            # TODO(#134): the spelling itself is not checked yet, and an unrecognised
            # one silently resolves to text -- `varchar(255)` and `numeric(10,2)`
            # included. This is where that check goes, once #134 makes the ordinary
            # spellings resolve so validating them stops rejecting them.
        object.__setattr__(self, "columns", MappingProxyType(columns))
        object.__setattr__(self, "primary_key", _key_columns(self.name, "primary key", self.primary_key, columns))

        declared_unique = [self.unique] if isinstance(self.unique, str) else list(self.unique)
        keys = tuple(_key_columns(self.name, "unique constraint", key, columns) for key in declared_unique)
        # Two identical unique keys would name two constraints the same thing, which
        # `\\d` renders as a duplicated line. Postgres tolerates it and disambiguates with
        # a counter; here it is almost certainly a copy-paste, so it is refused.
        for key in keys:
            if keys.count(key) > 1:
                raise ValueError(f"{self.name!r} declares the same unique constraint twice: {key}")
        object.__setattr__(self, "unique", keys)


def _key_columns(table: str, kind: str, declared: str | Sequence[str], columns: Mapping[str, str]) -> tuple[str, ...]:
    """A key spec as a tuple of this table's own columns, in the order it was given.

    A bare string is one column, which is the common case and not worth making callers
    spell as a one-tuple. Checked against `columns` here rather than at resolve time,
    because a key naming a column the table does not have is knowable at construction
    and so should raise on the line that wrote it. Whether a *reference target* is
    covered by a key is the other kind of question and cannot be answered until every
    table is in -- see https://github.com/jbylund/pg_mimic/issues/129.
    """
    key = (declared,) if isinstance(declared, str) else tuple(declared)
    for column in key:
        if column not in columns:
            raise ValueError(f"{table!r} declares a {kind} on {column!r}, which is not one of its columns")
    if len(set(key)) != len(key):
        raise ValueError(f"{table!r} names a column twice in its {kind}: {key}")
    return key


class Schema:
    """The tables a session declares.

    Indexed by name for the lookups psql's `\\d` family needs -- its SQL is full of
    `WHERE c.oid = '16384'` -- built from `Table.name` rather than accepting a mapping,
    so a name and its key cannot disagree and a duplicate raises instead of quietly
    winning.

    **Table order is load-bearing**, for the same reason column order is: a table's OID
    is its position (`_FIRST_USER_OID + index` in `pg_mimic.catalog`). Insertion order
    is what `tables` iterates in, and sorting it renumbers every table.

    Mutating a `Schema` after it has been served is undefined. Assembly happens once,
    before serving; `tables` is exposed read-only so that the constraint-declaring
    methods in https://github.com/jbylund/pg_mimic/issues/130 can be the only way in,
    which also keeps an explicit immutable snapshot additive if one is ever wanted.
    """

    __slots__ = ("_tables",)

    def __init__(self, tables: Sequence[Table] = ()):
        by_name: dict[str, Table] = {}
        for table in tables:
            if not isinstance(table, Table):
                raise TypeError(f"a Schema holds Table objects, not {type(table).__name__}")
            if table.name in by_name:
                raise ValueError(f"two tables are both named {table.name!r}")
            by_name[table.name] = table
        self._tables = by_name

    @property
    def tables(self) -> Mapping[str, Table]:
        return MappingProxyType(self._tables)

    def column_types(self) -> dict[str, dict[str, str]]:
        """The declared spellings in the historical nested shape.

        For the callers that still want `{table: {column: type}}` -- sqlglot's own
        `schema=` argument takes that shape, and `examples/git_sql.py` passes it
        straight through. Deliberately *not* sqlglot's typed view of a schema, which
        quotes its names and uses sqlglot's spellings and is
        https://github.com/jbylund/pg_mimic/issues/135.
        """
        return {name: dict(table.columns) for name, table in self._tables.items()}

    def __repr__(self) -> str:
        return f"Schema({list(self._tables)!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Schema):
            return NotImplemented
        # Order-sensitive, because it decides OIDs: two schemas that declare the same
        # tables in a different order are not interchangeable.
        return list(self._tables.items()) == list(other._tables.items())


Declared = Schema | Sequence[Table] | Mapping[str, Mapping[str, str]] | None


def resolve(declared: Declared) -> Schema:
    """Whatever `Session.schema()` returned, as a `Schema`.

    None becomes an empty one. The base `Session.schema()` no longer returns None --
    it returns an empty `Schema`, because None and `{}` produced identical catalogs and
    the branch guarding the difference is what once crashed every catalog query -- but
    a session's own override may still return it, and one in the suite does.

    A `Mapping[str, Table]` is refused rather than accepted. It is the one shape where
    the key and `Table.name` could disagree, and picking a winner is worse than saying
    so.
    """
    if declared is None:
        return Schema()
    if isinstance(declared, Schema):
        return declared
    if isinstance(declared, Mapping):
        tables = []
        for name, columns in declared.items():
            if isinstance(columns, Table):
                raise TypeError(
                    "schema() may return a Schema or a sequence of Table, but not a mapping of "
                    f"name to Table -- {name!r} would have two names. Pass Schema([...]) instead."
                )
            tables.append(Table(str(name), columns))
        return Schema(tables)
    if isinstance(declared, (str, bytes)):
        raise TypeError(f"schema() returned {declared!r}, which is not a schema")
    if isinstance(declared, Sequence):
        return Schema(declared)
    raise TypeError(f"schema() returned {type(declared).__name__}, which is not a Schema, a sequence of Table, or a mapping")
