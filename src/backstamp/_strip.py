"""`strip`: a copy of a PDF without its backstamp records, for publishing.

- Removes each record, with what backstamp wrote alongside it:
  - ``Subject``, with backstamp's ``Title`` and ``Creator``
  - each pdfLaTeX ``/PTEX.InfoDict`` holding a record
  - XMP ``dc:description`` holding a record, where Acrobat and Illustrator copy it, with
    backstamp's ``dc:title`` and ``xmp:CreatorTool``
- ``Title`` and ``Creator`` are backstamp's when they are what it writes for that record
  (`_our_title`, `_our_creator`). Another program's stay, as does the rest of the PDF.
- The records are removed from the reader's objects, then pypdf writes the copy, cloning
  the document. The copy is kept only when no record is found in it and no record text is
  left anywhere in the file (`contains`).
- Decks are not stripped: PowerPoint wraps each figure in an EMF, and Keynote's index may
  hold checksums of its files.
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import tempfile
import zlib
from pathlib import Path
from xml.dom import minidom

from ._inside import _forms, _resolved, is_deck
from ._pdf import summarize_inputs
from ._read import _included_as, _quiet, _reader, _text, read_all

# The start of a record searched for in the copy: enough to be that record's, and found in
# a copy cut short too.
_PREFIX = 100

_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)
_OBJECT = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", re.S)

# PDF string escapes: octal codes, named ones, a line continuation; and hex strings.
_ESCAPE = re.compile(rb"\\([0-7]{1,3}|[nrtbf()\\]|\r\n|\r|\n)")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
            b"(": b"(", b")": b")", b"\\": b"\\"}
_HEX = re.compile(rb"<([0-9A-Fa-f\s]*)>")

_DC = "http://purl.org/dc/elements/1.1/"
_XMP = "http://ns.adobe.com/xap/1.0/"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XMP_FIELDS = ((_DC, "description"), (_DC, "title"), (_XMP, "CreatorTool"))


class NoRecord(ValueError):
    """The file holds no backstamp record, so no copy is written."""


def strip(path, out=None) -> Path:
    """Write a copy of the PDF at ``path`` without its backstamp records; returns its path.

    - ``out``: ``<name>_stripped.pdf`` next to it by default. A file already there is
      replaced; the original is never changed.
    - Raises ``FileNotFoundError`` for a missing file, and ``ValueError``, writing nothing,
      for a PDF without a record, a deck (the message names its figures that hold one), a
      file pypdf cannot read, or record text left in the copy (the message says where).
    """
    return strip_copy(path, out)[0]


def strip_copy(path, out=None) -> tuple:
    """``(copy, [(place, record text or None)])``: `strip`, with what it removed."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"no such file: {src}")
    if is_deck(src):
        raise _deck(src)
    dst = Path(out) if out is not None else src.with_name(f"{src.stem}_stripped{src.suffix}")
    if dst.exists() and os.path.samefile(src, dst):
        raise ValueError(f"{src}: the copy would replace the original; name another out")

    from pypdf import PdfWriter

    with _quiet():
        try:
            reader = _reader(src)
        except Exception as exc:
            raise ValueError(f"{src}: not a PDF backstamp can read") from exc
        try:
            removed = _remove(reader)
        except Exception as exc:
            raise ValueError(f"{src}: could not remove the record: {exc}") from exc
        if not removed:
            raise NoRecord(f"{src}: no backstamp record, so no copy written")
        buffer = io.BytesIO()
        try:
            PdfWriter(clone_from=reader).write(buffer)
        except Exception as exc:
            raise ValueError(f"{src}: pypdf could not write the copy: {exc}") from exc
    data = buffer.getvalue()

    try:
        left = _found_in(data, [text for _, text in removed if text])
    except Exception as exc:
        raise ValueError(f"{src}: could not check the copy: {exc}") from exc
    fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix=f".{dst.stem}-", dir=dst.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        left += [_place(r) for r in read_all(tmp) if r.status == "ok"]
        if left:
            raise ValueError(f"{src}: record text left in the copy, in "
                             f"{', '.join(dict.fromkeys(left))}; no copy written")
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dst, removed


def _deck(src: Path) -> ValueError:
    """The error `strip` raises for a deck, naming the figures in it that hold records."""
    records = read_all(src)
    if any(r.where is None for r in records):
        return ValueError(f"{src}: not a deck backstamp can read")
    figures: dict = {}
    for r in records:
        if r.status == "ok":
            figures.setdefault(r.where["file"], (r, []))[1].append(r.where["slide"])
    if not figures:
        return NoRecord(f"{src}: a deck without backstamp records, so nothing to strip")
    lines = [f"{src}: a deck; backstamp strips PDFs, not decks. These figures in it hold a "
             f"record:"]
    for file, (r, slides) in figures.items():
        shown = [str(s) for s in slides if s is not None]
        on = f"slide{'s' if len(shown) > 1 else ''} {', '.join(shown)}" if shown else ""
        parts = [file, on, r.title or r.call["function"] or ""]
        lines.append("  " + "  ".join(p for p in parts if p))
    lines.append("Strip each figure before adding it to the deck, or share the deck exported "
                 "as PDF, which holds no records.")
    return ValueError("\n".join(lines))


