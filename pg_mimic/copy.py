"""The COPY sub-protocol: statement recognition, the text/CSV codecs, and the
Statement/Portal pair that puts a COPY on the same footing as any other statement.

COPY is the one command whose execution isn't Bind/Execute. Once the server has
answered with CopyInResponse or CopyOutResponse, the connection belongs to a
stream of CopyData messages until CopyDone/CopyFail. The split this module draws
is the one the rest of the codebase draws: it owns the *format* (what a row looks
like as bytes), connection.py owns the message loop -- so a session author only
ever sees decoded rows, through `Session.copy_in()` / `Session.copy_out()`.

Only the STDIN/STDOUT forms live here. `COPY t FROM '/path'` is a server-side
file read with no client protocol at all, so it falls through to the session like
any other statement pg_mimic doesn't model.

Binary COPY is refused rather than attempted -- the same call binary result
encoding makes. A guessed tuple layout produces plausible-looking wrong rows
where a refusal produces a failure the client reports.

COPY's options arrive in either of two syntaxes and both turn up in practice:
asyncpg writes the modern parenthesised list (without the optional WITH, which
sqlglot then can't parse), and psql's `\\copy` forwards whatever option text the
user typed, which may be the legacy bare-keyword form. So the option list is
scanned here rather than taken from sqlglot's parse tree, for the same reason
middleware.py classifies SET/SHOW with regexes.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Sequence

from .errors import (
    FEATURE_NOT_SUPPORTED,
    INTERNAL_ERROR,
    INVALID_PARAMETER_VALUE,
    SYNTAX_ERROR,
    PgError,
)
from .results import ResultColumn
from .session import Portal, Row, Session, Statement, _resolve_row_source
from .types import UNKNOWN, encode_value

DIRECTION_IN = "in"
DIRECTION_OUT = "out"


@dataclass(frozen=True)
class CopyOptions:
    """The COPY options pg_mimic implements, already resolved against the
    per-format defaults (tab and `\\N` for text, comma and empty for CSV)."""

    format: str = "text"
    delimiter: str = "\t"
    null_string: str = "\\N"
    header: bool = False
    quote: str = '"'
    escape: str = '"'

    @property
    def is_csv(self) -> bool:
        return self.format == "csv"


@dataclass(frozen=True)
class ParsedCopy:
    direction: str  # DIRECTION_IN or DIRECTION_OUT
    table: str | None  # None when the source is a parenthesised query
    columns: list[str] | None  # the explicit column list, if the statement has one
    options: CopyOptions


# --- statement recognition ----------------------------------------------------------

# The endpoint is matched first because it's the single token that says this COPY
# is a protocol exchange rather than a server-side file read -- the target and the
# option list are both too free-form to lead with. The non-greedy target lets
# `COPY (SELECT ... FROM t) TO STDOUT` backtrack past its own FROM.
_COPY_STDIO_RE = re.compile(
    r"^\s*COPY\s+(?P<target>.+?)\s+(?:FROM\s+STDIN|TO\s+(?P<stdout>STDOUT))\b(?P<options>.*?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _split_outside_quotes(text: str, separator: str) -> list[str]:
    """Split on `separator`, ignoring any that falls inside a quoted identifier."""
    parts = [""]
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        if char == separator and not quoted:
            parts.append("")
        else:
            parts[-1] += char
    return parts


def _split_target(target: str) -> tuple[str | None, str | None]:
    """`t (a, b)` as ("t", "a, b"), and a bare `t` as ("t", None).

    Scanned rather than matched with a regex: a quoted identifier may contain a
    space or a parenthesis, which is where a `[^\\s(]+` name pattern gave up -- and
    it gave up on the whole target, throwing away an explicit column list that was
    right there in the statement.
    """
    quoted = False
    for position, char in enumerate(target):
        if char == '"':
            quoted = not quoted
        elif char == "(" and not quoted:
            if not target.endswith(")"):
                return None, None
            return target[:position].strip(), target[position + 1 : -1]
    return (None, None) if quoted else (target, None)


def parse_copy(sql: str) -> ParsedCopy | None:
    """A `COPY ... FROM STDIN` / `COPY ... TO STDOUT`, or None if `sql` isn't one.

    Raises for a COPY that *is* one of those but asks for something pg_mimic
    doesn't implement (binary format, FORCE_QUOTE, ...). Passing it to the session
    instead would be worse than a clean error: the client is waiting for a
    CopyInResponse and would hang on whatever the session answered with.
    """
    match = _COPY_STDIO_RE.match(sql)
    if match is None:
        return None
    table, columns = _parse_target(match.group("target"))
    return ParsedCopy(
        direction=DIRECTION_OUT if match.group("stdout") else DIRECTION_IN,
        table=table,
        columns=columns,
        options=_parse_options(match.group("options")),
    )


def _parse_target(target: str) -> tuple[str | None, list[str] | None]:
    target = target.strip()
    if target.startswith("("):
        return None, None  # a query: it has no table name and no column list of its own
    name, raw_columns = _split_target(target)
    if name is None:
        return None, None
    # Split outside quotes, so `("a,b", c)` is the two columns it names rather than
    # three -- a miscount that goes on to declare the wrong arity in
    # CopyInResponse/CopyOutResponse.
    columns = [_unquote_identifier(c) for c in _split_outside_quotes(raw_columns, ",")] if raw_columns else None
    # Only the last dotted part is kept -- Session.schema() names tables without a
    # schema qualifier -- and a dot inside a quoted name is not a separator.
    return _unquote_identifier(_split_outside_quotes(name, ".")[-1]), columns


def _unquote_identifier(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    return name.lower()  # an unquoted identifier folds to lower case in Postgres


# --- option list scanning -----------------------------------------------------------

_TOKEN_RE = re.compile(r"""\s*(?:(?P<string>'(?:[^']|'')*')|(?P<punct>[(),*])|(?P<word>[^\s(),*']+))""")

# Every option name COPY accepts, whether or not pg_mimic implements it, split by
# whether the name is followed by a value. Listing the ones we don't implement is
# the point: FORCE_QUOTE or ENCODING silently dropped would change the bytes on
# the wire, so they're refused by name in _parse_options rather than ignored.
_VALUE_OPTIONS = {
    "FORMAT",
    "DELIMITER",
    "NULL",
    "QUOTE",
    "ESCAPE",
    "ENCODING",
    "FORCE_QUOTE",
    "FORCE_NOT_NULL",
    "FORCE_NULL",
    "DEFAULT",
    "ON_ERROR",
    "LOG_VERBOSITY",
    "REJECT_LIMIT",
}
_FLAG_OPTIONS = {"CSV", "BINARY", "HEADER", "FREEZE", "OIDS"}
_BOOLEAN_WORDS = {"TRUE": True, "FALSE": False, "ON": True, "OFF": False, "1": True, "0": False}


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        # The alternation is required, so trailing whitespace alone fails to match.
        match = _TOKEN_RE.match(text, position)
        if match is None:
            if text[position:].strip():
                raise PgError(SYNTAX_ERROR, f"could not parse COPY options at {text[position:].strip()!r}")
            break
        position = match.end()
        if match.group("string") is not None:
            tokens.append(("string", match.group("string")[1:-1].replace("''", "'")))
        elif match.group("punct") is not None:
            tokens.append(("punct", match.group("punct")))
        else:
            tokens.append(("word", match.group("word")))
    return tokens


def _option_items(text: str) -> list[tuple[str, Any]]:
    """(NAME, value) pairs from either COPY option syntax -- the parenthesised
    modern list or the legacy bare keywords. A flag's value is True."""
    tokens = _tokenize(text)
    index = 0
    if index < len(tokens) and tokens[index][0] == "word" and tokens[index][1].upper() == "WITH":
        index += 1  # optional since Postgres 9.0, and asyncpg omits it
    parenthesised = index < len(tokens) and tokens[index] == ("punct", "(")
    if parenthesised:
        index += 1

    items: list[tuple[str, Any]] = []
    while index < len(tokens):
        kind, token = tokens[index]
        if kind == "punct":
            if token == "," or (token == ")" and parenthesised):
                index += 1
                if token == ")":
                    break
                continue
            raise PgError(SYNTAX_ERROR, f"unexpected {token!r} in COPY options")
        name, index = _read_option_name(tokens, index)
        if name in _VALUE_OPTIONS:
            if index >= len(tokens):
                raise PgError(SYNTAX_ERROR, f"COPY option {name.lower()} requires a value")
            value, index = _read_option_value(tokens, index, name)
        elif name in _FLAG_OPTIONS:
            value, index = _read_flag_value(tokens, index)
        else:
            raise PgError(SYNTAX_ERROR, f'unrecognized COPY option "{name.lower()}"')
        items.append((name, value))
    # Anything left is past the closing ')' of the option list. Ignoring it would
    # drop a clause that changes the answer -- `WITH (FORMAT csv) WHERE id > 100`
    # is a PG12+ row filter, and silently copying every row instead is exactly the
    # kind of quiet wrong answer the options are refused by name to avoid.
    if index < len(tokens):
        raise PgError(SYNTAX_ERROR, f"unexpected {tokens[index][1]!r} after the COPY option list")
    return items


