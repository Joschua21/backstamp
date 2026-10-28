"""Save a figure as PDF with its provenance record embedded."""
from __future__ import annotations

from pathlib import Path

from .metadata import build_pdf_metadata, filter_metadata
from .provenance import provenance as _provenance


def save(fig, name: str, *, fig_dir, filename=None, subjids=None, dates=None,
         dpi: int = 600, bbox_inches=None, provenance=None, metadata=None,
         skip_modules=()) -> Path:
    """Save ``fig`` as ``fig_dir / filename``, with its provenance embedded.

    - ``name`` heads the ``Title`` summary; ``filename`` defaults to ``{name}.pdf``.
    - ``fig_dir`` is created if absent.
    - ``subjids`` / ``dates`` go into the record and the ``Title``.
    - ``provenance``: a `provenance.provenance()` record to embed instead of capturing one.
    - ``skip_modules``: modules the frame walk passes over. A wrapper around `save` must
      pass its own, or the record names the wrapper instead of the plotting function.
    - ``metadata``: extra PDF info-dictionary entries; keys outside the PDF standard set
      are dropped.

    Read it back with `metadata.read_figure_metadata(path)`.
    """
    # Imported here, so `import backstamp.provenance` stays standard library only.
    import matplotlib as mpl

    if fig is None:
        raise ValueError("fig cannot be None")
    if not name:
        raise ValueError("name must be non-empty")

    fig_dir = Path(fig_dir)
    fig_dir.mkdir(parents=True, exist_ok=True)
    out_path = fig_dir / (filename or f"{name}.pdf")

    bbox = bbox_inches if bbox_inches is not None else "tight"

    # Provenance is attached to real work, so it must never be the reason a save fails:
    # a broken frame walk or an unreadable git tree costs the metadata, not the figure.
    try:
        record = provenance if provenance is not None else _provenance(
            skip_modules=skip_modules)
        info = build_pdf_metadata(name, subjids=subjids, dates=dates, provenance=record)
        info.update(metadata or {})
        info = filter_metadata(info)
    except Exception:
        info = filter_metadata(metadata or {})

    # Type 42 (TrueType) keeps PDF text editable/searchable; matplotlib's default of
    # Type 3 does not, and journals reject it. Enforced here, scoped, so a caller that
    # applied no style still gets it.
    with mpl.rc_context({"pdf.fonttype": 42, "ps.fonttype": 42}):
        fig.savefig(out_path, bbox_inches=bbox, dpi=dpi, metadata=info)

    return out_path


__all__ = ["save"]
