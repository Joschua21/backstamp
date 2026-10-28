#!/usr/bin/env python
"""Where a figure's record survives: compare files that went through a route with the originals.

    python scripts/survival_check.py --make ~/backstamp-survival/originals
    python scripts/survival_check.py ~/backstamp-survival/originals ~/backstamp-survival/returned

- ``--make`` saves the test set: ``small.pdf``; ``large.pdf``, its record just under the
  size limit; ``nonascii.pdf``, with non-ASCII text in its title and inputs. A remade set
  has new records, so its files go through the routes again.
- A returned file's route is its folder (``slack/small.pdf``), or its name up to ``__``
  (``slack__small.pdf``). Folders are searched with their subfolders, except a folder with
  a suffix, such as a Keynote package: that is one file.
- A file is compared with the original of the same name, ignoring copy suffixes such as
  `` (1)``. A file named after no original, such as a deck, is compared with each.
- Reported for each returned file:
  - whether the bytes are identical
  - the record: intact, partly kept (what was) or lost, from `backstamp.read` and from
    ``Title``, ``Subject`` and ``Creator`` as stored
  - in a zip or a package (``.pptx``, ``.key``): whether each original is stored inside,
    found by its bytes or its record, and the state of that copy's record
  - whether XMP ``dc:description``, or another part of the file, holds the record:
    places `backstamp.read` does not look
- A ``.txt`` file holds a ``Subject`` copied out of a viewer, such as Preview's inspector,
  and is compared with the original's.
"""
from __future__ import annotations

import argparse
import logging
import re
import tempfile
import time
import warnings
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import backstamp  # noqa: E402
from backstamp._pdf import MAX_BLOB  # noqa: E402

FIELDS = ("Title", "Subject", "Creator")

# What download folders and Finder append to a copy's name.
_COPY = re.compile(r"( \(\d+\)| copy( \d+)?| \d+)$")

_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)


# --- the test set ---------------------------------------------------------


def plot_small(fig_dir, subjids, dates, window=50):
    fig, ax = plt.subplots(figsize=(3, 2))
    ax.plot([1, 2, 3, 4], [0.55, 0.62, 0.71, 0.78])
    ax.set_xlabel("Session")
    ax.set_ylabel("Accuracy")
    path = backstamp.save(fig, "small", fig_dir=fig_dir,
                          inputs={"subject": subjids, "date": dates})
    plt.close(fig)
    return path


def plot_large(fig_dir, subjids, trials):
    fig, ax = plt.subplots(figsize=(3, 2))
    ax.hist([t % 97 for t in trials] or [0], bins=20)
    ax.set_xlabel("Trial")
    path = backstamp.save(fig, "large", fig_dir=fig_dir, inputs={"subject": subjids})
    plt.close(fig)
    return path


def plot_nonascii(fig_dir, subjects, condition):
    fig, ax = plt.subplots(figsize=(3, 2))
    ax.bar(subjects, [0.7, 0.4])
    ax.set_title(condition)
    path = backstamp.save(fig, "Größe µ", fig_dir=fig_dir, filename="nonascii.pdf",
                          inputs={"subject": subjects, "condition": condition})
    plt.close(fig)
    return path


def make(folder) -> list:
    """Save the test set into ``folder``; returns the paths."""
    folder = Path(folder).expanduser()
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message="backstamp:")  # large.pdf must fit as is
        paths = [plot_small(folder, [40, 41], ("20251124", "20251130"))]
        time.sleep(1)  # each its own created_at, to the second
        paths.append(_large(folder))
        time.sleep(1)
        paths.append(plot_nonascii(folder, ["Maus-ä", "Ratte-ø"], "Reiz → Licht"))
    return paths


def _large(folder: Path) -> Path:
    """``large.pdf``, its ``Subject`` within 9 characters of `MAX_BLOB`."""
    count = 0
    for _ in range(3):
        # 7-digit trials: 9 characters each in the record, with the ", " between them.
        path = plot_large(folder, [40], list(range(1_000_000, 1_000_000 + count)))
        size = len(_stored(path)[0]["Subject"])
        if MAX_BLOB - 9 < size <= MAX_BLOB:
            return path
        count += (MAX_BLOB - size) // 9
    raise RuntimeError(f"large.pdf: a record of {size:,} characters, not just under {MAX_BLOB:,}")


# --- reading --------------------------------------------------------------


def _stored(path: Path) -> tuple:
    """``Title``, ``Subject`` and ``Creator`` as stored, and XMP ``dc:description``.

    - Raises when pypdf cannot read the file.
    """
    from pypdf import PdfReader

    logger = logging.getLogger("pypdf")
    level = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reader = PdfReader(str(path))
            meta = reader.metadata or {}
            stored = {key: _text(meta.get(f"/{key}")) for key in FIELDS}
            try:
                described = reader.xmp_metadata.dc_description
            except Exception:
                described = None
            return stored, next(iter((described or {}).values()), None)
    finally:
        logger.setLevel(level)