def _place(rec) -> str:
    """Where `read_all` found ``rec`` in a PDF."""
    w = rec.where
    return f"{w['file']} on page {w['page']}" if w else "Subject"


# --- removing -------------------------------------------------------------


def _remove(reader) -> list:
    """Remove the records from the objects of ``reader``, before it is cloned.

    - Returns ``(place, record text or None)`` for each item removed; ``[]`` for none.
    """
    removed = []
    info = _resolved(reader.trailer.get("/Info"))
    record = None
    if isinstance(info, dict):
        subject = _text(info.get("/Subject"))
        record = _stored(subject)
        if record is not None:
            creator = _our_creator(_text(info.get("/Creator")), record)
            title = _our_title(_text(info.get("/Title")), record, creator)
            keys = ["Subject"] + (["Title"] if title else []) + (["Creator"] if creator else [])
            for key in keys:
                del info[f"/{key}"]
            removed += [(key, subject if key == "Subject" else None) for key in keys]

    for number, page in enumerate(reader.pages, 1):
        for form in _forms(page.get("/Resources"), set()):
            figure = _resolved(form.get("/PTEX.InfoDict"))
            subject = _text(figure.get("/Subject")) if isinstance(figure, dict) else None
            if _stored(subject) is not None:
                del form["/PTEX.InfoDict"]
                name = _included_as(form.get("/PTEX.FileName")) or "a figure"
                removed.append((f"{name} on page {number}", subject))

    return removed + _remove_xmp(reader, record)


def _remove_xmp(reader, record) -> list:
    """Remove a record in XMP ``dc:description``, with backstamp's ``dc:title`` and
    ``xmp:CreatorTool``; as `_remove`.

    - ``record``: the one in ``Subject``, which decides the title and creator tool when
      XMP holds none.
    - XMP that does not parse, or declares a DTD, is left to the check of the copy.
    """
    root = _resolved(reader.trailer.get("/Root"))
    stream = _resolved(root.get("/Metadata")) if isinstance(root, dict) else None
    if not hasattr(stream, "get_data"):
        return []
    try:
        data = stream.get_data()
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            return []
        doc = minidom.parseString(data)
    except Exception:
        return []

    fields = _xmp_fields(doc)
    held = [_stored(t) for name, texts, _ in fields if name == "description" for t in texts]
    record = next((r for r in held if r is not None), record)
    if record is None:
        return []
    tools = [t for name, texts, _ in fields if name == "CreatorTool" for t in texts]
    tool = any(_our_creator(t, record) for t in tools)

    removed = []
    for name, texts, drop in fields:
        if name == "description":
            hits = [t for t in texts if _stored(t) is not None]
        elif name == "title":
            hits = [t for t in texts if _our_title(t, record, tool)]
        else:
            hits = [t for t in texts if _our_creator(t, record)]
        if hits:
            drop()
            prefix = "dc" if name != "CreatorTool" else "xmp"
            removed.append((f"XMP {prefix}:{name}", hits[0] if name == "description" else None))
    if removed:
        # An XMP packet starts with its header, not an XML declaration.
        stream.set_data(b"\n".join(node.toxml("utf-8") for node in doc.childNodes))
    return removed


def _xmp_fields(doc) -> list:
    """``(name, [texts], drop)`` for each ``dc:description``, ``dc:title`` and
    ``xmp:CreatorTool`` in ``doc``, as an element or an attribute; ``drop`` removes it."""
    found = []
    descriptions = doc.getElementsByTagNameNS(_RDF, "Description")
    for ns, name in _XMP_FIELDS:
        for node in doc.getElementsByTagNameNS(ns, name):
            items = node.getElementsByTagNameNS(_RDF, "li")
            texts = [_node_text(li) for li in items] if items else [_node_text(node)]
            found.append((name, texts, lambda n=node: n.parentNode.removeChild(n)))
        for node in descriptions:
            if node.hasAttributeNS(ns, name):
                found.append((name, [node.getAttributeNS(ns, name)],
                              lambda n=node, ns=ns, name=name: n.removeAttributeNS(ns, name)))
    return found


