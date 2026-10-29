"""Figures stored inside other files: Keynote and PowerPoint decks, PDFs compiled with pdfLaTeX.

- Keynote (``.key``, a zip or a package folder): each figure is a PDF under ``Data/``, named
  ``<name>-<data ID>.pdf``. The slide order is in ``Index/*.iwa``, snappy-compressed
  protobuf, decoded here without Apple's schema or a snappy library.
- PowerPoint (``.pptx``): each figure is under ``ppt/media/``, a PDF on its own or wrapped
  in an EMF. The slide order is ``presentation.xml``'s slide list; the slide file names do
  not follow it.
- pdfLaTeX: each included PDF is a form XObject with ``/PTEX.FileName``, holding the PDF's
  info dictionary as ``/PTEX.InfoDict``.
"""
from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import namedtuple
from pathlib import Path

DECKS = (".key", ".pptx")

# Keynote message types: the show, a slide node.
_SHOW, _NODE = 2, 4

_DATA_ID = re.compile(r"-(\d+)\.\w+$")

_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_SLIDE_ID = "{http://schemas.openxmlformats.org/presentationml/2006/main}sldId"

# One message of a Keynote object: its type, the objects and data it references, its bytes.
_Message = namedtuple("_Message", "type refs data payload")


def is_deck(path) -> bool:
    return Path(path).suffix.lower() in DECKS


def deck_figures(path: Path) -> tuple:
    """``([(slide, member, PDF bytes)], ordered)`` of the deck at ``path``.

    - One entry per figure on each slide it is on, in slide order. Figures on no slide come
      last, by name, with ``slide`` ``None``.
    - ``ordered`` is False when the slide order cannot be read; every ``slide`` is then
      ``None``.
    - Raises when ``path`` is neither a zip nor a folder.
    """
    with _Package(path) as package:
        read = _keynote if path.suffix.lower() == ".key" else _powerpoint
        pdfs, placed = read(package)
    ordered = placed is not None
    placed = placed or []
    shown = {member for _, member in placed}
    placed += [(None, member) for member in sorted(pdfs) if member not in shown]
    return [(slide, member, pdfs[member]) for slide, member in placed], ordered


class _Package:
    """The members of a zip, or the files of a package folder, by their names in a zip."""

    def __init__(self, path: Path):
        self._folder = path if path.is_dir() else None
        self._zip = None if self._folder else zipfile.ZipFile(path)
        self.names = (sorted(p.relative_to(path).as_posix() for p in path.rglob("*")
                             if p.is_file())
                      if self._folder else self._zip.namelist())

    def read(self, name: str, size: int = -1) -> bytes:
        if self._folder:
            with open(self._folder / name, "rb") as f:
                return f.read(size)
        with self._zip.open(name) as f:
            return f.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._zip:
            self._zip.close()


# --- Keynote --------------------------------------------------------------


def _keynote(package) -> tuple:
    """``({member: PDF bytes}, [(slide, member)] or None)``."""
    pdfs = {n: package.read(n) for n in package.names
            if n.startswith("Data/") and package.read(n, 5) == b"%PDF-"}
    by_id = {int(m.group(1)): n for n in pdfs for m in [_DATA_ID.search(n)] if m}
    try:
        objects, home = _index(package)
        slides = _slide_order(objects)
    except Exception:
        return pdfs, None
    if slides is None:
        return pdfs, None
    placed = []
    for number, slide in enumerate(slides, 1):
        placed += [(number, by_id[d]) for d in _slide_data(slide, objects, home) if d in by_id]
    return pdfs, placed


def _index(package) -> tuple:
    """``({object ID: [_Message]}, {object ID: its .iwa file})`` of the whole index.

    - A package folder may hold the index zipped, as ``Index.zip``.
    - A damaged ``.iwa`` file is skipped, losing only the objects in it.
    """
    files = [(n, package.read(n)) for n in package.names
             if n.startswith("Index/") and n.endswith(".iwa")]
    if "Index.zip" in package.names:
        with zipfile.ZipFile(io.BytesIO(package.read("Index.zip"))) as z:
            files += [(n, z.read(n)) for n in z.namelist() if n.endswith(".iwa")]
    objects, home = {}, {}
    for name, raw in files:
        try:
            found = _archives(_iwa(raw))
        except Exception:
            continue
        for ident, messages in found.items():
            objects[ident], home[ident] = messages, name
    return objects, home