def _text(value) -> str | None:
    if hasattr(value, "get_object"):
        value = value.get_object()
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return str(value) if isinstance(value, str) else None


def _record(path) -> dict:
    """`backstamp.read` as a dict, without the path."""
    record = backstamp.read(path).to_dict()
    del record["path"]
    return record


def contains(data: bytes, text: str) -> bool:
    """Whether ``data``, or a Flate stream in it, holds ``text`` as a PDF string would."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    forms = [t.encode(codec) for t in {text, escaped} for codec in ("utf-8", "utf-16-be")]
    blobs = [data]
    for match in _STREAM.finditer(data):
        try:
            blobs.append(zlib.decompressobj().decompress(match.group(1)))
        except zlib.error:
            pass
    return any(form in blob for blob in blobs for form in forms)


def _members(path: Path) -> list:
    """``(name, bytes)`` of each file in a package folder or a zip."""
    if path.is_dir():
        return [(p.relative_to(path).as_posix(), p.read_bytes())
                for p in sorted(path.rglob("*")) if p.is_file()]
    with zipfile.ZipFile(path) as z:
        return [(i.filename, z.read(i)) for i in z.infolist() if not i.is_dir()]


# --- checking -------------------------------------------------------------


@dataclass
class Original:
    path: Path
    data: bytes
    stored: dict
    record: dict

    @classmethod
    def load(cls, path) -> Original:
        path = Path(path)
        return cls(path, path.read_bytes(), _stored(path)[0], _record(path))


@dataclass
class Finding:
    """What a file holds of one original's record.

    - ``record``: ``"intact"``, ``"partly kept"`` or ``"lost"``.
    - ``matches``: the file holds this original's ``Title``, ``Subject`` or record.
    - ``stored_as``: in a container, the member holding the original.
    """

    original: str
    record: str = "lost"
    detail: str = ""
    matches: bool = False
    stored_as: str | None = None
    notes: list = field(default_factory=list)


@dataclass
class Checked:
    """One returned file; ``identical`` is ``None`` for a container or a ``.txt``."""

    path: Path
    route: str
    named: bool = False
    container: bool = False
    identical: bool | None = None
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def check(originals, returned) -> list:
    """One `Checked` per returned file. Both take files and folders."""
    origs = [Original.load(p) for p in _originals(originals)]
    if not origs:
        raise SystemExit("survival_check: no original PDFs found")
    return [_check(path, route, origs) for path, route in _returned(returned)]


def _originals(paths) -> list:
    found = []
    for path in (Path(p).expanduser() for p in paths):
        found += sorted(path.glob("*.pdf")) if path.is_dir() else [path]
    return found


def _returned(paths) -> list:
    """``(file, route)`` for each returned file; see the module docstring."""
    found = []
    for path in (Path(p).expanduser() for p in paths):
        if path.is_dir() and not path.suffix:
            found += _returned(sorted(p for p in path.iterdir() if not p.name.startswith(".")))
        else:
            route = path.name.split("__")[0] if "__" in path.name else path.parent.name
            found.append((path, route))
    return found


def _name(path: Path) -> str:
    return path.name.split("__", 1)[-1]


def _stem(name: str) -> str:
    return _COPY.sub("", Path(name).stem)


def _check(path: Path, route: str, origs: list) -> Checked:
    named = [o for o in origs if _stem(o.path.name) == _stem(_name(path))]
    checked = Checked(path, route, named=bool(named))
    candidates = named or origs
    if path.is_dir() or zipfile.is_zipfile(path):
        checked.container = True
        _check_container(path, candidates, checked)
        return checked
    if path.suffix == ".txt":
        findings = [_judge_text(o, path) for o in candidates]
    else:
        data = path.read_bytes()
        checked.identical = any(o.data == data for o in candidates)
        findings = [_judge(o, path) for o in candidates]
    if named:
        checked.findings = findings
    else:
        checked.findings = [f for f in findings if f.matches] or [Finding(
            "any original", detail="no original's Title, Subject or record",
            notes=[note for f in findings for note in f.notes])]
    return checked


def _state(now, was, cut=True) -> str:
    """``kept``, ``gone``, ``changed``, or with ``cut``, ``cut to N of M characters``."""
    if now == was:
        return "kept"
    if not now:
        return "gone"
    if cut and was and was.startswith(now):
        return f"cut to {len(now):,} of {len(was):,} characters"
    return "changed"


def _judge(orig: Original, path: Path) -> Finding:
    """``orig``'s record as the PDF at ``path`` holds it."""
    found = Finding(orig.path.name)
    record = _record(path)
    if record["status"] == "unreadable":
        found.detail = "not a PDF pypdf can read"
        return found
    stored, xmp = _stored(path)
    # Only a Subject is cut: a Title replaced by the file name can look like one.
    states = {key: _state(stored[key], orig.stored[key], cut=key == "Subject")
              for key in FIELDS}

    if record == orig.record:
        found.record, found.matches = "intact", True
        if states["Subject"] != "kept":
            found.notes.append("Subject written anew, holding the same record")
    else:
        kept = [key for key in FIELDS if states[key] == "kept"]
        parts = [f"{', '.join(kept)} kept"] if kept else []
        parts += [f"{key} {state}" for key, state in states.items() if state != "kept"]
        if record["status"] == "ok":
            changed = [key for key in orig.record if key not in ("title", "creator")
                       and record[key] != orig.record[key]]
            parts.append(f"record fields changed: {', '.join(changed)}" if changed
                          else "the record reads back in full")
        found.detail = "; ".join(parts)
        same = orig.record["created"] is not None and record["created"] == orig.record["created"]
        found.matches = (same or states["Title"] == "kept" or states["Subject"] == "kept"
                         or states["Subject"].startswith("cut"))
        partly = found.matches or kept or record["status"] == "ok"
        found.record = "partly kept" if partly else "lost"

    subject = orig.stored["Subject"]
    if xmp == subject:
        found.notes.append("XMP dc:description holds the record")
    elif xmp and orig.record["created"] in xmp:
        found.notes.append("XMP dc:description holds part of the record")
    elif found.record != "intact" and states["Subject"] != "kept" and contains(
            path.read_bytes(), subject):
        found.notes.append(f"the full record of {orig.path.name} is elsewhere in the file")
    return found