def _read_option_name(tokens: list[tuple[str, str]], index: int) -> tuple[str, int]:
    kind, token = tokens[index]
    if kind != "word":
        raise PgError(SYNTAX_ERROR, f"expected a COPY option name, got {token!r}")
    name = token.upper()
    index += 1
    if name == "FORCE":
        # The legacy syntax spells these as separate words: FORCE QUOTE, FORCE NOT
        # NULL, FORCE NULL. Joined up so they're refused under the one name.
        while index < len(tokens) and tokens[index][0] == "word" and tokens[index][1].upper() in ("QUOTE", "NOT", "NULL"):
            name += "_" + tokens[index][1].upper()
            index += 1
    return name, index


# The legacy spellings that take an optional `AS` noise word between the option and
# its value -- `DELIMITER AS '|'`, `NULL AS ''`. psql's `\copy ... with delimiter as
# '|'` forwards it verbatim, so refusing it refuses the legacy form outright.
_AS_NOISE_OPTIONS = {"DELIMITER", "NULL", "QUOTE", "ESCAPE"}


def _read_option_value(tokens: list[tuple[str, str]], index: int, name: str) -> tuple[Any, int]:
    kind, token = tokens[index]
    if kind == "word" and token.upper() == "AS" and name in _AS_NOISE_OPTIONS:
        index += 1
        if index >= len(tokens):
            raise PgError(SYNTAX_ERROR, f"COPY option {name.lower()} requires a value")
        kind, token = tokens[index]
    if kind != "punct":
        return token, index + 1
    if token == "*":
        return "*", index + 1
    if token == "(":
        # A column list, e.g. FORCE_QUOTE (a, b) -- consumed whole so the option can
        # be refused by name instead of its tail being misread as further options.
        names = []
        index += 1
        while index < len(tokens) and tokens[index] != ("punct", ")"):
            if tokens[index][0] != "punct":
                names.append(tokens[index][1])
            index += 1
        return names, index + 1
    raise PgError(SYNTAX_ERROR, f"unexpected {token!r} in COPY options")


