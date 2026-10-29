#!/usr/bin/env python
"""Tests for `backstamp.read_all`: the figures inside decks and pdfLaTeX PDFs.

Run directly (no pytest needed)::

    python tests/test_inside.py
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURES = ROOT / "tests" / "fixtures"

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from pypdf import PdfWriter  # noqa: E402
from pypdf.generic import (ArrayObject, DictionaryObject, NameObject,  # noqa: E402
                           NumberObject, StreamObject, TextStringObject)

import backstamp  # noqa: E402
from backstamp._cli import main as backstamp_main  # noqa: E402
from backstamp._inside import _snappy  # noqa: E402
from backstamp._read import _read_all  # noqa: E402

# The figures on each slide of the test decks: tests/fixtures/README.md.
_BOTH = [(slide, f) for slide in (4, 5) for f in ("nonascii", "large", "small")]
DECKS = {
    "keynote-slides.key": [(1, "nonascii"), (2, "large"), (3, "small")] + _BOTH,
    "keynote-reordered.key": [(1, "small"), (2, "nonascii"), (3, "large")] + _BOTH,
    "powerpoint-slides.pptx": [(1, "nonascii"), (2, "large"), (3, "small")] + _BOTH,
    "powerpoint-reordered.pptx": [(1, "small"), (2, "nonascii"), (3, "large")] + _BOTH,
}


def _by_slide(records) -> list:
    return [(r.where["slide"], r.call["function"][len("plot_"):]) for r in records]


def _run(*argv) -> tuple:
    """``(exit code, stdout, stderr)`` of ``backstamp *argv``, in this process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = backstamp_main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _headings(out: str) -> list:
    """The first line of each block `backstamp show` prints."""
    return [line for line in out.splitlines() if line and not line.startswith(" ")]


def _copy(src: Path, dst: Path, changes: dict) -> Path:
    """The zip ``src`` with members replaced, or dropped where the new value is ``None``."""
    with zipfile.ZipFile(src) as z, zipfile.ZipFile(dst, "w") as out:
        for info in z.infolist():
            data = changes.get(info.filename, z.read(info.filename))
            if data is not None:
                out.writestr(info, data)
    return dst


def _info(metadata: dict) -> dict:
    return {k: metadata[k] for k in ("Title", "Subject", "Creator")}


def _pdftex(path: Path, pages: list, forms: dict) -> Path:
    """A PDF laid out as pdfLaTeX writes one.

    - ``forms``: ``{file name: (info dictionary or None, [names nested in it])}``. Each is
      one form XObject with ``/PTEX.FileName`` and ``/PTEX.InfoDict``, however often it is
      included.
    - ``pages``: the names included on each page.
    """
    writer = PdfWriter()
    refs: dict = {}

    def form(name):
        if name not in refs:
            info, inner = forms[name]
            x = StreamObject()
            x._data = b""
            x.update({NameObject("/Type"): NameObject("/XObject"),
                      NameObject("/Subtype"): NameObject("/Form"),
                      NameObject("/BBox"): ArrayObject([NumberObject(0)] * 4),
                      NameObject("/PTEX.FileName"): TextStringObject(f"./{name}")})
            if info is not None:
                x[NameObject("/PTEX.InfoDict")] = writer._add_object(DictionaryObject(
                    {NameObject(f"/{k}"): TextStringObject(v) for k, v in info.items()}))
            if inner:
                x[NameObject("/Resources")] = _xobjects([form(n) for n in inner])
            refs[name] = writer._add_object(x)
        return refs[name]

    for names in pages:
        page = writer.add_blank_page(100, 100)
        page[NameObject("/Resources")] = _xobjects([form(n) for n in names])
    writer.add_metadata({"/Creator": "TeX", "/Producer": "pdfTeX-1.40.29"})
    writer.write(str(path))
    return path


def _xobjects(refs) -> DictionaryObject:
    return DictionaryObject({NameObject("/XObject"): DictionaryObject(
        {NameObject(f"/Im{i}"): ref for i, ref in enumerate(refs, 1)})})


def _paper(path: Path) -> Path:
    """Page 1: a stamped figure and another program's. Page 2: a panel holding a stamped
    figure, then the first figure again."""
    def plot_a():
        return _info(backstamp.metadata("a", inputs={"subject": [40]}))

    def plot_c():
        return _info(backstamp.metadata("c"))

    return _pdftex(path, [["a.pdf", "b.pdf"], ["panel.pdf", "a.pdf"]],
                   {"a.pdf": (plot_a(), []), "b.pdf": ({"Title": "Grant figure"}, []),
                    "panel.pdf": (None, ["c.pdf"]), "c.pdf": (plot_c(), [])})


# --- decks ----------------------------------------------------------------


def test_each_deck_gives_every_figure_on_its_slide():
    for name, expected in DECKS.items():
        records = backstamp.read_all(FIXTURES / name)
        assert all(r.status == "ok" for r in records), name
        assert all(r.path == str(FIXTURES / name) for r in records), name
        assert _by_slide(records) == expected, name