def _judge_text(orig: Original, path: Path) -> Finding:
    """A ``Subject`` copied out of a viewer, against ``orig``'s."""
    state = _state(path.read_text(encoding="utf-8", errors="replace").strip(),
                   orig.stored["Subject"])
    record = {"kept": "intact", "gone": "lost", "changed": "lost"}.get(state, "partly kept")
    return Finding(orig.path.name, record, detail=f"Subject {state}",
                   matches=record != "lost")


def _check_container(path: Path, origs: list, checked: Checked):
    members = _members(path)
    with tempfile.TemporaryDirectory() as tmp:
        pdfs = []  # (name, file) for each PDF inside, cut out of anything wrapping it
        for i, (name, data) in enumerate(members):
            start, end = data.find(b"%PDF-"), data.rfind(b"%%EOF")
            if start >= 0:
                file = Path(tmp) / f"{i}.pdf"
                file.write_bytes(data[start:end + 5] if end > start else data[start:])
                pdfs.append((name, file))
        claimed = set()
        for orig in origs:
            inside = next(((name, data) for name, data in members if orig.data in data), None)
            if inside:
                name, data = inside
                detail = "byte-identical copy" + ("" if data == orig.data else ", wrapped")
                checked.findings.append(Finding(orig.path.name, "intact", detail,
                                                matches=True, stored_as=name))
                claimed.add(name)
                continue
            judged = [(name, _judge(orig, file)) for name, file in pdfs]
            judged = [(name, f) for name, f in judged if f.matches]
            judged.sort(key=lambda pair: pair[1].record != "intact")
            if judged:
                name, found = judged[0]
                found.stored_as = name
                found.detail = "; ".join(filter(None, ["rewritten", found.detail]))
                claimed.add(name)
            else:
                found = Finding(orig.path.name, detail="not stored")
                found.notes += [f"the full record of {orig.path.name} is in {name}"
                                for name, data in members
                                if contains(data, orig.stored["Subject"])]
            checked.findings.append(found)
    others = [name for name, _ in pdfs if name not in claimed]
    if others:
        checked.notes.append(f"holds {len(others)} PDF(s) matching no original: "
                             f"{', '.join(others)}")


# --- output ---------------------------------------------------------------


def report(results) -> str:
    """The results as text, grouped by route."""
    lines, route = [], None
    for c in results:
        if c.route != route:
            route = c.route
            lines.append(route)
        head = f"  {_name(c.path)}"
        if c.container:
            head += "  container"
        elif c.identical is not None:
            head += f"  bytes {'identical' if c.identical else 'changed'}"
        one = c.named and len(c.findings) == 1 and not c.container
        if not one:
            lines.append(head)
        for f in c.findings:
            text = f"record {f.record}" + (f" ({f.detail})" if f.detail else "")
            if f.stored_as:
                text = f"stored in {f.stored_as}: {text}"
            lines.append(f"{head}  {text}" if one else f"    {f.original}: {text}")
            lines += [f"      {note}" for note in f.notes]
        lines += [f"    {note}" for note in c.notes]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare files that went through a route with the originals.")
    parser.add_argument("--make", metavar="FOLDER", help="save the test set into FOLDER")
    parser.add_argument("original", nargs="?", help="an original PDF, or the test-set folder")
    parser.add_argument("returned", nargs="*", help="returned files, or folders of them")
    args = parser.parse_args(argv)
    if args.make:
        for path in make(args.make):
            rec = backstamp.read(path)
            size = len(_stored(path)[0]["Subject"])
            print(f"{path}  record {size:,} characters, read back: {rec.status}")
            print(f"  title {rec.title}")
        return 0
    if not args.original or not args.returned:
        parser.error("give the originals and at least one returned file or folder")
    print(report(check([args.original], args.returned)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