def _read_flag_value(tokens: list[tuple[str, str]], index: int) -> tuple[Any, int]:
    """A bare flag is True. HEADER/FREEZE/OIDS may carry an explicit boolean (and
    HEADER a MATCH), but only those words are eaten: the legacy `CSV HEADER` is two
    flags, not a flag with a value."""
    if index < len(tokens) and tokens[index][0] == "word":
        word = tokens[index][1].upper()
        if word in _BOOLEAN_WORDS:
            return _BOOLEAN_WORDS[word], index + 1
        if word == "MATCH":
            return "match", index + 1
    return True, index


def _parse_options(text: str) -> CopyOptions:
    items = _option_items(text)
    names = {name for name, _ in items}

    # FORMAT is resolved first: it sets the defaults every other option overrides.
    fmt = "text"
    for name, value in items:
        if name == "FORMAT":
            fmt = str(value).lower()
        elif name in ("CSV", "BINARY") and value is not False:
            fmt = name.lower()  # the legacy spelling of the same thing
    if fmt == "binary":
        raise PgError(
            FEATURE_NOT_SUPPORTED,
            "binary COPY is not supported: pg_mimic has no table definition to encode a tuple against, "
            "and guessing one would produce plausible-looking wrong rows rather than a failure. "
            "Use FORMAT text or FORMAT csv.",
        )
    if fmt not in ("text", "csv"):
        raise PgError(SYNTAX_ERROR, f'COPY format "{fmt}" does not exist')

    is_csv = fmt == "csv"
    delimiter = "," if is_csv else "\t"
    null_string = "" if is_csv else "\\N"
    header = False
    quote = '"'
    escape: str | None = None

    for name, value in items:
        if name in ("FORMAT", "CSV", "BINARY"):
            continue
        if name == "DELIMITER":
            delimiter = _single_character(name, value)
        elif name == "NULL":
            null_string = str(value)
        elif name == "HEADER":
            if value == "match":
                raise PgError(
                    FEATURE_NOT_SUPPORTED,
                    "COPY HEADER MATCH is not supported: pg_mimic has no authoritative column list to "
                    "check the header line against",
                )
            header = bool(value)
        elif name == "QUOTE":
            quote = _single_character(name, value)
        elif name == "ESCAPE":
            escape = _single_character(name, value)
        elif name == "ENCODING":
            if str(value).upper().replace("-", "").replace("_", "") not in ("UTF8", "UNICODE"):
                raise PgError(FEATURE_NOT_SUPPORTED, f'COPY ENCODING "{value}" is not supported: pg_mimic speaks UTF-8 only')
        else:
            raise PgError(FEATURE_NOT_SUPPORTED, f'COPY option "{name.lower()}" is not supported by pg_mimic')

    if not is_csv and names & {"QUOTE", "ESCAPE"}:
        raise PgError(FEATURE_NOT_SUPPORTED, "COPY quote/escape available only in CSV mode")

    return CopyOptions(
        format=fmt,
        delimiter=delimiter,
        null_string=null_string,
        header=header,
        quote=quote,
        # ESCAPE defaults to the quote character, which is what makes "" the way a
        # literal quote is written in a quoted CSV field.
        escape=quote if escape is None else escape,
    )


