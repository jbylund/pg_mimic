"""The static shape of the emulated catalog.

Pure data, and a leaf: it imports nothing from its siblings, so both the module
that fills these tables in (pg_mimic.catalog) and the one that rewrites queries
against them (pg_mimic.catalog_rewrite) can depend on it without a cycle.

That cycle is why this module exists. The rewrite needs to know which columns
hold integers, which is a fact about the schema below -- reading it back from
pg_mimic.catalog meant either a lazy import or passing the set in as an argument,
both of which were working around the layering rather than fixing it.
"""

from __future__ import annotations

PG_CATALOG_SCHEMA = {
    "pg_catalog": {
        "pg_namespace": {"oid": "INT", "nspname": "TEXT", "nspowner": "INT"},
        "pg_class": {
            "oid": "INT",
            "relname": "TEXT",
            "relnamespace": "INT",
            "relkind": "TEXT",
            "relowner": "INT",
            "relam": "INT",
            "reltuples": "INT",
            "relpages": "INT",
            "relhasindex": "BOOLEAN",
            "relpersistence": "TEXT",
            "reltablespace": "INT",
            "relispartition": "BOOLEAN",
            "relrowsecurity": "BOOLEAN",
            "relforcerowsecurity": "BOOLEAN",
            "relreplident": "TEXT",
            "reloftype": "INT",
            "relhastriggers": "BOOLEAN",
            "relchecks": "INT",
            "relhasrules": "BOOLEAN",
            "reltoastrelid": "INT",
        },
        "pg_am": {"oid": "INT", "amname": "TEXT", "amhandler": "TEXT", "amtype": "TEXT"},
        "pg_attribute": {
            "attrelid": "INT",
            "attname": "TEXT",
            "atttypid": "INT",
            "attnum": "INT",
            "attnotnull": "BOOLEAN",
            "atthasdef": "BOOLEAN",
            "attisdropped": "BOOLEAN",
            "attidentity": "TEXT",
            "attgenerated": "TEXT",
            "attcollation": "INT",
            "atttypmod": "INT",
            "attstattarget": "INT",
            "attformattype": "TEXT",
            # How Postgres would store a column of this type. pg_mimic stores
            # nothing, so this is derived from the type rather than observed --
            # which is what psql's \\d+ "Storage" column is reporting anyway.
            "attstorage": "TEXT",
            "attcompression": "TEXT",
        },
        # Declared but always empty: pg_mimic models no defaults, collations,
        # indexes, constraints, triggers or inheritance. They still need their
        # columns declared, or a query selecting from them fails to resolve rather
        # than returning the no rows it should.
        "pg_attrdef": {"adrelid": "INT", "adnum": "INT", "adbin": "TEXT"},
        "pg_constraint": {
            "oid": "INT",
            "conname": "TEXT",
            "conrelid": "INT",
            "contype": "TEXT",
            "conparentid": "INT",
            "conindid": "INT",
            "confrelid": "INT",
            "condeferrable": "BOOLEAN",
            "condeferred": "BOOLEAN",
            "convalidated": "BOOLEAN",
            "connoinherit": "BOOLEAN",
            "conislocal": "BOOLEAN",
            "coninhcount": "INT",
            "conkey": "TEXT",
        },
        "pg_index": {
            "indexrelid": "INT",
            "indrelid": "INT",
            "indisprimary": "BOOLEAN",
            "indisunique": "BOOLEAN",
            "indisclustered": "BOOLEAN",
            "indisvalid": "BOOLEAN",
            "indisreplident": "BOOLEAN",
            "indkey": "TEXT",
        },
        "pg_inherits": {"inhrelid": "INT", "inhparent": "INT", "inhseqno": "INT", "inhdetachpending": "BOOLEAN"},
        "pg_rewrite": {"oid": "INT", "rulename": "TEXT", "ev_class": "INT", "ev_type": "TEXT", "is_instead": "BOOLEAN"},
        "pg_trigger": {
            "oid": "INT",
            "tgname": "TEXT",
            "tgrelid": "INT",
            "tgenabled": "TEXT",
            "tgisinternal": "BOOLEAN",
            "tgconstraint": "INT",
        },
        "pg_policy": {
            "oid": "INT",
            "polname": "TEXT",
            "polrelid": "INT",
            "polcmd": "TEXT",
            "polpermissive": "BOOLEAN",
            "polqual": "TEXT",
            "polwithcheck": "TEXT",
            "polroles": "TEXT",
        },
        "pg_roles": {
            "oid": "INT",
            "rolname": "TEXT",
            "rolsuper": "BOOLEAN",
            "rolinherit": "BOOLEAN",
            "rolcreaterole": "BOOLEAN",
            "rolcreatedb": "BOOLEAN",
            "rolcanlogin": "BOOLEAN",
            "rolconnlimit": "INT",
            "rolvaliduntil": "TEXT",
            "rolreplication": "BOOLEAN",
            "rolbypassrls": "BOOLEAN",
        },
        "pg_publication": {"oid": "INT", "pubname": "TEXT", "pubowner": "INT", "puballtables": "BOOLEAN"},
        "pg_publication_rel": {"oid": "INT", "prpubid": "INT", "prrelid": "INT", "prqual": "TEXT", "prattrs": "TEXT"},
        "pg_publication_namespace": {"oid": "INT", "pnpubid": "INT", "pnnspid": "INT"},
        # One row, for the database this connection is on -- filled in from the
        # startup packet rather than left empty, because \\l should list it.
        "pg_database": {
            "oid": "INT",
            "datname": "TEXT",
            "datdba": "INT",
            "datcollate": "TEXT",
            "datctype": "TEXT",
            "datlocprovider": "TEXT",
            "daticulocale": "TEXT",
            "daticurules": "TEXT",
            "datacl": "TEXT",
            "datallowconn": "BOOLEAN",
            "datconnlimit": "INT",
            "dattablespace": "INT",
        },
        "pg_statistic_ext": {
            "oid": "INT",
            "stxname": "TEXT",
            "stxrelid": "INT",
            "stxnamespace": "INT",
            "stxkind": "TEXT",
            "stxstattarget": "INT",
        },
        "pg_collation": {"oid": "INT", "collname": "TEXT"},
        "pg_type": {
            "oid": "INT",
            "typcollation": "INT",
            "typname": "TEXT",
            "typnamespace": "INT",
            "typtype": "TEXT",
            "typelem": "INT",
            "typbasetype": "INT",
            "typrelid": "INT",
            "typcategory": "TEXT",
            "typdelim": "TEXT",
            # The array type over this one, which pg_mimic knows exactly: see
            # arrays.ARRAY_OID. 0 where there is none, as in Postgres.
            "typarray": "INT",
        },
    }
}


# psql writes OIDs as string literals (`WHERE c.oid = '16384'`); the rewrite has to
# know which columns to coerce them against. Derived from the schema above rather
# than listed again, so a column added there is covered without anyone remembering.
INTEGER_COLUMNS = {
    column for table in PG_CATALOG_SCHEMA["pg_catalog"].values() for column, column_type in table.items() if column_type == "INT"
}


DECLARED_TYPE_OIDS = {
    "integer": "INT4",
    "int": "INT4",
    "int4": "INT4",
    "bigint": "INT8",
    "int8": "INT8",
    "smallint": "INT2",
    "int2": "INT2",
    "text": "TEXT",
    "varchar": "VARCHAR",
    "character varying": "VARCHAR",
    "boolean": "BOOL",
    "bool": "BOOL",
    "real": "FLOAT4",
    "double precision": "FLOAT8",
    "numeric": "NUMERIC",
    "date": "DATE",
    "time": "TIME",
    "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMPTZ",
    "interval": "INTERVAL",
    "uuid": "UUID",
    "json": "JSON",
    "jsonb": "JSONB",
    "bytea": "BYTEA",
}