def test_a_figure_on_several_slides_is_listed_on_each():
    for name, stored in (("keynote-slides.key", "small-9092.pdf"),
                         ("powerpoint-slides.pptx", "image3.emf")):
        deck = FIXTURES / name
        small = [r for r in backstamp.read_all(deck) if r.call["function"] == "plot_small"]
        assert [r.where for r in small] == [{"slide": s, "file": stored} for s in (3, 4, 5)]
        assert len({r.created for r in small}) == 1
        assert repr(small[0]) == f"<Record ok {deck}  slide 3  {stored}>"
        assert str(small[0]).startswith(f"{deck}  slide 3  {stored}\n  title     small sub-40,41")


def test_a_keynote_package_folder_reads_as_the_zip_does():
    expected = DECKS["keynote-slides.key"]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "deck.key"
        with zipfile.ZipFile(FIXTURES / "keynote-slides.key") as z:
            z.extractall(folder)
        assert _by_slide(backstamp.read_all(folder)) == expected
        zipped = shutil.copytree(folder, Path(tmp) / "zipped.key")
        with zipfile.ZipFile(zipped / "Index.zip", "w") as z:
            for iwa in sorted((zipped / "Index").iterdir()):
                z.write(iwa, f"Index/{iwa.name}")
        shutil.rmtree(zipped / "Index")
        assert _by_slide(backstamp.read_all(zipped)) == expected


def test_a_deck_whose_slide_order_cannot_be_read_lists_its_figures():
    with tempfile.TemporaryDirectory() as tmp:
        key = _copy(FIXTURES / "keynote-slides.key", Path(tmp) / "a.key",
                    {"Index/Document.iwa": b"not an index"})
        pptx = _copy(FIXTURES / "powerpoint-slides.pptx", Path(tmp) / "b.pptx",
                     {"ppt/presentation.xml": None})
        for deck, stored in ((key, ["large-9090.pdf", "nonascii-9091.pdf", "small-9092.pdf"]),
                             (pptx, ["image1.emf", "image2.emf", "image3.emf"])):
            records, ordered = _read_all(deck)
            assert not ordered and all(r.status == "ok" for r in records)
            assert [r.where for r in records] == [{"slide": None, "file": f} for f in stored]
        code, out, err = _run("show", str(key))
        assert code == 0 and _headings(out)[0] == f"{key}  large-9090.pdf"
        assert out.count("made by") == 3
        assert f"{key}: could not read the slide order" in err
        code, _, err = _run("show", "--slide", "1", str(pptx))
        assert code == 1 and "no figures on slide 1" in err


def test_read_keeps_one_record_per_file():
    deck = backstamp.read(FIXTURES / "keynote-slides.key")
    assert deck.status == "unreadable" and deck.where is None
    assert "a deck: read_all or backstamp show reads the figures inside" in str(deck)
    with tempfile.TemporaryDirectory() as tmp:
        paper = backstamp.read(_paper(Path(tmp) / "paper.pdf"))
    assert paper.status == "unstamped" and paper.creator == "TeX"


def test_a_damaged_or_empty_deck():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        junk = root / "junk.key"
        junk.write_bytes(b"not a zip")
        assert [(r.status, r.where) for r in backstamp.read_all(junk)] == [("unreadable", None)]
        assert [r.status for r in backstamp.read_all(root / "nope.pptx")] == ["missing"]
        broken = root / "broken.key"
        with zipfile.ZipFile(broken, "w") as z:
            z.writestr("Data/fig-7.pdf", b"%PDF-1.4 cut short")
        [rec] = backstamp.read_all(broken)
        assert (rec.status, rec.where) == ("unreadable", {"slide": None, "file": "fig-7.pdf"})
        empty = root / "empty.pptx"
        with zipfile.ZipFile(empty, "w") as z:
            z.writestr("ppt/presentation.xml", "<p/>")
        assert backstamp.read_all(empty) == []
        code, out, err = _run("show", str(empty))
    assert code == 1 and not out and f"{empty}: no PDF figures inside" in err
    assert "slide order" not in err


