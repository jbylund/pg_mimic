"""What a configuration parameter's value may be, and what it means.

`settings_catalog` says which parameters exist and who may set them (#32, #77).
This says whether a *value* is one of them accepts, which is the other half of
`SET`: PostgreSQL 18.4 answers `SET work_mem = 'banana'` with 22023, where
pg_mimic used to answer `SET` (#105).

The catalogue already carries everything the check needs, unread until now --
`vartype` picks the rule, and `unit`, `min_val`, `max_val` and `enumvals`
parameterise it.

`parse` is the primitive and `check` is it with the answer thrown away, because
deciding whether a value is acceptable and working out what it means are the same
job: `SET work_mem = 32` is refused for being 32 *kB*, which is only known once
`'32'` has been read as a number in the parameter's unit.

Values come back in the parameter's own unit, which is the unit `min_val` and
`max_val` are expressed in and the one `pg_settings.setting` reports -- `4MB` of
`work_mem` is 4096, not 4194304. Postgres keeps those two representations apart
and so does this.

`vartype` is not the whole rule. Postgres hangs a check hook off 13 of the 150
user-settable parameters -- `client_encoding`, `datestyle`, `timezone`, the `lc_*`
family, `temp_tablespaces` -- and refuses values this accepts, because deciding
them needs something a mimic does not have: `SET temp_tablespaces = 'banana'` is
42704 there because no such tablespace exists. Those stay accepted here rather
than guessed at. Measured against PostgreSQL 18.4, the other 136 agree.
"""

from __future__ import annotations

from . import settings_catalog
from .errors import INVALID_PARAMETER_VALUE, PgError

#: The width Postgres stores an `integer` parameter in. A value past it is refused
#: for overflowing rather than for being out of range, with its own HINT, even when
#: max_val would have refused it anyway.
_INT32_MAX = 2**31 - 1
_INT32_MIN = -(2**31)

