"""Read a figure's provenance record back: `read` returns a `Record`.

- pypdf reads the PDF info dictionary, with its log lines silenced: a file that is not a
  PDF is ``unreadable``, quietly.
- The record is the JSON object in ``Subject``. Any other ``Subject`` is another program's,
  and the figure is ``unstamped``.
"""
from __future__ import annotations

import html
import json
import logging
import re
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .provenance import _MARKERS, is_summary

# The stored fields that have a `Record` attribute of their own.
_MAPPED = frozenset({
    "schema", "created_at", "commit", "branch", "repo", "diff_sha", "version", "python",
    "backstamp", "function", "module", "file", "lineno", "chain", "inputs", "params",
})

_STATUS = {
    "unstamped": "a PDF without a backstamp record",
    "unreadable": "not a PDF backstamp can read",
    "missing": "no such file",
}

# A value cut to fit the record.
_CUT = re.compile(r"<cut, [\d,]+ characters>")

# A remote ``git clone`` can reach: a URL or ``user@host:path``.
_CLONABLE = re.compile(r"[A-Za-z][\w+.-]*://|[\w.-]+@[\w.-]+:")

# Longest value `Record.__str__` prints in full, in characters.
_SHOW = 200


def _call() -> dict:
    return dict.fromkeys(("function", "module", "file", "line", "chain", "params"))


def _versions() -> dict:
    return dict.fromkeys(("package", "python", "backstamp"))