def test_snappy_copies_that_overlap_their_output():
    # The literal "ab", then 6 bytes copied from 2 back: the copy reads its own output.
    assert _snappy(bytes([8, 0x04]) + b"ab" + bytes([0x09, 0x02])) == b"abababab"
    # A copy with a 2-byte offset.
    assert _snappy(bytes([6, 0x08]) + b"xyz" + bytes([0x0A, 3, 0])) == b"xyzxyz"
    for bad in (bytes([4, 0x09, 0x02]),                          # copies from before the start
                bytes([9, 0x04]) + b"ab" + bytes([0x09, 0x02])):  # one byte short
        try:
            _snappy(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


# --- pdfLaTeX -------------------------------------------------------------


def test_a_pdflatex_pdf_gives_each_included_figure_by_page():
    with tempfile.TemporaryDirectory() as tmp:
        records = backstamp.read_all(_paper(Path(tmp) / "paper.pdf"))
    assert [(r.where["page"], r.where["file"], r.status) for r in records] == [
        (1, "a.pdf", "ok"), (1, "b.pdf", "unstamped"),
        (2, "panel.pdf", "unstamped"), (2, "c.pdf", "ok"), (2, "a.pdf", "ok")]
    assert records[0].call["function"] == "plot_a" and records[0].inputs == {"subject": [40]}
    assert records[1].title == "Grant figure"
    assert records[3].call["function"] == "plot_c"


def test_a_figure_pdf_gives_its_own_record_alone():
    with tempfile.TemporaryDirectory() as tmp:
        fig, ax = plt.subplots()
        path = backstamp.save(fig, "fig", fig_dir=tmp)
        plt.close(fig)
        [rec] = backstamp.read_all(path)
    assert rec.status == "ok" and rec.where is None and str(rec).startswith(f"{path}\n")


# --- the command line -----------------------------------------------------


def test_show_keeps_the_slides_and_pages_asked_for():
    deck = FIXTURES / "keynote-slides.key"
    code, out, _ = _run("show", "--slide", "3", "--slide", "1", str(deck))
    assert code == 0 and _headings(out) == [f"{deck}  slide 1  nonascii-9091.pdf",
                                            f"{deck}  slide 3  small-9092.pdf"]
    code, out, _ = _run("show", "--json", "--slide", "4", str(deck))
    assert [d["where"] for d in json.loads(out)] == [
        {"slide": 4, "file": f} for f in ("nonascii-9091.pdf", "large-9090.pdf", "small-9092.pdf")]
    code, _, err = _run("show", "--slide", "9", str(deck))
    assert code == 1 and "no figures on slide 9" in err
    with tempfile.TemporaryDirectory() as tmp:
        paper = _paper(Path(tmp) / "paper.pdf")
        fig, ax = plt.subplots()
        figure = backstamp.save(fig, "fig", fig_dir=tmp)
        plt.close(fig)
        code, out, _ = _run("show", "--page", "2", str(paper), str(figure), str(deck))
        assert code == 0 and _headings(out) == [f"{paper}  page 2  panel.pdf",
                                                f"{paper}  page 2  c.pdf",
                                                f"{paper}  page 2  a.pdf"]
        code, out, _ = _run("show", "--json", str(paper))
        assert [d["where"]["page"] for d in json.loads(out)] == [1, 1, 2, 2, 2]
        code, out, _ = _run("show", "--reproduce", "--page", "1", str(paper))
        assert out.startswith(f"# {paper}  page 1  a.pdf\n")
        assert f"# {paper}  page 1  b.pdf: unstamped, no record to reproduce from" in out


def test_ls_lists_the_figures_inside_as_rows():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copy(FIXTURES / "keynote-reordered.key", root / "talk.key")
        shutil.copy(FIXTURES / "powerpoint-slides.pptx", root / "talk.pptx")
        (root / "~$talk.pptx").write_bytes(b"an open deck's lock file")
        with zipfile.ZipFile(FIXTURES / "keynote-slides.key") as z:
            z.extractall(root / "sub" / "package.key")
        _paper(root / "paper.pdf")
        fig, ax = plt.subplots()
        backstamp.save(fig, "fig", fig_dir=root)
        plt.close(fig)

        def ls(*flags):
            code, out, err = _run("ls", str(root), *flags)
            assert code == 0 and not err, err
            return [re.split(r"\s{2,}", line)[0] for line in out.splitlines()[1:]]

        rows = ls()
        assert rows[:6] == ["fig", "paper page 1 a", "paper page 1 b", "paper page 2 panel",
                            "paper page 2 c", "paper page 2 a"]
        assert rows[6:9] == ["talk.key slide 1 small-9092", "talk.key slide 2 nonascii-9091",
                             "talk.key slide 3 large-9090"]
        assert rows[15:18] == ["talk.pptx slide 1 image1", "talk.pptx slide 2 image2",
                               "talk.pptx slide 3 image3"]
        assert rows[24] == "sub/package.key slide 1 nonascii-9091" and len(rows) == 33
        assert ls("--function", "plot_small") == [
            "talk.key slide 1 small-9092", "talk.key slide 4 small-9092",
            "talk.key slide 5 small-9092", "talk.pptx slide 3 image3",
            "talk.pptx slide 4 image3", "talk.pptx slide 5 image3",
            "sub/package.key slide 3 small-9092", "sub/package.key slide 4 small-9092",
            "sub/package.key slide 5 small-9092"]
        assert ls("--unstamped") == ["paper page 1 b", "paper page 2 panel"]
        assert ls("--subject", "40", "--function", "plot_a") == [
            "paper page 1 a", "paper page 2 a"]


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