def _single_character(name: str, value: Any) -> str:
    text = str(value)
    if len(text) != 1:
        raise PgError(INVALID_PARAMETER_VALUE, f"COPY {name.lower()} must be a single one-byte character")
    return text


# --- encoding (COPY ... TO STDOUT) --------------------------------------------------

# Postgres's own output escapes: the six C escapes plus the backslash. Other
# control characters go out raw, as they do from a real server.
_TEXT_ESCAPES = {"\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\v": "\\v"}


class CopyEncoder:
    """Rows -> the payload of one CopyData message each."""

    def __init__(self, options: CopyOptions):
        self._options = options

    def header(self, names: Sequence[str]) -> bytes:
        return self._line([str(name) for name in names])

    def row(self, row: Sequence[Any]) -> bytes:
        return self._line([None if value is None else copy_text(value) for value in row])

    def _line(self, fields: Sequence[str | None]) -> bytes:
        render = self._csv_field if self._options.is_csv else self._text_field
        return (self._options.delimiter.join(render(field) for field in fields) + "\n").encode("utf-8")

    def _text_field(self, value: str | None) -> str:
        if value is None:
            return self._options.null_string
        delimiter = self._options.delimiter
        out = []
        for char in value:
            escaped = _TEXT_ESCAPES.get(char)
            if escaped is not None:
                out.append(escaped)
            elif char == delimiter:
                out.append("\\" + char)
            else:
                out.append(char)
        return "".join(out)

    def _csv_field(self, value: str | None) -> str:
        options = self._options
        if value is None:
            # Unquoted, so it reads back as NULL: in CSV the null string only means
            # NULL when it wasn't quoted.
            return options.null_string
        specials = (options.delimiter, options.quote, "\n", "\r")
        needs_quotes = value == options.null_string or any(char in value for char in specials)
        if not needs_quotes:
            return value
        body = "".join((options.escape + char) if char in (options.quote, options.escape) else char for char in value)
        return options.quote + body + options.quote


