#!/usr/bin/env python
"""Tests for `backstamp.strip` and ``backstamp strip``.

Run directly (no pytest needed)::

    python tests/test_strip.py
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from xml.dom import minidom
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
FIXTURES = ROOT / "tests" / "fixtures"

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,  # noqa: E402
                           NameObject, NumberObject, TextStringObject)

import backstamp  # noqa: E402
from backstamp._cli import main as backstamp_main  # noqa: E402
from backstamp._read import _text  # noqa: E402
from backstamp._strip import NoRecord, _xmp_fields, contains, strip_copy  # noqa: E402
from test_inside import _paper  # noqa: E402


def plot_small(fig_dir, subjids, name="small"):
    fig, ax = plt.subplots(figsize=(2, 2))
    ax.plot([1, 2, 3], [0.55, 0.62, 0.71])
    path = backstamp.save(fig, name, fig_dir=fig_dir,
                          inputs={"subject": subjids, "date": ("20251124", "20251130")})
    plt.close(fig)
    return path


def _saved(fig_dir, name, **kwargs) -> Path:
    fig, ax = plt.subplots(figsize=(2, 2))
    path = backstamp.save(fig, name, fig_dir=fig_dir, **kwargs)
    plt.close(fig)
    return path


def _info(path) -> dict:
    meta = PdfReader(str(path)).metadata or {}
    return {key[1:]: _text(value) for key, value in meta.items()}


def _places(path, out=None) -> list:
    return [place for place, _ in strip_copy(path, out)[1]]


def _packet(description=None, title=None, tool=None, tool_attribute=None) -> str:
    """An XMP packet laid out as Acrobat writes one, holding the fields given."""
    def alt(name, text):
        return "" if text is None else (
            f'<dc:{name}><rdf:Alt><rdf:li xml:lang="x-default">{escape(text)}</rdf:li>'
            f'</rdf:Alt></dc:{name}>')

    attribute = "" if tool_attribute is None else f' xmp:CreatorTool="{escape(tool_attribute)}"'
    element = "" if tool is None else f"<xmp:CreatorTool>{escape(tool)}</xmp:CreatorTool>"
    return ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
            'xmlns:pdf="http://ns.adobe.com/pdf/1.3/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            f'xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"{attribute}>'
            f'{element}<pdf:Producer>Matplotlib</pdf:Producer>'
            f'{alt("description", description)}{alt("title", title)}'
            '<xmpMM:DocumentID>uuid:b0c73e08</xmpMM:DocumentID>'
            '</rdf:Description></rdf:RDF></x:xmpmeta>\n<?xpacket end="w"?>')


def _stream(writer, data: bytes, flate=False, **entries):
    stream = DecodedStreamObject()
    stream.set_data(zlib.compress(data) if flate else data)
    if flate:
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    stream.update({NameObject(k): NameObject(v) for k, v in entries.items()})
    return writer._add_object(stream)


def _rewritten(src: Path, dst: Path, drop=(), xmp=None, flate=False, **info) -> Path:
    """``src`` written anew by pypdf: its info dictionary without ``drop`` and with
    ``info``, and ``xmp`` as its XMP packet, Flate-compressed with ``flate``."""
    reader = PdfReader(str(src))
    writer = PdfWriter(clone_from=reader)
    kept = {k: v for k, v in reader.metadata.items() if k[1:] not in drop}
    writer.metadata = {**kept, **{f"/{k}": v for k, v in info.items()}}
    if xmp is not None:
        writer.root_object[NameObject("/Metadata")] = _stream(
            writer, xmp.encode("utf-8"), flate, **{"/Type": "/Metadata", "/Subtype": "/XML"})
    writer.write(str(dst))
    return dst


def _xmp(path) -> tuple:
    """The XMP packet of ``path`` as text, and ``[(field, texts)]`` of the fields strip
    looks at."""
    stream = PdfReader(str(path)).root_object["/Metadata"].get_object()
    text = stream.get_data().decode("utf-8")
    return text, [(name, texts) for name, texts, _ in _xmp_fields(minidom.parseString(text))]


def _published(paper: Path, dst: Path) -> Path:
    """``paper`` with what a real one has: its own Title, an outline entry and a link."""
    writer = PdfWriter(clone_from=PdfReader(str(paper)))
    writer.add_metadata({"/Title": "Figure provenance in practice"})
    writer.add_outline_item("Results", 1)
    action = DictionaryObject({NameObject("/S"): NameObject("/URI"),
                               NameObject("/URI"): TextStringObject("https://example.org")})
    link = DictionaryObject({NameObject("/Type"): NameObject("/Annot"),
                             NameObject("/Subtype"): NameObject("/Link"),
                             NameObject("/Rect"): ArrayObject([NumberObject(0)] * 4),
                             NameObject("/A"): action})
    writer.pages[0][NameObject("/Annots")] = ArrayObject([writer._add_object(link)])
    writer.write(str(dst))
    return dst


def _error(path, kind=ValueError) -> str:
    """The message of the error `strip` raises for ``path``."""
    try:
        backstamp.strip(path)
    except kind as exc:
        return str(exc)
    raise AssertionError(f"{path}: no {kind.__name__}")


def _run(*argv) -> tuple:
    """``(exit code, stdout, stderr)`` of ``backstamp *argv``, in this process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = backstamp_main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _command(*argv, stdin=None):
    """``python -m backstamp *argv`` in a fresh process."""
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-m", "backstamp", *argv], input=stdin, env=env,
                          capture_output=True, text=True, encoding="utf-8")


