"""`Table`, `Schema` and `resolve()` -- what a session declares about its tables.

The interesting assertions here are not that the containers hold what they were
given. They are the properties the rest of the package quietly depends on and that
nothing else would notice breaking:

- **order**, which decides OIDs and `ordinal_position`, so sorting either mapping
  renumbers something a client may already have introspected
- **immutability**, because a module-level `Table` is shared by every connection
- **which shapes `resolve()` refuses**, since the one it must refuse is the one where
  a table could end up with two different names

See https://github.com/jbylund/pg_mimic/issues/126.
"""

from __future__ import annotations

import pytest

from pg_mimic import Schema, Table
from pg_mimic.declared import resolve


def test_columns_are_read_only_even_though_the_dataclass_is_frozen():
    """`frozen=True` blocks rebinding the attribute, not mutating the mapping it holds.

    Load-bearing rather than tidy: `session_factory` runs once per client, so a
    module-level Table is shared by every connection, and a writable mapping there
    lets one connection alter another's catalog.
    """
    table = Table("commits", {"sha": "text"})

    with pytest.raises(TypeError):
        table.columns["sha"] = "integer"
    with pytest.raises(Exception):
        table.name = "other"

    assert dict(table.columns) == {"sha": "text"}


def test_a_table_does_not_alias_the_mapping_it_was_given():
    declared = {"sha": "text"}
    table = Table("commits", declared)
    declared["injected"] = "text"
    assert dict(table.columns) == {"sha": "text"}


def test_column_order_is_kept_because_it_is_ordinal_position():
    columns = {"z": "text", "a": "integer", "m": "date"}
    assert list(Table("t", columns).columns) == ["z", "a", "m"]


def test_table_order_is_kept_because_it_decides_oids():
    schema = Schema([Table("z", {}), Table("a", {}), Table("m", {})])
    assert list(schema.tables) == ["z", "a", "m"]


def test_a_table_with_no_columns_is_legal():
    """Postgres allows `CREATE TABLE footable ()`, queries it, and keeps its row count."""
    assert dict(Table("footable", {}).columns) == {}
    assert list(Schema([Table("footable", {})]).tables) == ["footable"]


def test_a_schema_refuses_two_tables_with_one_name():
    with pytest.raises(ValueError, match="both named 'commits'"):
        Schema([Table("commits", {"a": "text"}), Table("commits", {"b": "text"})])


def test_a_schemas_mapping_cannot_be_mutated_through_the_accessor():
    schema = Schema([Table("commits", {"sha": "text"})])
    with pytest.raises(TypeError):
        schema.tables["files"] = Table("files", {})
    assert list(schema.tables) == ["commits"]


def test_schema_equality_is_order_sensitive():
    """Two schemas declaring the same tables in a different order are not
    interchangeable, because the order is what assigns their OIDs."""
    one, two = Table("one", {}), Table("two", {})
    assert Schema([one, two]) == Schema([one, two])
    assert Schema([one, two]) != Schema([two, one])


def test_column_types_is_the_declaration_in_the_historical_shape():
    schema = Schema([Table("commits", {"sha": "text"}), Table("files", {"path": "text", "lines": "integer"})])
    assert schema.column_types() == {"commits": {"sha": "text"}, "files": {"path": "text", "lines": "integer"}}


def test_resolve_accepts_the_nested_dict_a_session_has_always_returned():
    schema = resolve({"commits": {"sha": "text"}, "files": {"path": "text"}})
    assert list(schema.tables) == ["commits", "files"]
    assert dict(schema.tables["commits"].columns) == {"sha": "text"}


def test_resolve_accepts_a_sequence_of_tables():
    assert list(resolve([Table("commits", {"sha": "text"})]).tables) == ["commits"]


def test_resolve_passes_a_schema_through_untouched():
    schema = Schema([Table("commits", {"sha": "text"})])
    assert resolve(schema) is schema


def test_resolve_turns_none_into_an_empty_schema():
    """The base `Session.schema()` no longer returns None, but an override may, and
    `test_catalog.py::test_a_session_that_declares_no_schema_can_still_be_introspected`
    is a session that does."""
    assert resolve(None).tables == {}