def _node_text(node) -> str:
    return "".join(n.data if n.nodeType in (n.TEXT_NODE, n.CDATA_SECTION_NODE)
                   else _node_text(n) for n in node.childNodes)


def _stored(text) -> dict | None:
    """The record in ``text``: any JSON object, as `read` takes it; else ``None``."""
    try:
        value = json.loads(text or "")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _our_creator(creator, record: dict) -> bool:
    """Whether ``creator`` is what backstamp writes with ``record``: ``<module> <version>``
    from the record, or ``backstamp <version>``."""
    if not creator:
        return False
    module, version = record.get("module"), record.get("version")
    if module and version and creator == f"{module} {version}":
        return True
    return re.fullmatch(r"backstamp \S+", creator) is not None


def _our_title(title, record: dict, creator: bool) -> bool:
    """Whether ``title`` is the one backstamp writes with ``record``.

    - When the ``Creator`` beside it is backstamp's (``creator``), or when it is
      backstamp's summary of the record's inputs, under any name and cut or not.
    - A record without inputs gives a Title of its name alone. With another program's
      ``Creator``, that may be the file name the program put there, and it stays.
    """
    if not title:
        return False
    if creator:
        return True
    inputs = record.get("inputs")
    if not summarize_inputs("", inputs):
        return False
    names = [""] + [title[:i] for i, c in enumerate(title) if c == " "]
    return any(summarize_inputs(name, inputs) == title for name in names)


# --- checking -------------------------------------------------------------


def contains(data: bytes, text: str) -> bool:
    """Whether ``data``, or a Flate stream in it, holds ``text``.

    - As a PDF string writes it, with any escapes or in hex, or as XML text; in UTF-8 or
      UTF-16.
    """
    return _holds(_blobs(data), text)


def _blobs(data: bytes) -> list:
    """``data`` and each Flate stream in it, decompressed; each also with PDF string
    escapes and hex strings decoded."""
    blobs = [data]
    for match in _STREAM.finditer(data):
        try:
            blobs.append(zlib.decompressobj().decompress(match.group(1)))
        except zlib.error:
            pass
    return blobs + [_decoded(blob) for blob in blobs]


def _decoded(blob: bytes) -> bytes:
    """``blob`` with PDF string escapes (``\\042``, ``\\(``) and hex strings (``<7B22>``)
    decoded, wherever they are: only searched, so the rest may turn to noise."""
    def escape(match):
        code = match.group(1)
        if code[:1].isdigit():
            return bytes([int(code, 8) & 0xFF])
        return _ESCAPES.get(code, b"")  # a line continuation

    def hexed(match):
        digits = re.sub(rb"\s", b"", match.group(1))
        return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())

    return _HEX.sub(hexed, _ESCAPE.sub(escape, blob))


def _holds(blobs: list, text: str) -> bool:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    texts = {text, escaped, html.escape(text, quote=False), html.escape(text)}
    forms = [t.encode(codec) for t in texts for codec in ("utf-8", "utf-16-be")]
    return any(form in blob for blob in blobs for form in forms)


def _found_in(data: bytes, texts: list) -> list:
    """Where the PDF ``data`` still holds a record: one of ``texts``, or one backstamp finds.

    - Each object holding the start of a text is named, by what it is where known.
    """
    with _quiet():
        reader = _reader(data)
        found = [place for place, _ in _remove(reader)]
        names = _names(reader)
    starts = [text[:_PREFIX] for text in texts]
    blobs = _blobs(data)
    if not any(_holds(blobs, s) for s in starts):
        return found
    objects = []
    for match in _OBJECT.finditer(data):
        blobs = _blobs(match.group(2))
        if any(_holds(blobs, s) for s in starts):
            number = int(match.group(1))
            objects.append(f"{names[number]} (object {number})" if number in names
                           else f"object {number}")
    return found + (objects or ["the file, outside its objects"])


def _names(reader) -> dict:
    """``{object number: what it is}`` for the info dictionary, the XMP metadata, the pages
    and the form XObjects on them."""
    names: dict = {}

    def name(ref, text):
        if hasattr(ref, "idnum"):
            names.setdefault(ref.idnum, text)

    root = _resolved(reader.trailer.get("/Root"))
    name(reader.trailer.get("/Info"), "the info dictionary")
    if isinstance(root, dict):
        name(root.get("/Metadata"), "the XMP metadata")
    for number, page in enumerate(reader.pages, 1):
        name(getattr(page, "indirect_reference", None), f"page {number}")
        for form in _forms(page.get("/Resources"), set()):
            figure = _included_as(form.get("/PTEX.FileName")) or "a form XObject"
            name(getattr(form, "indirect_reference", None), f"{figure} on page {number}")
    return names


__all__ = ["strip"]