# --- figures --------------------------------------------------------------


def test_a_saved_figure_loses_its_record_and_keeps_the_rest():
    with tempfile.TemporaryDirectory() as tmp:
        fig = plot_small(Path(tmp), [40, 41])
        original, before = fig.read_bytes(), _info(fig)
        copy = backstamp.strip(fig)
        assert copy == fig.with_name("small_stripped.pdf")
        assert fig.read_bytes() == original
        assert _info(copy) == {k: before[k] for k in ("Producer", "CreationDate")}
        assert [r.status for r in backstamp.read_all(copy)] == ["unstamped"]
        assert not contains(copy.read_bytes(), before["Subject"][:40])
        pages = [[p.get_contents().get_data() for p in PdfReader(str(f)).pages]
                 for f in (fig, copy)]
        assert pages[0] == pages[1]


def test_title_and_creator_go_only_when_backstamp_wrote_them():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fig = plot_small(root, [40, 41])
        many = plot_small(root, list(range(100, 200)), name="many")
        plain = _saved(root, "plain")
        lab = _saved(root, "lab", provenance={"module": "lab.plots", "version": "0.3.0",
                                              "function": "plot_lab"})
        assert _info(many)["Title"].endswith("...") and _info(lab)["Creator"] == "lab.plots 0.3.0"
        cases = [
            # Illustrator: the file name, and its own Creator
            (fig, {"Title": "small", "Creator": "Adobe Illustrator 30.8 (Macintosh)"},
             ["Subject"]),
            # another Creator beside backstamp's Title: the summary of the inputs gives it away
            (fig, {"Creator": "Some editor"}, ["Subject", "Title"]),
            (many, {"Creator": "Some editor"}, ["Subject", "Title"]),
            # backstamp's Creator: the Title beside it is backstamp's
            (fig, {"Title": "Figure 2"}, ["Subject", "Title", "Creator"]),
            # no inputs: a Title of the name alone, which another program may have set
            (plain, {"Creator": "Some editor"}, ["Subject"]),
            (plain, {}, ["Subject", "Title", "Creator"]),
            (lab, {}, ["Subject", "Title", "Creator"]),
        ]
        for i, (src, info, expected) in enumerate(cases):
            edited = _rewritten(src, root / f"edited-{i}.pdf", **info)
            assert _places(edited) == expected, i
            assert _info(root / f"edited-{i}_stripped.pdf") == {
                k: v for k, v in _info(edited).items() if k not in expected}, i


def test_out_names_the_copy_and_never_the_original():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fig = plot_small(root, [40])
        original = fig.read_bytes()
        out = root / "public" / "fig.pdf"
        out.parent.mkdir()
        out.write_bytes(b"an older copy")
        assert backstamp.strip(fig, out=out) == out
        assert backstamp.read(out).status == "unstamped"
        for same in (fig, str(fig), root / "public" / ".." / "small.pdf"):
            try:
                backstamp.strip(fig, out=same)
            except ValueError as exc:
                assert "the copy would replace the original" in str(exc)
            else:
                raise AssertionError(same)
        assert fig.read_bytes() == original
        assert sorted(p.name for p in root.iterdir()) == ["public", "small.pdf"]
        assert [p.name for p in out.parent.iterdir()] == ["fig.pdf"]


# --- papers and XMP -------------------------------------------------------