@dataclass(repr=False)
class Record:
    """A figure's provenance record, as `read` returns it.

    - ``status``: ``"ok"``, ``"unstamped"`` (a PDF without a backstamp record),
      ``"unreadable"`` (not a PDF pypdf can open) or ``"missing"``.
    - A field the record does not hold is ``None``. ``commit`` is the bare hash; ``dirty``
      says the code had uncommitted changes, and ``diff_sha`` identifies them.
    - ``call``: ``function``, ``module``, ``file``, ``line``, ``chain``, ``params``.
    - ``versions``: ``package`` (the calling code's), ``python``, ``backstamp``.
    - ``cut``: what was cut to fit the size limit, e.g. ``params.trials``. Those values are
      not in the record.
    - ``extra``: stored fields without an attribute of their own.
    """

    path: str
    status: str
    title: str | None = None
    creator: str | None = None
    schema: int | None = None
    created: str | None = None
    commit: str | None = None
    dirty: bool = False
    diff_sha: str | None = None
    branch: str | None = None
    repo: str | None = None
    call: dict = field(default_factory=_call)
    inputs: dict | None = None
    versions: dict = field(default_factory=_versions)
    cut: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Every field, as JSON-safe data."""
        return asdict(self)

    def __repr__(self) -> str:
        return f"<Record {self.status} {self.path}>"

    def __str__(self) -> str:
        lines = [self.path] + [f"  {label:<9} {text}".rstrip() for label, text in self._rows()]
        if self._shortened():
            lines.append("  (long values shortened; to_dict() and --json hold them in full)")
        return "\n".join(lines)

    def _repr_html_(self) -> str:
        cells = "".join(
            f"<tr><th style='text-align:left;padding-right:1em'>{html.escape(label)}</th>"
            f"<td style='text-align:left'><code>{html.escape(text)}</code></td></tr>"
            for label, text in self._rows())
        return f"<div><b>{html.escape(self.path)}</b><table>{cells}</table></div>"

    def _rows(self) -> list:
        """``(label, text)`` rows for `__str__` and `_repr_html_`."""
        if self.status != "ok":
            rows = [("status", f"{self.status}: {_STATUS.get(self.status, '')}"),
                    ("title", self.title), ("creator", self.creator)]
            return [(label, text) for label, text in rows if text]
        c = self.call
        rows = [("title", self.title)]
        rows += _pairs("inputs", self.inputs)
        where = ":".join(x for x in (c["module"], c["function"]) if x)
        place = ":".join(str(x) for x in (c["file"], c["line"]) if x is not None)
        rows.append(("made by", f"{where}  ({place})" if where and place else where or place))
        if c["chain"] and len(c["chain"]) > 1:
            rows.append(("chain", " <- ".join(map(str, c["chain"]))))
        rows += _pairs("params", c["params"])
        if self.commit:
            parts = [self.commit + ("-dirty" if self.dirty else "")]
            parts += [f"diff {self.diff_sha}"] if self.diff_sha else []
            parts += [f"on {self.branch}"] if self.branch else []
            parts += [self.repo] if self.repo else []
            rows.append(("commit", "  ".join(parts)))
        rows.append(("created", self.created))
        package = (c["module"] or "").split(".")[0]
        names = {"package": package if package and package != "__main__" else "package",
                 "python": "python", "backstamp": "backstamp"}
        rows.append(("versions", ", ".join(f"{names[k]} {self.versions.get(k)}"
                                           for k in names if self.versions.get(k))))
        if self.cut:
            rows.append(("cut", ", ".join(self.cut) + "  (not in the record)"))
        rows += _pairs("extra", self.extra)
        return [(label, text) for label, text in rows if text]

    def _shortened(self) -> bool:
        values = [*(self.inputs or {}).values(), *(self.call["params"] or {}).values(),
                  *self.extra.values()]
        return self.status == "ok" and any(len(_json(v)) > _SHOW for v in values)

    def reproduce(self) -> str:
        """Commands that check out the recorded code and repeat the call, as text.

        - Lists first what keeps them from rebuilding the figure exactly: uncommitted
          changes, no repo or commit, a script run directly, placeholders and cut values.
        """
        if self.status != "ok":
            return f"# {self.path}: {self.status}, no record to reproduce from"
        notes: list = []
        shell = self._checkout(notes)
        python = self._repeat(notes)
        lines = [f"# {self.path}"]
        if notes:
            lines += ["# Not an exact rebuild:"] + [f"# - {note}" for note in notes]
        lines += [""] + shell
        if python:
            lines += [""] + python
        return "\n".join(lines)

    def _checkout(self, notes: list) -> list:
        if self.repo and _CLONABLE.match(self.repo):
            folder = re.split(r"[/:]", self.repo.rstrip("/"))[-1]
            folder = folder[:-4] if folder.endswith(".git") else folder
            shell = [f"git clone {self.repo}", f"cd {folder}"]
        else:
            if self.repo:
                notes.append(f"The repository was a local path, stored as {self.repo!r}: "
                             f"clone it yourself.")
            else:
                notes.append(f"The record names no repository: clone the one holding "
                             f"{self.call['file'] or 'the code'} yourself.")
            shell = ["cd <your clone of the repository>"]
        if not self.commit:
            notes.append("The record names no commit: the code was not in a git repository.")
            return shell
        shell.append(f"git checkout {self.commit}")
        if self.dirty:
            diff = f" (diff {self.diff_sha})" if self.diff_sha else ""
            notes.append(f"The code had uncommitted changes{diff}: the checkout is not the "
                         f"code that ran.")
        return shell

    def _repeat(self, notes: list) -> list:
        c = self.call
        function, file = c["function"], c["file"] or "the script"
        if not function:
            notes.append("The record names no function.")
            return []
        if function == "<module>":
            notes.append(f"The figure was saved at the top level of {file}, outside any "
                         f"function: run that file.")
            return []
        if c["module"] in (None, "", "__main__"):
            notes.append(f"{file} ran as a script, and the record keeps its file name, not "
                         f"its folder.")
            lines = [f"# Then, in Python, from the folder holding {file}:", "import runpy", "",
                     f"{function} = runpy.run_path({file!r})[{function!r}]"]
        else:
            lines = ["# Then, in Python, with this checkout installed (e.g. pip install -e .):",
                     f"from {c['module']} import {function}"]
        if self.cut:
            notes.append(f"Cut to fit the size limit, so not in the record: "
                         f"{', '.join(self.cut)}.")
        if c["params"] is None:
            return lines + ["", f"{function}(...)  # the parameters are not in the record"]

        body, held = [], []
        for name, value in c["params"].items():
            if isinstance(value, str) and _CUT.fullmatch(value):
                body.append(f"    {name}=...,  # cut to fit the size limit")
            elif _placeholder(value):
                body.append(f"    {name}=...,  # {value}")
                held.append(f"{name} ({value})")
            else:
                inside = _placeholders_in(value)
                comment = f"  # holds {', '.join(inside)}" if inside else ""
                body.append(f"    {name}={value!r},{comment}")
                held += [f"{name} (holds {', '.join(inside)})"] if inside else []
        if held:
            notes.append(f"The record holds a placeholder, not the value, for "
                         f"{', '.join(held)}: pass the real values.")
        return lines + [""] + ([f"{function}("] + body + [")"] if body else [f"{function}()"])


def _pairs(label: str, values) -> list:
    """``name = value`` rows with the names aligned; the first row carries ``label``."""
    if not isinstance(values, dict) or not values:
        return []
    width = max(len(str(k)) for k in values)
    return [(label if i == 0 else "", f"{str(k):<{width}} = {_brief(v)}")
            for i, (k, v) in enumerate(values.items())]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _brief(value) -> str:
    text = _json(value)
    return text if len(text) <= _SHOW else text[: _SHOW - 3] + "..."


def _placeholder(value) -> bool:
    """Whether ``value`` stands in for one the record does not hold."""
    return isinstance(value, str) and (value in _MARKERS or is_summary(value)
                                       or _CUT.fullmatch(value) is not None)


def _placeholders_in(value) -> list:
    """The placeholders inside a list or dict, once each."""
    found: list = []

    def walk(v):
        if _placeholder(v):
            if v not in found:
                found.append(v)
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    if isinstance(value, (dict, list)):
        walk(value)
    return found


# --- reading --------------------------------------------------------------


def read(path) -> Record:
    """The provenance record of the figure at ``path``; never raises.

    - ``status`` says whether it has one: see `Record`.
    """
    text = str(path)
    try:
        file = Path(path)
        if not file.exists():
            return Record(text, "missing")
        info = _info(file)
    except Exception:
        return Record(text, "unreadable")
    try:
        return _record(text, info)
    except Exception:
        return Record(text, "unstamped", title=info.get("Title"), creator=info.get("Creator"))


def _info(path: Path) -> dict:
    """``Title``, ``Subject`` and ``Creator`` of a PDF; raises when pypdf cannot read it."""
    from pypdf import PdfReader

    logger = logging.getLogger("pypdf")
    level = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            meta = PdfReader(str(path)).metadata or {}
            return {key: _text(meta.get(f"/{key}")) for key in ("Title", "Subject", "Creator")}
    finally:
        logger.setLevel(level)


def _text(value) -> str | None:
    """A PDF string as text, through an indirect reference if it is one."""
    if hasattr(value, "get_object"):
        value = value.get_object()
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return str(value) if isinstance(value, str) else None


def _record(path: str, info: dict) -> Record:
    rec = Record(path, "unstamped", title=info.get("Title"), creator=info.get("Creator"))
    try:
        stored = json.loads(info.get("Subject") or "")
    except ValueError:
        return rec
    if not isinstance(stored, dict):
        return rec

    def get(key, kind):
        value = stored.get(key)
        return value if isinstance(value, kind) and not isinstance(value, bool) else None

    rec.status = "ok"
    rec.schema = get("schema", int)
    rec.created = get("created_at", str)
    commit = get("commit", str)
    if commit and commit.endswith("-dirty"):
        commit, rec.dirty = commit[: -len("-dirty")], True
    rec.commit = commit
    rec.diff_sha, rec.branch, rec.repo = (get(k, str) for k in ("diff_sha", "branch", "repo"))
    rec.call = {"function": get("function", str), "module": get("module", str),
                "file": get("file", str), "line": get("lineno", int),
                "chain": get("chain", list), "params": get("params", dict)}
    rec.versions = {"package": get("version", str), "python": get("python", str),
                    "backstamp": get("backstamp", str)}
    rec.inputs = get("inputs", dict)
    rec.extra = {k: v for k, v in stored.items() if k not in _MAPPED}

    for name, values in (("params", rec.call["params"]), ("inputs", rec.inputs)):
        rec.cut += [f"{name}.{key}" for key, value in (values or {}).items()
                    if isinstance(value, str) and _CUT.fullmatch(value)]
    # Over the size limit, a record keeps the code fields alone.
    if rec.schema is not None and rec.call["function"] and "params" not in stored:
        rec.cut.append("every field but the code")
    return rec


__all__ = ["read", "Record"]