def _slide_order(objects: dict) -> list | None:
    """The slide object IDs in slide order; ``None`` without a show or slides.

    - The show's slide tree (field 3) lists the slide nodes (field 2). Each node holds its
      slide (field 2) and the nodes nested under it (field 1), which follow it.
    """
    show = next((m for ms in objects.values() for m in ms if m.type == _SHOW), None)
    if show is None:
        return None
    tree = [v for f, v in _fields(show.payload) if f == 3]
    stack = [_ref(v) for t in tree for f, v in _fields(t) if f == 2][::-1]
    slides, seen = [], set()
    while stack:
        node = stack.pop()
        message = next((m for m in objects.get(node, ()) if m.type == _NODE), None)
        if message is None or node in seen:
            continue
        seen.add(node)
        fields = _fields(message.payload)
        slides += [_ref(v) for f, v in fields if f == 2][:1]
        stack += [_ref(v) for f, v in fields if f == 1][::-1]
    return slides or None


def _slide_data(slide: int, objects: dict, home: dict) -> list:
    """The data IDs a slide uses, following references among the objects in its own file.

    - Staying in the file keeps out the images of the template slide, which the slide
      references too.
    """
    file, found, seen, stack = home.get(slide), [], {slide}, [slide]
    while stack:
        for message in objects.get(stack.pop(), ()):
            found += [d for d in message.data if d not in found]
            for ref in reversed(message.refs):
                if ref not in seen and home.get(ref) == file:
                    seen.add(ref)
                    stack.append(ref)
    return found


def _iwa(raw: bytes) -> bytes:
    """An ``.iwa`` file decompressed: chunks of [0, 3-byte little-endian length, snappy block]."""
    out, p = bytearray(), 0
    while p < len(raw):
        if raw[p] != 0:
            raise ValueError(f"unknown .iwa chunk type {raw[p]}")
        n = int.from_bytes(raw[p + 1:p + 4], "little")
        out += _snappy(raw[p + 4:p + 4 + n])
        p += 4 + n
    return bytes(out)


