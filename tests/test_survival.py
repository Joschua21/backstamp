#!/usr/bin/env python
"""Tests for ``scripts/survival_check.py``.

Run directly (no pytest needed)::

    python tests/test_survival.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import warnings
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.xmp import XmpInformation  # noqa: E402

import survival_check as sc  # noqa: E402
from backstamp._pdf import MAX_BLOB  # noqa: E402


def _original(folder: Path) -> Path:
    return sc.plot_small(folder / "originals", [40, 41], ["20251124"])


def _rewritten(src: Path, dst: Path, drop=(), xmp=None, **info) -> Path:
    """``src`` written anew by pypdf, its info dictionary without ``drop`` and with ``info``."""
    reader = PdfReader(str(src))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    kept = {k: v for k, v in reader.metadata.items() if k not in drop}
    writer.add_metadata({**kept, **{f"/{k}": v for k, v in info.items()}})
    if xmp is not None:
        meta = XmpInformation.create()
        meta.dc_description = {"x-default": xmp}
        writer.xmp_metadata = meta
    dst.parent.mkdir(parents=True, exist_ok=True)
    writer.write(str(dst))
    return dst


def _one(original: Path, returned: Path) -> sc.Checked:
    return sc.check([original], [returned])[0]


def test_a_copy_is_identical_and_intact():
    with tempfile.TemporaryDirectory() as tmp:
        original = _original(Path(tmp))
        copy = Path(tmp) / "returned" / "cp" / "small (1).pdf"
        copy.parent.mkdir(parents=True)
        shutil.copy(original, copy)
        got = _one(original, Path(tmp) / "returned")
    assert got.route == "cp" and got.named and got.identical
    assert [(f.original, f.record) for f in got.findings] == [("small.pdf", "intact")]


def test_a_rewritten_pdf_that_keeps_the_info_dictionary_is_intact():
    with tempfile.TemporaryDirectory() as tmp:
        original = _original(Path(tmp))
        got = _one(original, _rewritten(original, Path(tmp) / "pypdf" / "small.pdf"))
    assert got.identical is False and got.findings[0].record == "intact"


def test_a_dropped_subject_leaves_the_record_partly_kept():
    with tempfile.TemporaryDirectory() as tmp:
        original = _original(Path(tmp))
        got = _one(original, _rewritten(original, Path(tmp) / "x" / "small.pdf",
                                        drop=("/Subject",)))
    found = got.findings[0]
    assert found.record == "partly kept" and found.detail == "Title, Creator kept; Subject gone"


def test_a_cut_subject_says_how_much_is_left():
    with tempfile.TemporaryDirectory() as tmp:
        original = _original(Path(tmp))
        subject = sc._stored(original)[0]["Subject"]
        got = _one(original, _rewritten(original, Path(tmp) / "x" / "small.pdf",
                                        Subject=subject[:100]))
    assert got.findings[0].record == "partly kept"
    assert f"Subject cut to 100 of {len(subject):,} characters" in got.findings[0].detail


def test_a_title_replaced_by_the_file_name_leaves_the_record_readable():
    with tempfile.TemporaryDirectory() as tmp:
        original = _original(Path(tmp))
        got = _one(original, _rewritten(original, Path(tmp) / "x" / "small.pdf",
                                        Title="small", Creator="Illustrator"))
    assert (got.findings[0].record, got.findings[0].detail) == (
        "partly kept", "Subject kept; Title changed; Creator changed; "
                       "the record reads back in full")


def test_a_bare_pdf_has_lost_the_record():
    with tempfile.TemporaryDirectory() as tmp:
        original = _original(Path(tmp))
        bare = Path(tmp) / "x" / "small.pdf"
        bare.parent.mkdir()
        fig, ax = plt.subplots()
        fig.savefig(bare)
        plt.close(fig)
        junk = Path(tmp) / "x" / "small.png"
        junk.write_bytes(b"\x89PNG")
        got, png = sc.check([original], [bare, junk])
    assert got.findings[0].record == "lost"
    assert got.findings[0].detail == "Title gone; Subject gone; Creator changed"
    assert png.findings[0].record == "lost" and "not a PDF" in png.findings[0].detail


def test_a_zip_holding_the_pdf_stores_it_intact():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        original = _original(root)
        rewritten = _rewritten(original, root / "x.pdf")
        deck = root / "returned" / "powerpoint" / "deck.pptx"
        deck.parent.mkdir(parents=True)
        with zipfile.ZipFile(deck, "w") as z:
            z.write(original, "ppt/media/image1.pdf")
        wrapped = root / "returned" / "powerpoint-emf" / "deck.pptx"
        wrapped.parent.mkdir()
        with zipfile.ZipFile(wrapped, "w") as z:
            z.writestr("ppt/media/image1.emf", b"\x01\0\0\0 FDP" + original.read_bytes() + b"\0")
        package = root / "returned" / "keynote" / "deck.key"
        (package / "Data").mkdir(parents=True)
        shutil.copy(rewritten, package / "Data" / "pasted-image-7.pdf")
        empty = root / "returned" / "keynote-empty" / "deck.key"
        empty.parent.mkdir()
        with zipfile.ZipFile(empty, "w") as z:
            z.writestr("Index/Document.iwa", b"")
        results = sc.check([original], [root / "returned"])
        text = sc.report(results)
    by_route = {c.route: c for c in results}
    zipped, folder, none = (by_route[r] for r in ("powerpoint", "keynote", "keynote-empty"))
    assert zipped.container and zipped.findings[0].record == "intact"
    assert zipped.findings[0].stored_as == "ppt/media/image1.pdf"
    emf = by_route["powerpoint-emf"].findings[0]
    assert (emf.record, emf.detail) == ("intact", "byte-identical copy, wrapped")
    assert folder.container
    assert folder.findings[0].stored_as == "Data/pasted-image-7.pdf"
    assert (folder.findings[0].record, folder.findings[0].detail) == ("intact", "rewritten")
    assert none.findings[0].record == "lost" and none.findings[0].detail == "not stored"
    assert ("small.pdf: stored in ppt/media/image1.pdf: record intact (byte-identical copy)"
            in text)


def test_the_record_in_xmp_or_elsewhere_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        original = _original(Path(tmp))
        subject = sc._stored(original)[0]["Subject"]
        got = _one(original, _rewritten(original, Path(tmp) / "x" / "small.pdf",
                                        drop=("/Subject",), xmp=subject))
    assert got.findings[0].notes == ["XMP dc:description holds the record"]
    stream = b"5 0 obj\n<< /Filter /FlateDecode >>\nstream\n" + zlib.compress(
        f"<< /Subject ({subject}) >>".encode()) + b"\nendstream\nendobj\n"
    assert sc.contains(stream, subject) and not sc.contains(stream, subject + "x")
    assert sc.contains(b"(a\\(b\\))", "a(b)")


def test_a_deck_export_is_compared_with_every_original():
    with tempfile.TemporaryDirectory() as tmp:
        original = _original(Path(tmp))
        export = Path(tmp) / "export__deck.pdf"
        fig, ax = plt.subplots()
        fig.savefig(export, metadata={"Keywords": sc._stored(original)[0]["Subject"]})
        plt.close(fig)
        got = _one(original, export)
    assert got.route == "export" and not got.named
    assert [(f.original, f.record) for f in got.findings] == [("any original", "lost")]
    assert got.findings[0].notes == ["the full record of small.pdf is elsewhere in the file"]


def test_a_pasted_subject_is_compared_with_the_original():
    with tempfile.TemporaryDirectory() as tmp:
        original = _original(Path(tmp))
        subject = sc._stored(original)[0]["Subject"]
        full, cut = Path(tmp) / "small.txt", Path(tmp) / "small 2.txt"
        full.write_text(subject + "\n", encoding="utf-8")
        cut.write_text(subject[:50], encoding="utf-8")
        got = sc.check([original], [full, cut])
    assert [f.record for c in got for f in c.findings] == ["intact", "partly kept"]


def test_make_saves_the_test_set():
    with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings():
        warnings.simplefilter("error")
        paths = sc.make(Path(tmp))
        stored = [sc._stored(p)[0] for p in paths]
        records = [sc._record(p) for p in paths]
    assert [p.name for p in paths] == ["small.pdf", "large.pdf", "nonascii.pdf"]
    assert all(r["status"] == "ok" for r in records)
    assert MAX_BLOB - 9 < len(stored[1]["Subject"]) <= MAX_BLOB
    assert len({r["created"] for r in records}) == 3
    assert stored[2]["Title"].startswith("Größe µ sub-Maus-ä,Ratte-ø")
    assert json.loads(stored[2]["Subject"])["inputs"]["condition"] == "Reiz → Licht"


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
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