def test_a_pdflatex_paper_loses_each_figures_record_and_keeps_its_own():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paper = _published(_paper(root / "draft.pdf"), root / "paper.pdf")
        original = paper.read_bytes()
        copy, removed = strip_copy(paper)
        assert [place for place, _ in removed] == ["a.pdf on page 1", "c.pdf on page 2"]
        assert paper.read_bytes() == original
        assert [(r.where["page"], r.where["file"], r.status, r.title)
                for r in backstamp.read_all(copy)] == [
            (1, "a.pdf", "unstamped", None), (1, "b.pdf", "unstamped", "Grant figure"),
            (2, "panel.pdf", "unstamped", None), (2, "c.pdf", "unstamped", None),
            (2, "a.pdf", "unstamped", None)]
        assert _info(copy) == _info(paper)
        assert _info(copy)["Title"] == "Figure provenance in practice"
        reader = PdfReader(str(copy))
        assert [item.title for item in reader.outline] == ["Results"]
        link = reader.pages[0]["/Annots"][0].get_object()
        assert link["/A"]["/URI"] == "https://example.org"
        assert len(reader.pages) == 2


def test_a_record_in_xmp_goes_with_backstamps_title_and_creator_tool():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fig = plot_small(root, [40, 41])
        info = _info(fig)
        subject, title, creator = info["Subject"], info["Title"], info["Creator"]

        # Acrobat: the record, backstamp's Title and Creator, all copied into XMP
        acrobat = _rewritten(fig, root / "acrobat.pdf", xmp=_packet(subject, title, creator))
        assert _places(acrobat) == ["Subject", "Title", "Creator", "XMP dc:description",
                                    "XMP dc:title", "XMP xmp:CreatorTool"]
        text, fields = _xmp(root / "acrobat_stripped.pdf")
        assert fields == []
        assert text.startswith('<?xpacket begin="﻿"') and text.endswith('<?xpacket end="w"?>')
        assert "<pdf:Producer>Matplotlib</pdf:Producer>" in text and "uuid:b0c73e08" in text

        # Illustrator, compressed, with the record in XMP alone: its title and tool stay
        illustrator = _rewritten(fig, root / "illustrator.pdf", drop=("Subject",),
                                 Title="small", Creator="Adobe Illustrator",
                                 xmp=_packet(subject, "small", "Adobe Illustrator"), flate=True)
        assert backstamp.read(illustrator).status == "unstamped"
        assert _places(illustrator) == ["XMP dc:description"]
        copy = root / "illustrator_stripped.pdf"
        assert _xmp(copy)[1] == [("title", ["small"]), ("CreatorTool", ["Adobe Illustrator"])]
        assert not contains(copy.read_bytes(), subject[:40])

        # the creator tool as an attribute
        attribute = _rewritten(fig, root / "attribute.pdf",
                               xmp=_packet(subject, title, tool_attribute=creator))
        assert _places(attribute)[-1] == "XMP xmp:CreatorTool"
        assert "CreatorTool" not in _xmp(root / "attribute_stripped.pdf")[0]

        # another program's XMP beside a record in Subject: it stays
        other = _rewritten(fig, root / "other.pdf", xmp=_packet(None, "Poster", "Acrobat"))
        assert _places(other) == ["Subject", "Title", "Creator"]
        assert _xmp(root / "other_stripped.pdf")[1] == [("title", ["Poster"]),
                                                        ("CreatorTool", ["Acrobat"])]


# --- what is not stripped -------------------------------------------------


def test_a_pdf_without_a_record_gets_no_copy():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bare = root / "bare.pdf"
        fig = plt.figure()
        fig.savefig(bare)
        plt.close(fig)
        (root / "junk.pdf").write_bytes(b"not a pdf")
        assert _error(bare, NoRecord) == f"{bare}: no backstamp record, so no copy written"
        assert "not a PDF backstamp can read" in _error(root / "junk.pdf")
        assert "no such file" in _error(root / "nope.pdf", FileNotFoundError)
        assert issubclass(NoRecord, ValueError)
        code, out, err = _run("strip", str(bare))
        assert sorted(p.name for p in root.iterdir()) == ["bare.pdf", "junk.pdf"]
    assert code == 0 and out == f"{bare}: no backstamp record, so no copy written\n"
    assert not err