def _snappy(block: bytes) -> bytes:
    """A raw snappy block: the length as a varint, then literals and back-references."""
    size, p = _varint(block, 0)
    out = bytearray()
    while p < len(block):
        tag = block[p]
        p += 1
        kind = tag & 3
        if kind == 0:  # a literal; a length over 60 follows in 1-4 bytes
            n = tag >> 2
            if n >= 60:
                k = n - 59
                n = int.from_bytes(block[p:p + k], "little")
                p += k
            out += block[p:p + n + 1]
            p += n + 1
            continue
        if kind == 1:
            n, offset = ((tag >> 2) & 7) + 4, ((tag >> 5) << 8) | block[p]
            p += 1
        else:
            k = 2 if kind == 2 else 4
            n, offset = (tag >> 2) + 1, int.from_bytes(block[p:p + k], "little")
            p += k
        if not 0 < offset <= len(out):
            raise ValueError("snappy back-reference out of range")
        start = len(out) - offset
        # A copy longer than its offset repeats the last `offset` bytes.
        out += out[start:start + n] if n <= offset else (out[start:] * (n // offset + 1))[:n]
    if len(out) != size:
        raise ValueError("snappy block of the wrong length")
    return bytes(out)


def _archives(data: bytes) -> dict:
    """``{object ID: [_Message]}`` of a decompressed ``.iwa`` file.

    - Each archive: a varint length, ``TSP.ArchiveInfo`` (1 identifier, 2 MessageInfo...),
      then the messages' payloads. MessageInfo: 1 type, 3 payload length, 5 object refs,
      6 data refs.
    """
    p, found = 0, {}
    while p < len(data):
        n, p = _varint(data, p)
        info = _fields(data[p:p + n])
        p += n
        messages = []
        for number, value in info:
            if number != 2:
                continue
            fields = _fields(value)
            size = next(v for f, v in fields if f == 3)
            messages.append(_Message(next(v for f, v in fields if f == 1),
                                     _numbers(fields, 5), _numbers(fields, 6),
                                     data[p:p + size]))
            p += size
        found[next(v for f, v in info if f == 1)] = messages
    return found


def _varint(b: bytes, p: int) -> tuple:
    n = shift = 0
    while True:
        x = b[p]
        p += 1
        n |= (x & 0x7F) << shift
        shift += 7
        if x < 0x80:
            return n, p


def _fields(b: bytes) -> list:
    """``[(field number, value)]`` of a protobuf message, decoded without its schema."""
    p, found = 0, []
    while p < len(b):
        key, p = _varint(b, p)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, p = _varint(b, p)
        elif wire == 2:
            n, p = _varint(b, p)
            value, p = b[p:p + n], p + n
        elif wire == 1:
            value, p = b[p:p + 8], p + 8
        elif wire == 5:
            value, p = b[p:p + 4], p + 4
        else:
            raise ValueError(f"protobuf wire type {wire}")
        found.append((number, value))
    return found


def _numbers(fields: list, number: int) -> list:
    """The values of a repeated integer field, packed or not."""
    out = []
    for f, v in fields:
        if f != number:
            continue
        if isinstance(v, int):
            out.append(v)
            continue
        p = 0
        while p < len(v):
            x, p = _varint(v, p)
            out.append(x)
    return out


def _ref(message: bytes) -> int:
    """The object ID in a ``TSP.Reference``."""
    return next(v for f, v in _fields(message) if f == 1)


# --- PowerPoint -----------------------------------------------------------


def _powerpoint(package) -> tuple:
    """``({member: PDF bytes}, [(slide, member)] or None)``."""
    pdfs = {}
    for name in package.names:
        if "/media/" not in f"/{name}":
            continue
        if package.read(name, 5) == b"%PDF-":
            pdfs[name] = package.read(name)
        elif name.lower().endswith(".emf"):
            data = package.read(name)
            start, end = data.find(b"%PDF-"), data.rfind(b"%%EOF")
            if start >= 0:
                pdfs[name] = data[start:end + 5] if end > start else data[start:]
    try:
        slides = _rels(package, "ppt/presentation.xml")
        root = ET.fromstring(package.read("ppt/presentation.xml"))
        order = [slides[node.get(f"{_R}id")] for node in root.iter(_SLIDE_ID)]
        placed = []
        for number, slide in enumerate(order, 1):
            rels, used = _rels(package, slide), []
            # Relationship IDs in drawing order, as the slide's XML uses them.
            for node in ET.fromstring(package.read(slide)).iter():
                for key, value in node.attrib.items():
                    member = rels.get(value) if key.startswith(_R) else None
                    if member in pdfs and member not in used:
                        used.append(member)
            placed += [(number, member) for member in used]
    except Exception:
        return pdfs, None
    return pdfs, placed if order else None


def _rels(package, part: str) -> dict:
    """``{relationship ID: member}`` of an OOXML part; links outside the package left out."""
    folder, name = posixpath.split(part)
    rels = f"{folder}/_rels/{name}.rels"
    if rels not in package.names:
        return {}
    found = {}
    for node in ET.fromstring(package.read(rels)).iter(_RELS):
        target = node.get("Target", "")
        if node.get("TargetMode") == "External":
            continue
        found[node.get("Id")] = (target.lstrip("/") if target.startswith("/")
                                 else posixpath.normpath(posixpath.join(folder, target)))
    return found


# --- pdfLaTeX -------------------------------------------------------------


def pdftex_figures(reader) -> list:
    """``[(page, /PTEX.FileName, /PTEX.InfoDict)]`` of a PDF compiled with pdfLaTeX, by page.

    - Each form XObject that pdfTeX made from an included PDF, then those nested in it. A
      figure on several pages is listed on each, once per page.
    - ``reader`` is a pypdf ``PdfReader``; the values are pypdf objects, or ``None``.
    """
    return [(number, _resolved(form.get("/PTEX.FileName")),
             _resolved(form.get("/PTEX.InfoDict")))
            for number, page in enumerate(reader.pages, 1)
            for form in _forms(page.get("/Resources"), set())
            if "/PTEX.FileName" in form or "/PTEX.InfoDict" in form]


def _forms(resources, seen: set):
    """The form XObjects in ``resources``, each followed by those nested in it; once each."""
    resources = _resolved(resources)
    xobjects = _resolved(resources.get("/XObject")) if resources else None
    for ref in (xobjects or {}).values():
        key = (ref.idnum, ref.generation) if hasattr(ref, "idnum") else id(ref)
        form = _resolved(ref)
        if key in seen or _resolved(form.get("/Subtype")) != "/Form":
            continue
        seen.add(key)
        yield form
        yield from _forms(form.get("/Resources"), seen)


def _resolved(value):
    """A pypdf object, through an indirect reference if it is one."""
    return value.get_object() if hasattr(value, "get_object") else value