def test_resolve_refuses_a_mapping_of_name_to_table():
    """The one shape where a table would have two names -- the key and `Table.name` --
    so it is refused rather than having a winner picked for it."""
    with pytest.raises(TypeError, match="two names"):
        resolve({"commits": Table("comits", {"sha": "text"})})


@pytest.mark.parametrize(
    argnames=["declared"],
    argvalues=[["commits"], [42], [object()]],
    ids=["a string", "a number", "some other object"],
)
def test_resolve_refuses_what_is_not_a_schema_at_all(declared):
    with pytest.raises(TypeError):
        resolve(declared)


@pytest.mark.parametrize(
    argnames=["name", "columns"],
    argvalues=[["", {}], [None, {}], ["t", {"": "text"}], ["t", {"a": ""}], ["t", {"a": None}]],
    ids=["empty table name", "table name is not a string", "empty column name", "empty type name", "type is not a string"],
)
def test_a_table_refuses_a_name_or_type_that_is_not_a_non_empty_string(name, columns):
    with pytest.raises(ValueError):
        Table(name, columns)


# --- primary keys --------------------------------------------------------------------


def test_a_primary_key_is_normalised_to_a_tuple():
    """A bare string is one column, which is the common case and not worth making a
    caller spell as a one-tuple. Storing only the tuple means nothing downstream has to
    branch on which spelling it was given."""
    assert Table("t", {"a": "text"}, primary_key="a").primary_key == ("a",)
    assert Table("t", {"a": "text", "b": "text"}, primary_key=("a", "b")).primary_key == ("a", "b")
    assert Table("t", {"a": "text", "b": "text"}, primary_key=["b", "a"]).primary_key == ("b", "a")


def test_a_table_declares_no_primary_key_by_default():
    assert Table("t", {"a": "text"}).primary_key == ()


def test_a_composite_key_keeps_the_order_it_was_declared_in():
    """Not a set: the order is part of the key, and it is what conkey and psql report."""
    assert Table("t", {"a": "text", "b": "text"}, primary_key=("b", "a")).primary_key == ("b", "a")


def test_a_primary_key_must_name_the_tables_own_columns():
    with pytest.raises(ValueError, match="not one of its columns"):
        Table("t", {"a": "text"}, primary_key="b")


def test_a_primary_key_cannot_name_a_column_twice():
    with pytest.raises(ValueError, match="twice in its primary key"):
        Table("t", {"a": "text"}, primary_key=("a", "a"))


# --- unique constraints --------------------------------------------------------------


def test_unique_keys_are_normalised_the_same_way_a_primary_key_is():
    table = Table("t", {"a": "text", "b": "integer", "c": "text"}, unique=["a", ("b", "c")])
    assert table.unique == (("a",), ("b", "c"))


def test_a_single_unique_column_may_be_spelled_as_a_bare_string():
    assert Table("t", {"a": "text"}, unique="a").unique == (("a",),)


def test_a_table_declares_no_unique_constraints_by_default():
    assert Table("t", {"a": "text"}).unique == ()


def test_a_unique_constraint_must_name_the_tables_own_columns():
    with pytest.raises(ValueError, match="not one of its columns"):
        Table("t", {"a": "text"}, unique="b")


def test_a_unique_constraint_cannot_name_a_column_twice():
    with pytest.raises(ValueError, match="twice in its unique constraint"):
        Table("t", {"a": "text"}, unique=[("a", "a")])


def test_the_same_unique_constraint_cannot_be_declared_twice():
    """Two identical keys would name two constraints the same thing, which `\\d` renders
    as a duplicated line. Postgres tolerates it with a counter; here it is a copy-paste."""
    with pytest.raises(ValueError, match="same unique constraint twice"):
        Table("t", {"a": "text", "b": "text"}, unique=["a", "a"])


def test_a_unique_constraint_may_repeat_the_primary_key():
    """Redundant, and legal in Postgres too -- the names differ, so nothing collides."""
    table = Table("t", {"a": "text"}, primary_key="a", unique="a")
    assert (table.primary_key, table.unique) == (("a",), (("a",),))