#: Unit suffixes, each in the smallest unit of its family. Case-sensitive, as
#: Postgres has them: `1MB` is memory and `1mb` is not a unit at all.
_MEMORY_UNITS = {"B": 1, "kB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
_TIME_UNITS = {"us": 1, "ms": 1000, "s": 1000**2, "min": 60 * 1000**2, "h": 3600 * 1000**2, "d": 86400 * 1000**2}

#: Spellings of true and false, and the shortest prefix each accepts. Postgres
#: takes any unambiguous prefix -- `tr` is true -- but `on` and `off` need two
#: characters, since `o` cannot say which it meant.
_BOOLS = ((("true", "yes"), 1, True), (("false", "no"), 1, False), (("on",), 2, True), (("off",), 2, False))

#: The same spellings written out, which is all a boolean-ish enum accepts.
_BOOL_WORDS = {"1": True, "0": False, "true": True, "false": False, "yes": True, "no": False, "on": True, "off": False}


#: Where pg_mimic is narrower than Postgres, said in the catalogue's own vocabulary
#: so the ordinary rules apply and the error comes out in the ordinary shape.
#:
#: client_encoding is a promise about bytes this server has already decided: every
#: string it writes goes out as UTF-8 (see pg_mimic.messages). Postgres takes LATIN1
#: happily and converts; taking it here would leave `café` arriving as `cafÃ©`,
#: silently, with the connection carrying on -- and a name that is not an encoding at
#: all kills psycopg outright, which looks it up as a Python codec. So it is an enum
#: of one, and the refusal names the only value there is.
#: `middleware._reject_placeholder_value` guards the same hazard through another door.
_NARROWER_THAN_POSTGRES = {"client_encoding": {"vartype": "enum", "enumvals": ["UTF8"]}}


def check(name: str, value: str) -> None:
    """Raise unless `value` is one parameter `name` accepts."""
    parse(name, value)


def render(name: str, value: bool | int | float | str) -> str:
    """The text Postgres reports for `value` -- what SHOW, current_setting() and
    ParameterStatus all carry.

    The inverse of `parse`, and the reason a setting is stored as a value rather
    than as the text a client happened to type: `SET row_security = 'tr'` reads
    back `on`, and `SET work_mem = '32768kB'` reads back `32MB` (#115).
    """
    entry = settings_catalog.SETTINGS.get(name.lower())
    if entry is None:
        return str(value)
    vartype = entry["vartype"]
    if vartype == "bool":
        return "on" if value else "off"
    if vartype in ("integer", "real"):
        return _render_number(int(value) if vartype == "integer" else float(value), entry)
    return str(value)


def _render_number(value: int | float, entry: dict) -> str:
    """In the largest unit of the parameter's family that divides it exactly.

    Postgres reports 1024 kB of work_mem as `1MB` and 1023 kB as `1023kB`, which is
    one rule rather than two once the value is taken as an absolute quantity: a
    parameter measured in `8kB` reports 100 of them as `800kB`, in the family's unit
    rather than its own. Zero carries no unit at all.
    """
    family = _family(entry.get("unit"))
    if family is None or value == 0:
        return str(value) if isinstance(value, int) else f"{value:g}"
    multiple, base = _split_multiple(entry["unit"])
    absolute = value * multiple * family[base]
    for unit, size in sorted(family.items(), key=lambda item: -item[1]):
        if absolute % size == 0:
            return f"{int(absolute // size)}{unit}"
    # Only a real can miss: 2.5 kB is 2560 B, but 0.5 us is nothing smaller.
    return f"{value:g}{entry['unit']}"


def parse(name: str, value: str) -> bool | int | float | str:
    """What `value` means for parameter `name`, in the parameter's own unit.

    A name the catalogue does not carry is a custom GUC, which holds any text --
    `_check_settable` has already refused an undotted name that does not exist, so
    anything reaching here unknown is one a session invented.
    """
    entry = settings_catalog.SETTINGS.get(name.lower())
    if entry is None:
        return value
    entry = {**entry, **_NARROWER_THAN_POSTGRES.get(name.lower(), {})}
    vartype = entry["vartype"]
    if vartype == "bool":
        return _parse_bool(name, value)
    if vartype == "enum":
        return _parse_enum(name, value, entry)
    if vartype in ("integer", "real"):
        return _parse_number(name, value, entry, integral=vartype == "integer")
    return value  # `string`, which Postgres never refuses


def _invalid(name: str, value: str, hint: str | None = None) -> PgError:
    fields = {"H": hint} if hint is not None else {}
    return PgError(INVALID_PARAMETER_VALUE, f'invalid value for parameter "{name}": "{value}"', **fields)


def _parse_bool(name: str, value: str) -> bool:
    text = value.strip().lower()
    if text in ("1", "0"):
        return text == "1"
    for spellings, shortest, result in _BOOLS:
        if len(text) >= shortest and any(word.startswith(text) for word in spellings):
            return result
    # Its own wording, without the `invalid value` prefix the other types get.
    raise PgError(INVALID_PARAMETER_VALUE, f'parameter "{name}" requires a Boolean value')


def _parse_enum(name: str, value: str, entry: dict) -> str:
    text = value.strip().lower()
    allowed = entry.get("enumvals", ())
    for candidate in allowed:
        if text == candidate.lower():
            return candidate
    # An enum offering on and off also answers to the words for them -- `1` and
    # `true` are `on` for debug_parallel_query -- but only spelled in full, where a
    # bool parameter would have taken `tr`.
    if {"on", "off"} <= set(allowed) and text in _BOOL_WORDS:
        return "on" if _BOOL_WORDS[text] else "off"
    raise _invalid(name, value, hint=f"Available values: {', '.join(allowed)}.")


def _parse_number(name: str, value: str, entry: dict, *, integral: bool) -> int | float:
    text = value.strip()
    number = _in_own_units(name, text, entry)
    result = int(number) if integral else number
    # Checked before the range, and reported differently: Postgres refuses a value
    # past int32 for overflowing even when max_val would have refused it anyway.
    if integral and not _INT32_MIN <= result <= _INT32_MAX:
        raise _invalid(name, text, hint="Value exceeds integer range.")
    _check_range(name, result, entry)
    return result


def _in_own_units(name: str, text: str, entry: dict) -> float:
    """`text` as a number in the parameter's own unit."""
    digits, suffix = _split_suffix(text)
    try:
        number = float(digits)
    except ValueError:
        # No number at all, so nothing to say about units -- Postgres hints about
        # them only when something was written where a unit belongs.
        raise _invalid(name, text) from None
    if not suffix:
        return number

    own = entry.get("unit")
    family = _family(own)
    if family is None or suffix not in family:
        raise _invalid(name, text, hint=None if family is None else _units_hint(family)) from None
    multiple, base = _split_multiple(own)
    return number * family[suffix] / (family[base] * multiple)


def _split_suffix(text: str) -> tuple[str, str]:
    """`4.5x` is a number and a suffix; `banana` is all suffix and no number."""
    end = len(text)
    while end and not (text[end - 1].isdigit() or text[end - 1] == "."):
        end -= 1
    return text[:end].strip(), text[end:].strip()


def _family(unit: str | None) -> dict[str, int] | None:
    """The units a parameter measured in `unit` accepts, or None if it has no unit."""
    if unit is None:
        return None
    base = _split_multiple(unit)[1]
    return _MEMORY_UNITS if base in _MEMORY_UNITS else _TIME_UNITS if base in _TIME_UNITS else None


def _units_hint(family: dict[str, int]) -> str:
    quoted = [f'"{unit}"' for unit in family]
    return f"Valid units for this parameter are {', '.join(quoted[:-1])}, and {quoted[-1]}."


def _split_multiple(unit: str) -> tuple[int, str]:
    """`8kB` is eight kilobytes, so a parameter's unit may carry a multiplier."""
    digits = ""
    for char in unit:
        if not char.isdigit():
            break
        digits += char
    return int(digits or 1), unit[len(digits) :]


def _check_range(name: str, result: int | float, entry: dict) -> None:
    low, high = entry.get("min_val"), entry.get("max_val")
    if low is None or high is None:
        return
    if float(low) <= result <= float(high):
        return
    unit = f" {entry['unit']}" if "unit" in entry else ""
    raise PgError(
        INVALID_PARAMETER_VALUE,
        f'{_shown(result)}{unit} is outside the valid range for parameter "{name}" ({low}{unit} .. {high}{unit})',
    )


def _shown(result: int | float) -> str:
    """As Postgres prints it back. An integer parameter reports the whole number it
    was read as; a real goes through %g, so 2147483648 reads 2.14748e+09."""
    return str(result) if isinstance(result, int) else f"{result:g}"