def copy_text(value: Any) -> str:
    """A COPY field's text for a Python value.

    The copy sub-protocol has no RowDescription and no describe(), so there are no
    declared column types here -- the value's own type has to settle its
    representation. That's the same dispatch `encode_value` does for every scalar.
    The two shapes it can't settle without a declared OID are refused instead: a
    list is equally an array or a json document, and a dict is json, and rendering
    either on a guess would put a Python repr on the wire.
    """
    if isinstance(value, (list, dict, tuple, set)):
        raise PgError(
            FEATURE_NOT_SUPPORTED,
            f"COPY cannot encode a {type(value).__name__} without a declared column type: yield it "
            f"already formatted (an array literal, or a JSON string) from copy_out() instead",
        )
    return encode_value(UNKNOWN, value)


# --- decoding (COPY ... FROM STDIN) -------------------------------------------------

_TEXT_UNESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


class CopyInDecoder:
    """CopyData payloads -> decoded rows, incrementally.

    Stateful because none of the boundaries line up: a row can span CopyData
    messages, a quoted CSV field can contain the newline that would otherwise end
    one, and a multi-byte UTF-8 character can be split across two payloads.
    """

    def __init__(self, options: CopyOptions):
        self._options = options
        self._utf8 = codecs.getincrementaldecoder("utf-8")()
        self._pending = ""
        self._ended = False  # the \. end-of-data marker has been seen
        self._skip_header = options.header

    def feed(self, data: bytes) -> list[Row]:
        return self._consume(self._utf8.decode(data))

    def finish(self) -> list[Row]:
        """The rows left over at CopyDone -- a last record with no trailing
        newline is still a record."""
        rows = self._consume(self._utf8.decode(b"", final=True))
        leftover = self._pending.rstrip("\r\n")
        self._pending = ""
        if leftover and not self._ended:
            rows.extend(self._record(leftover))
        return rows

    def _consume(self, text: str) -> list[Row]:
        self._pending += text
        rows: list[Row] = []
        while not self._ended:
            record, remainder = self._split_record(self._pending)
            if record is None:
                break
            self._pending = remainder
            rows.extend(self._record(record))
        if self._ended:
            self._pending = ""  # everything after the marker is ignored
        return rows

    def _record(self, record: str) -> list[Row]:
        """Zero or one row: the end-of-data marker and a skipped header produce none."""
        if record.endswith("\r"):
            record = record[:-1]
        if record == "\\.":
            # Postgres's end-of-data marker, still recognised in CSV mode (outside
            # quotes) where it predates the format.
            self._ended = True
            return []
        if self._skip_header:
            self._skip_header = False
            return []
        if self._options.is_csv:
            return [tuple(_csv_fields(record, self._options))]
        return [tuple(_text_fields(record, self._options))]

    def _split_record(self, buffer: str) -> tuple[str | None, str]:
        if not self._options.is_csv:
            # A raw newline can't occur inside a text-format field -- it has to
            # arrive as the two characters \n -- so records split on it directly.
            index = buffer.find("\n")
            return (None, buffer) if index < 0 else (buffer[:index], buffer[index + 1 :])
        return self._split_csv_record(buffer)

    def _split_csv_record(self, buffer: str) -> tuple[str | None, str]:
        quote, escape = self._options.quote, self._options.escape
        in_quotes = False
        index = 0
        while index < len(buffer):
            char = buffer[index]
            if not in_quotes:
                if char == quote:
                    in_quotes = True
                elif char == "\n":
                    return buffer[:index], buffer[index + 1 :]
                index += 1
            elif escape != quote and char == escape:
                index += 2  # whatever follows is a literal character
            elif char == quote:
                if escape != quote:
                    in_quotes = False
                    index += 1
                elif index + 1 < len(buffer):
                    # A doubled quote is a literal one; a single quote ends the
                    # field. Which this is decides whether a following newline ends
                    # the record, so both characters have to be in hand.
                    in_quotes = buffer[index + 1] == quote
                    index += 2 if in_quotes else 1
                else:
                    return None, buffer
            else:
                index += 1
        return None, buffer


