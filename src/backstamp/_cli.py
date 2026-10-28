"""The ``backstamp`` command: ``show`` prints figures' records, ``ls`` lists a folder.

- ``backstamp show`` with no path reads paths from the terminal, one line at a time, so
  figures can be dropped into the window.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from ._pdf import _DATE, _NEXT, range_ends, summarize_inputs
from ._read import read

_GLOB = re.compile(r"[*?[]")
_INT = re.compile(r"[0-9]+")

# Widest `ls` columns, in characters.
_COLUMNS = (("name", 48), ("function", 28), ("commit", 13), ("inputs", 36), ("created", 10))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="backstamp", description="Read the record backstamp stores in a figure.")
    commands = parser.add_subparsers(dest="command", metavar="{show,ls}")

    show = commands.add_parser(
        "show", help="print each figure's record",
        description="Print each figure's record. With no path, drop figures into the "
                    "terminal one at a time.")
    show.add_argument("paths", nargs="*", help="figures, folders or globs")
    mode = show.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="every field, as JSON")
    mode.add_argument("--reproduce", action="store_true",
                      help="the commands that rebuild the figure")

    ls = commands.add_parser(
        "ls", help="list the figures in a folder",
        description="One row per figure, searching folders recursively. Filters combine.")
    ls.add_argument("folders", nargs="*", default=["."], help="folders or figures")
    ls.add_argument("--dirty", action="store_true", help="made with uncommitted changes")
    ls.add_argument("--function", metavar="NAME",
                    help="made by NAME, or inside a call to NAME")
    ls.add_argument("--input", metavar="KEY=VALUE", action="append", default=[],
                    help="with VALUE under the input KEY")
    for key, metavar in (("subject", "ID"), ("date", "DATE"), ("session", "ID")):
        ls.add_argument(f"--{key}", metavar=metavar, action="append", default=[],
                        help=f"with {metavar} under the input {key!r}")
    ls.add_argument("--commit", metavar="SHA", help="made at SHA; a prefix is enough")
    ls.add_argument("--unstamped", action="store_true", help="only figures without a record")

    args = parser.parse_args(argv)
    if args.command == "ls" and any("=" not in item for item in args.input):
        parser.error("--input takes KEY=VALUE")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # a title the console cannot encode
        except Exception:
            pass
    try:
        if args.command == "show":
            return _show(args) if args.paths else _drop(args)
        if args.command == "ls":
            return _ls(args)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    parser.print_help()
    return 2


# --- show -----------------------------------------------------------------


def _show(args, paths=None) -> int:
    """Print the records of ``paths``, or of ``args.paths``; 1 if any is missing or unreadable."""
    paths = args.paths if paths is None else paths
    figures = _figures(paths)
    if not figures:
        print(f"backstamp: no PDFs in {' '.join(paths)}", file=sys.stderr)
        return 1
    records = [read(f) for f in figures]
    if args.json:
        data = [r.to_dict() for r in records]
        one = len(paths) == 1 and len(figures) == 1 and Path(paths[0]).is_file()
        print(json.dumps(data[0] if one else data, indent=2, ensure_ascii=False))
    else:
        print("\n\n".join(r.reproduce() if args.reproduce else str(r) for r in records))
    return 1 if any(r.status in ("missing", "unreadable") for r in records) else 0


def _drop(args) -> int:
    """Read figure paths from the terminal until an empty line or end of input."""
    quit_key = "Ctrl-Z, Enter" if os.name == "nt" else "Ctrl-D"
    print(f"Drop a figure here or paste its path, then Enter ({quit_key} to quit):",
          file=sys.stderr)
    while True:
        print("> ", end="", file=sys.stderr, flush=True)
        line = sys.stdin.readline()
        paths = clean_paths(line)
        if not paths:
            print(file=sys.stderr)
            return 0
        _show(args, paths)
        print()


def clean_paths(line: str) -> list:
    """The paths in one dropped or pasted line.

    - Undoes what terminals add: backslash-escaped spaces (Terminal, iTerm), single quotes
      (VS Code), double quotes (Windows Terminal), ``file://`` URLs, a trailing space.
    - Several files dropped at once give several paths. A line naming one existing file is
      that file, spaces and all.
    """
    text = line.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()
    if not text:
        return []
    whole = _unquote(text)
    if os.path.exists(whole):
        return [whole]
    try:
        parts = shlex.split(text, posix=os.name != "nt")
    except ValueError:  # an unclosed quote
        return [whole]
    return [_unquote(p) for p in parts] or [whole]


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    if text.startswith("file://"):
        text = unquote(urlparse(text).path)
        if re.match(r"/[A-Za-z]:", text):  # file:///C:/...
            text = text[1:]
    return text


def _figures(paths) -> list:
    """The PDFs ``paths`` name: files as given, folders searched, globs expanded."""
    found = []
    for arg in paths:
        path = Path(arg)
        matches = sorted(glob.glob(arg, recursive=True)) if (
            not path.exists() and _GLOB.search(arg)) else []
        for item in [Path(m) for m in matches] or [path]:
            found += _pdfs(item) if item.is_dir() else [item]
    return found


def _pdfs(root: Path) -> list:
    """The PDFs under ``root``, sorted; hidden files and folders skipped."""
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        found += [Path(folder) / f for f in sorted(files)
                  if f.lower().endswith(".pdf") and not f.startswith(".")]
    return found


# --- ls -------------------------------------------------------------------


def _ls(args) -> int:
    rows = []
    for arg in args.folders:
        root = Path(arg)
        if not root.exists():
            print(f"backstamp: no such folder: {arg}", file=sys.stderr)
            continue
        for figure in _pdfs(root) if root.is_dir() else [root]:
            rec = read(figure)
            if _keep(rec, args):
                name = figure.relative_to(root) if root.is_dir() else figure
                if root.is_dir() and len(args.folders) > 1:
                    name = root / name
                rows.append((re.sub(r"\.pdf$", "", str(name), flags=re.I), rec))
    if not rows:
        print("backstamp: no figures match", file=sys.stderr)
        return 1
    table = [tuple(name for name, _ in _COLUMNS)] + [_row(name, rec) for name, rec in rows]
    widths = [min(cap, max(len(r[i]) for r in table)) for i, (_, cap) in enumerate(_COLUMNS)]
    for r in table:
        print("  ".join(_fit(cell, w) for cell, w in zip(r, widths)).rstrip())
    return 0


def _row(name: str, rec) -> tuple:
    if rec.status != "ok":
        return (name, f"({rec.status})", "", "", "")
    commit = (rec.commit or "") + ("-dirty" if rec.dirty else "")
    return (name, rec.call["function"] or "", commit, summarize_inputs("", rec.inputs),
            (rec.created or "")[:10])


def _fit(text: str, width: int) -> str:
    return (text if len(text) <= width else text[: width - 3] + "...").ljust(width)


def _keep(rec, args) -> bool:
    """Whether ``rec`` passes every filter in ``args``."""
    if args.unstamped:
        return rec.status == "unstamped"
    wanted = [tuple(item.split("=", 1)) for item in args.input]
    wanted += [(key, value) for key in ("subject", "date", "session")
               for value in getattr(args, key)]
    if rec.status != "ok":
        return not (wanted or args.dirty or args.function or args.commit)
    if args.dirty and not rec.dirty:
        return False
    if args.function and args.function != rec.call["function"] and (
            args.function not in (rec.call["chain"] or ())):
        return False
    if args.commit:
        sha = args.commit[: -len("-dirty")] if args.commit.endswith("-dirty") else args.commit
        if not (rec.commit and (rec.commit.startswith(sha) or sha.startswith(rec.commit))):
            return False
    values = input_values(rec.inputs)
    return all(any(value_matches(v, want) for v in values.get(key, ())) for key, want in wanted)


def input_values(inputs) -> dict:
    """Every value under each input key, as text.

    - A mapping under ``subject``, ``date`` or ``session`` gives its keys to that key and
      its values to the next level, as in the ``Title``: subject, then date, then session.
    - Under any other key, a mapping's keys and values count alike.
    """
    out: dict = {}
    for key, value in (inputs or {}).items():
        _flatten(str(key), value, out)
    return out


def _flatten(key: str, value, out: dict) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            out.setdefault(key, []).append(str(k))
            if key not in _NEXT:
                _flatten(key, v, out)
            elif isinstance(v, dict):
                for k2, v2 in v.items():
                    _flatten(str(k2), v2, out)
            elif _NEXT[key]:
                _flatten(_NEXT[key], v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _flatten(key, v, out)
    elif value is not None:
        out.setdefault(key, []).append(str(value))


def value_matches(stored: str, wanted: str) -> bool:
    """Whether a stored input value matches the one asked for.

    - Text matches exactly. Whole numbers match by value, so ``40`` finds ``"040"``.
    - Dates, ``YYYYMMDD`` or ISO, match when their spans overlap: a date finds a stored
      range holding it, and ``wanted`` may be a range too. Other ranges match an end
      exactly.
    """
    if stored == wanted:
        return True
    if _INT.fullmatch(stored) and _INT.fullmatch(wanted):
        return int(stored) == int(wanted)
    have, want = _span(stored), _span(wanted)
    if have and want:
        return have[0] <= want[1] and want[0] <= have[1]
    return wanted in range_ends(stored)


def _span(text: str):
    """``(first, last)`` of a date or a range of dates, as ``YYYYMMDD``; else ``None``."""
    ends = range_ends(text)
    if not all(_DATE.fullmatch(e) for e in ends):
        return None
    ends = [e.replace("-", "") for e in ends]
    return min(ends), max(ends)
