"""The record in a PDF's info dictionary: written by `metadata`, read by `read_figure_metadata`.

- matplotlib keeps only the standard info keys (``Title``, ``Subject``, ``Creator``, ...)
  and warns on any other. The record goes into ``Subject`` as JSON, with a one-line
  summary in ``Title``, the field a PDF viewer shows.
- Reading uses pypdf, with a standard-library parser for files pypdf cannot open:
  matplotlib writes the info dictionary uncompressed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .provenance import (
    _options, _warn, capture_call, check_inputs, inputs_record, is_summary,
    provenance as _provenance, resolve_inputs,
)

# Longest record stored, in characters. PDF readers may reject strings over 32,767 bytes.
MAX_BLOB = 30_000

# Longest Title, in characters.
MAX_TITLE = 200

# The fields that identify the code; never cut to fit `MAX_BLOB`.
_CODE_FIELDS = ("schema", "created_at", "commit", "branch", "repo", "diff_sha", "version",
                "python", "backstamp", "function", "module", "file", "lineno", "chain")

# The keys matplotlib accepts. Filtered here, so saving never warns.
_ALLOWED = frozenset({
    "Title", "Author", "Subject", "Keywords", "Creator", "Producer",
    "CreationDate", "ModDate", "Trapped",
})

# The recommended input keys: their Title prefix, and the level a mapping under each maps to.
_SHORT = {"subject": "sub-", "date": "", "session": "ses-"}
_NEXT = {"subject": "date", "date": "session", "session": None}

# A date the Title can sort: YYYYMMDD or ISO.
_DATE = re.compile(r"\d{8}|\d{4}-\d{2}-\d{2}")
_RANGE = re.compile(r"(\d{8})-(\d{8})|(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})|([^/]+)/([^/]+)")


def metadata(name: str | None = None, *, inputs=None, paths=None, redact=None,
             provenance=None, skip_modules=()) -> dict:
    """The record as a ``metadata=`` dict for ``savefig`` or ``PdfPages``.

    Call it inside the plotting function, as `save` is: the record names the function
    that calls it.

    - ``name`` heads the ``Title``; the calling function's name by default.
    - The other arguments are as in `save`.
    """
    paths, redact = _options(paths, redact)
    inputs = check_inputs(inputs)
    try:
        call = capture_call(skip_modules=skip_modules) if provenance is None else None
        resolved = resolve_inputs(inputs, call if provenance is None else provenance)
        record = record_for(call, provenance, resolved, paths, redact)
        name = name or record.get("function", "")
        return build_pdf_metadata(name, record, label=name)
    except Exception:
        return {}


def record_for(call, provenance, inputs, paths, redact) -> dict:
    """``provenance`` with ``inputs`` added, or a record built from ``call``."""
    if provenance is None:
        return _provenance(call=call, inputs=inputs, paths=paths, redact=redact)
    record = dict(provenance)
    if inputs is not None:
        record.setdefault("inputs", inputs_record(inputs, paths, redact))
    return record


def build_pdf_metadata(name: str, record: dict, *, label: str | None = None) -> dict:
    """The ``metadata=`` dict for one figure. ``label`` names it in a size warning."""
    return {
        "Title": summarize_inputs(name, record.get("inputs")),
        "Subject": _dump(record, label or name),
        "Creator": _creator(record),
        "CreationDate": datetime.now(),  # matplotlib takes only a datetime here
    }


def _creator(record: dict) -> str:
    if record.get("module") and record.get("version"):
        return f"{record['module']} {record['version']}"
    from . import __version__
    return f"backstamp {__version__}"


# --- the Title ------------------------------------------------------------


def summarize_inputs(name: str, inputs=None) -> str:
    """The one-line ``Title``, e.g. ``accuracy sub-40,45 20251124-20260203 window=50``.

    - ``subject`` and ``session`` show as ``sub-…`` and ``ses-…``. ``date`` shows as its
      span when every date is ``YYYYMMDD`` or ISO, else as listed.
    - A mapping under one of these adds its values to the next level: subject, then date,
      then session. A dict value adds its own keys.
    - Any other key shows as ``key=value``. Cut to `MAX_TITLE` characters.
    """
    short = {key: [] for key in _SHORT}
    other: dict = {}
    if isinstance(inputs, dict):
        for key, value in inputs.items():
            _collect(str(key), value, short, other)

    parts = [name] if name else []
    for key, prefix in _SHORT.items():
        if short[key]:
            parts.append(_date_span(short[key]) if key == "date"
                         else prefix + ",".join(_unique(short[key])))
    parts += [f"{key}={','.join(_unique(values))}" for key, values in other.items()]
    text = re.sub(r"\s+", " ", " ".join(parts))
    return text if len(text) <= MAX_TITLE else text[: MAX_TITLE - 3] + "..."


def _collect(key, value, short, other):
    if key not in short:
        texts = _texts(value)
        if texts:
            other.setdefault(key, []).extend(texts)
        return
    if not isinstance(value, dict):
        short[key].extend(_texts(value))
        return
    for k, v in value.items():
        short[key].append(str(k))
        if isinstance(v, dict):
            for k2, v2 in v.items():
                _collect(str(k2), v2, short, other)
        elif _NEXT[key]:
            _collect(_NEXT[key], v, short, other)


def _texts(value) -> list:
    if value is None:
        return []
    if isinstance(value, dict):
        return [str(k) for k in value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [t for v in value for t in _texts(v)]
    return [str(value)]


def _unique(items) -> list:
    return list(dict.fromkeys(items))


def _date_span(values) -> str:
    ends = [end for value in values for end in range_ends(value)]
    if ends and all(_DATE.fullmatch(e) for e in ends):
        ends = sorted(set(ends), key=lambda e: e.replace("-", ""))
        return ends[0] if len(ends) == 1 else f"{ends[0]}-{ends[-1]}"
    return ",".join(_unique("-".join(range_ends(value)) for value in values))


def range_ends(text: str) -> list:
    """``[start, end]`` of a range written ``start/end``, or ``YYYYMMDD-YYYYMMDD`` and its
    ISO form; ``[text]`` for anything else."""
    match = _RANGE.fullmatch(text)
    if match is None:
        return [text]
    return [g for g in match.groups() if g is not None]


# --- the record as text ---------------------------------------------------


def _json(value) -> str:
    # ensure_ascii, the default: matplotlib writes a PDF string holding any non-ASCII
    # character as UTF-16, so ASCII keeps the encoding the same for every reader.
    return json.dumps(value, default=str)


def _dump(record: dict, label: str) -> str:
    """The record as ASCII JSON of at most `MAX_BLOB` characters.

    - Over the cap, data summaries are dropped, then parameters cut largest first, then
      inputs. A `UserWarning` names the figure, the size and what was cut.
    - The code fields are never cut.
    """
    blob = _json(record)
    if len(blob) <= MAX_BLOB:
        return blob
    size = len(blob)
    record = dict(record)
    if isinstance(record.get("params"), dict):
        record["params"] = {k: v for k, v in record["params"].items() if not is_summary(v)}
        blob = _json(record)

    cut = []
    for field in ("params", "inputs"):
        if len(blob) <= MAX_BLOB or not isinstance(record.get(field), dict):
            continue
        values = dict(record[field])
        sizes = {key: len(_json(value)) for key, value in values.items()}
        for key in sorted(sizes, key=sizes.get, reverse=True):
            marker = f"<cut, {sizes[key]} characters>"
            if len(_json(marker)) >= sizes[key]:
                break  # the rest are smaller than the marker that would replace them
            values[key] = marker
            record[field] = values
            cut.append(f"{field}[{key!r}]")
            blob = _json(record)
            if len(blob) <= MAX_BLOB:
                break
    if len(blob) > MAX_BLOB:
        record = {k: record[k] for k in _CODE_FIELDS if k in record}
        cut.append("every field but the code")
        blob = _json(record)

    message = (f"backstamp: the record for {label} is {size:,} characters, over the "
               f"{MAX_BLOB:,} limit. Dropped the data summaries (such as <DataFrame 50x2>)")
    if cut:
        message += f" and cut {', '.join(cut)}"
    _warn(message + ". Pass smaller inputs or parameters, or redact= the large ones.")
    return blob


def filter_metadata(metadata: dict) -> dict:
    """Drop keys matplotlib would reject, so saving never warns."""
    return {k: v for k, v in (metadata or {}).items() if k in _ALLOWED}


# --- reading back ---------------------------------------------------------


def read_figure_metadata(path) -> dict:
    """A saved figure's record, plus its ``_title`` and ``_creator``.

    - ``{}`` for a PDF without a record, an unreadable file or a missing one; never raises.
      matplotlib stamps every PDF with its own ``Creator``, so ``{}`` keeps meaning "no
      record here".
    """
    path = Path(path)
    if not path.exists():
        return {}
    info = _read_info_pypdf(path)
    if info is None:
        info = _read_info_stdlib(path)

    subject = (info or {}).get("Subject")
    if not subject:
        return {}

    out: dict = {}
    try:
        parsed = json.loads(subject)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        out.update(parsed)
    else:
        out["_subject"] = subject
    if info.get("Title"):
        out["_title"] = info["Title"]
    if info.get("Creator"):
        out["_creator"] = info["Creator"]
    return out


def _read_info_pypdf(path: Path) -> dict | None:
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        meta = PdfReader(str(path)).metadata or {}
        return {str(k).lstrip("/"): str(v) for k, v in meta.items()}
    except Exception:
        return None


# `/Subject (....)` where the closing paren is not escaped. matplotlib writes the info
# dictionary uncompressed, so this finds it without a PDF parser.
_ENTRY_RE = r"/{key}\s*\((.*?)(?<!\\)\)"


def _read_info_stdlib(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    info = {}
    for key in ("Title", "Subject", "Creator"):
        match = re.search(_ENTRY_RE.format(key=key).encode(), raw, re.S)
        if match:
            info[key] = _unescape(match.group(1))
    return info


_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}


def _unescape(value: bytes) -> str:
    """Undo PDF literal-string escaping and decode.

    - One left-to-right pass: replacing ``\\(`` before ``\\\\`` corrupts a literal
      backslash before a paren.
    - A string holding any non-ASCII character is UTF-16 behind a byte-order mark. The
      record is ASCII, but other tools write such strings.
    """
    if value[:2] in (b"\xfe\xff", b"\xff\xfe"):
        text = value.decode("utf-16", errors="replace")
    else:
        text = value.decode("utf-8", errors="replace")
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), text)


__all__ = [
    "metadata", "build_pdf_metadata", "read_figure_metadata", "summarize_inputs",
    "filter_metadata", "range_ends", "MAX_BLOB", "MAX_TITLE",
]