def _text_fields(record: str, options: CopyOptions) -> list[str | None]:
    """Split on unescaped delimiters, then de-escape.

    The null string is compared against the *raw* field, before de-escaping, which
    is Postgres's own rule: `\\N` is NULL, while `\\\\N` is the two-character
    string it de-escapes to.
    """
    raw_fields: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(record):
        char = record[index]
        if char == "\\" and index + 1 < len(record):
            current.append(record[index : index + 2])
            index += 2
        elif char == options.delimiter:
            raw_fields.append("".join(current))
            current = []
            index += 1
        else:
            current.append(char)
            index += 1
    raw_fields.append("".join(current))
    return [None if field == options.null_string else _unescape_text(field) for field in raw_fields]


def _unescape_text(field: str) -> str:
    if "\\" not in field:
        return field
    out: list[str] = []
    index = 0
    while index < len(field):
        char = field[index]
        if char != "\\" or index + 1 >= len(field):
            out.append(char)
            index += 1
            continue
        marker = field[index + 1]
        if marker in _TEXT_UNESCAPES:
            out.append(_TEXT_UNESCAPES[marker])
            index += 2
        elif marker == "x" and (digits := _take_digits(field, index + 2, "0123456789abcdefABCDEF", 2)):
            out.append(chr(int(digits, 16)))
            index += 2 + len(digits)
        elif marker in "01234567":
            digits = _take_digits(field, index + 1, "01234567", 3)
            out.append(chr(int(digits, 8)))
            index += 1 + len(digits)
        else:
            # Any other escaped character is itself -- that's what makes \\ a
            # backslash and \. a period.
            out.append(marker)
            index += 2
    return "".join(out)


def _take_digits(text: str, start: int, allowed: str, limit: int) -> str:
    end = start
    while end < len(text) and end - start < limit and text[end] in allowed:
        end += 1
    return text[start:end]


def _csv_fields(record: str, options: CopyOptions) -> list[str | None]:
    """Split a CSV record. A field is NULL only if it matched the null string
    *unquoted*: `a,,b` has a NULL where `a,"",b` has an empty string."""
    quote, escape, delimiter = options.quote, options.escape, options.delimiter
    fields: list[str | None] = []
    current: list[str] = []
    quoted = False
    in_quotes = False
    index = 0
    while index < len(record):
        char = record[index]
        if in_quotes:
            if escape != quote and char == escape and index + 1 < len(record):
                current.append(record[index + 1])
                index += 2
            elif char == quote:
                if escape == quote and index + 1 < len(record) and record[index + 1] == quote:
                    current.append(quote)
                    index += 2
                else:
                    in_quotes = False
                    index += 1
            else:
                current.append(char)
                index += 1
        elif char == quote:
            in_quotes = True
            quoted = True
            index += 1
        elif char == delimiter:
            fields.append(_csv_value(current, quoted, options))
            current, quoted = [], False
            index += 1
        else:
            current.append(char)
            index += 1
    fields.append(_csv_value(current, quoted, options))
    return fields


def _csv_value(chars: list[str], quoted: bool, options: CopyOptions) -> str | None:
    text = "".join(chars)
    return None if not quoted and text == options.null_string else text


# --- the Statement/Portal pair ------------------------------------------------------