def test_a_deck_is_not_stripped_and_its_figures_are_named():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, stored in (("keynote-slides.key", ["nonascii-9091.pdf", "large-9090.pdf",
                                                     "small-9092.pdf"]),
                             ("powerpoint-slides.pptx", ["image1.emf", "image2.emf",
                                                         "image3.emf"])):
            deck = Path(shutil.copy(FIXTURES / name, root / name))
            lines = _error(deck).splitlines()
            assert lines[0] == (f"{deck}: a deck; backstamp strips PDFs, not decks. These "
                                f"figures in it hold a record:")
            assert [line.split("  ")[1:] for line in lines[1:4]] == [
                [stored[0], "slides 1, 4, 5", "Größe µ sub-Maus-ä,Ratte-ø condition=Reiz → Licht"],
                [stored[1], "slides 2, 4, 5", "large sub-40"],
                [stored[2], "slides 3, 4, 5", "small sub-40,41 20251124-20251130"]]
            assert lines[4] == ("Strip each figure before adding it to the deck, or share the "
                                "deck exported as PDF, which holds no records.")
        (root / "junk.pptx").write_bytes(b"not a zip")
        assert "not a deck backstamp can read" in _error(root / "junk.pptx")
        with zipfile.ZipFile(root / "empty.pptx", "w") as z:
            z.writestr("ppt/presentation.xml", "<p/>")
        assert "a deck without backstamp records" in _error(root / "empty.pptx", NoRecord)
        code, out, err = _run("strip", str(root / "keynote-slides.key"))
        assert sorted(p.name for p in root.iterdir()) == [
            "empty.pptx", "junk.pptx", "keynote-slides.key", "powerpoint-slides.pptx"]
    assert code == 1 and not out
    assert err.startswith(f"backstamp: {root / 'keynote-slides.key'}: a deck;")


def test_record_text_left_in_the_copy_fails_and_says_where():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fig = plot_small(root, [40])
        subject = _info(fig)["Subject"]
        # A program that also copied the record into Keywords.
        keywords = _rewritten(fig, root / "keywords.pdf", Keywords=subject)
        # One that kept it in a compressed stream of its own.
        reader = PdfReader(str(fig))
        writer = PdfWriter(clone_from=reader)
        writer.root_object[NameObject("/Notes")] = _stream(writer, subject.encode(), flate=True)
        hidden = root / "hidden.pdf"
        writer.write(str(hidden))

        for path, where in ((keywords, "the info dictionary (object "), (hidden, "object ")):
            original = path.read_bytes()
            message = _error(path)
            assert message.startswith(f"{path}: record text left in the copy, in {where}"), message
            assert message.endswith("; no copy written") and message.count("object") == 1
            assert path.read_bytes() == original
        code, out, err = _run("strip", str(keywords))
        names = sorted(p.name for p in root.iterdir())
    assert names == ["hidden.pdf", "keywords.pdf", "small.pdf"]
    assert code == 1 and not out and "record text left in the copy" in err


def test_the_search_finds_a_record_however_the_pdf_writes_it():
    text = '{"schema": 1, "note": "a (b) c"}'
    octal = "(" + "".join(f"\\{ord(c):03o}" for c in text) + ")"
    for data in (octal.encode(),
                 f"<{text.encode().hex()}>".encode(),
                 f"<FEFF{text.encode('utf-16-be').hex().upper()}>".encode(),
                 f"<rdf:li>{escape(text, {chr(34): '&quot;'})}</rdf:li>".encode(),
                 b"stream\n" + zlib.compress(octal.encode()) + b"\nendstream"):
        assert contains(data, text), data
    assert not contains(octal.encode(), text + "x")


# --- the command line -----------------------------------------------------


def test_strip_takes_files_folders_and_globs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fig = plot_small(root, [40])
        bare = root / "bare.pdf"
        figure = plt.figure()
        figure.savefig(bare)
        plt.close(figure)
        (root / "talks").mkdir()
        deck = Path(shutil.copy(FIXTURES / "keynote-slides.key", root / "talks" / "talk.key"))
        copy = root / "small_stripped.pdf"

        code, out, err = _run("strip", str(root))
        assert code == 1
        assert out.splitlines() == [
            f"{bare}: no backstamp record, so no copy written",
            f"{fig} -> {copy}  removed: Subject, Title, Creator"]
        assert err.startswith(f"backstamp: {deck}: a deck;")
        assert backstamp.read(copy).status == "unstamped"

        code, out, err = _run("strip", str(root / "*.pdf"))
        assert code == 0 and not err
        assert f"{copy}: no backstamp record, so no copy written" in out.splitlines()
        code, _, err = _run("strip", str(root / "nope.pdf"))
        assert code == 1 and err == f"backstamp: no such file: {root / 'nope.pdf'}\n"


def test_strip_reads_dropped_paths_until_an_empty_line():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "my figures"
        folder.mkdir()
        fig = plot_small(folder, [40], name="accuracy µ")
        done = _command("strip", stdin=str(fig).replace(" ", "\\ ") + "\n\n")
        copy = folder / "accuracy µ_stripped.pdf"
        status = backstamp.read(copy).status
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith(f"{fig} -> {copy}  removed: Subject, Title, Creator\n")
    assert done.stderr.startswith("Drop a figure here or paste its path")
    assert status == "unstamped"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append(test.__name__)
            print(f"  [FAIL] {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [ok]   {test.__name__}")
        plt.close("all")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