class CopyStatement(Statement):
    """A parsed `COPY ... FROM STDIN` / `COPY ... TO STDOUT`.

    describe() is None because COPY sends no RowDescription in either direction:
    the rows travel as CopyData, and the only thing that comes back on the normal
    path is the CommandComplete. Connection recognises this type and drives the
    exchange itself rather than going through Portal.execute() -- see
    Connection._run_copy.
    """

    def __init__(self, sql: str, parsed: ParsedCopy, session: Any):
        self.sql = sql
        self.param_oids: list[int | None] = []
        self.parsed = parsed
        self._session = session

    @property
    def direction(self) -> str:
        return self.parsed.direction

    @property
    def options(self) -> CopyOptions:
        return self.parsed.options

    @property
    def column_count(self) -> int:
        """What CopyInResponse/CopyOutResponse declare. Only an explicit column
        list can say -- pg_mimic has no table definition to count -- and no client
        reads the field for text or CSV, both of which are self-delimiting.
        """
        return len(self.parsed.columns or ())

    async def describe(self) -> list[ResultColumn] | None:
        return None

    def bind(self, params: list[str | None]) -> Portal:
        return CopyPortal(self)

    async def copy_in(self, rows: AsyncIterator[Row]) -> int | None:
        return await self._session.copy_in(self.sql, rows)

    async def copy_out(self) -> AsyncIterator[Row]:
        return await _resolve_row_source(self._session.copy_out(self.sql))

    async def header_names(self) -> list[str]:
        """Column names for a HEADER line on COPY TO.

        From the statement's own column list when it has one, else from
        Session.schema(). There is no third source -- pg_mimic doesn't model the
        table, and a row source carries no names -- so a header it can't name
        honestly is refused rather than invented.
        """
        if self.parsed.columns:
            return list(self.parsed.columns)
        schema_fn = getattr(self._session, "schema", None)
        schema = (await schema_fn()) if schema_fn is not None and self.parsed.table is not None else None
        schema = schema or {}
        # Exact first: a quoted `COPY "People"` names the schema() key as spelled.
        # Only an unquoted name -- already folded to lower case by
        # _unquote_identifier -- falls back to matching a key case-insensitively,
        # which is the direction Postgres folds in.
        columns = schema.get(self.parsed.table)
        if not columns:
            folded = {str(name).lower(): value for name, value in schema.items()}
            columns = folded.get(self.parsed.table or "")
        if columns:
            return [str(name) for name in columns]
        raise PgError(
            FEATURE_NOT_SUPPORTED,
            "COPY TO STDOUT with HEADER needs the column names: list them in the statement "
            '("COPY t (a, b) TO STDOUT ...") or declare the table in Session.schema()',
        )


class CopyPortal(Portal):
    """A bound COPY. Bind still has to produce a Portal, so this is what it
    produces -- but it is never executed like one; Connection recognises it and
    runs the copy sub-protocol instead."""

    def __init__(self, statement: CopyStatement):
        self.statement = statement

    async def execute(self, max_rows: int) -> tuple[list[Row], bool]:
        raise PgError(INTERNAL_ERROR, "a COPY portal is driven by the copy sub-protocol, not Portal.execute()")


def copy_statement(session: Any, sql: str) -> Statement | None:
    """A CopyStatement for `sql`, or None if `sql` isn't a COPY over the protocol.

    Raises instead of returning None when the session has no hook for the
    direction asked for. That refusal has to reach the client *before*
    CopyInResponse goes out: a frontend that has been told to start sending has no
    way to un-send.
    """
    parsed = parse_copy(sql)
    if parsed is None:
        return None
    copying_in = parsed.direction == DIRECTION_IN
    hook = "copy_in" if copying_in else "copy_out"
    if not _implements(session, hook):
        raise PgError(
            FEATURE_NOT_SUPPORTED,
            f"this session does not support COPY {'FROM STDIN' if copying_in else 'TO STDOUT'}: "
            f"override Session.{hook}() to handle it",
        )
    return CopyStatement(sql, parsed, session)


def _implements(session: Any, name: str) -> bool:
    """Whether `session` actually overrides a copy hook.

    Compared against Session's own method rather than probed with hasattr, because
    Session defines both -- that's where their contract is documented -- and the
    answer is needed at Parse time, before any data has moved.
    """
    hook = getattr(session, name, None)
    if hook is None:
        return False
    return getattr(hook, "__func__", hook) is not getattr(Session, name)
