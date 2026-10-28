"""Save a figure as PDF with its provenance record embedded."""
from __future__ import annotations

from pathlib import Path

from ._pdf import build_pdf_metadata, filter_metadata, record_for
from .provenance import _options, capture_call, check_inputs, resolve_inputs


def save(fig, name: str, *, fig_dir, filename=None, inputs=None, paths=None, redact=None,
         dpi: int = 600, bbox_inches=None, provenance=None, metadata=None,
         skip_modules=()) -> Path:
    """Save ``fig`` as a PDF in ``fig_dir``, with a record of how it was made.

    Call it inside the plotting function: the record names the function that calls
    `save`, with every parameter.

    - ``fig``: a Figure, an Axes (what pandas' ``.plot`` returns) or a seaborn grid.
    - ``name`` heads the PDF ``Title``. The file is ``{name}.pdf``, or ``filename``: a
      string, or a callable ``(name, inputs) -> str``. ``fig_dir`` is created if absent.
    - ``inputs``: the data shown. A dict, stored as given, where a ``(start, end)`` tuple
      is a range; ``subject``, ``date`` and ``session`` are summarised in the ``Title``.
      Or a list of parameter names, copied from the call.
    - ``paths``: paths stored by ``"name"`` (default), ``"full"`` or ``"hide"`` (``<path>``).
    - ``redact``: parameter names stored as ``<redacted>``, added to `configure`'s.
    - ``provenance``: a record to embed in place of capturing one.
    - ``skip_modules``: modules the frame walk passes over. A function wrapping `save`
      passes its own, or the record names the wrapper.
    - ``metadata``: extra PDF info-dictionary entries; keys outside the PDF standard set
      are dropped.
    """
    import matplotlib as mpl

    figure = _as_figure(fig)
    if not name:
        raise ValueError("name must be non-empty")
    paths, redact = _options(paths, redact)
    inputs = check_inputs(inputs)

    call = capture_call(skip_modules=skip_modules) if provenance is None else None
    resolved = resolve_inputs(inputs, call if provenance is None else provenance)
    if callable(filename):
        filename = filename(name, dict(resolved or {}))
    out_path = Path(fig_dir) / (filename or f"{name}.pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # The record must never be why a save fails: a broken frame walk or git tree costs
    # the metadata, not the figure.
    try:
        record = record_for(call, provenance, resolved, paths, redact)
        info = build_pdf_metadata(name, record, label=out_path.name)
        info.update(metadata or {})
        info = filter_metadata(info)
    except Exception:
        info = filter_metadata(metadata or {})

    # Type 42 (TrueType) keeps PDF text editable and searchable; matplotlib's default,
    # Type 3, does not, and journals reject it.
    with mpl.rc_context({"pdf.fonttype": 42, "ps.fonttype": 42}):
        figure.savefig(out_path, bbox_inches=bbox_inches if bbox_inches is not None
                       else "tight", dpi=dpi, metadata=info)
    return out_path


def _as_figure(fig):
    """The root `Figure` behind a figure, an axes or a seaborn grid."""
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure, SubFigure

    target = fig
    if isinstance(target, Axes):
        target = target.figure
    elif type(target).__module__.split(".")[0] == "seaborn":
        target = getattr(target, "figure", None) or getattr(target, "fig", None)
    if isinstance(target, SubFigure):
        target = target.figure
    if isinstance(target, Figure):
        return target
    raise TypeError(
        f"save takes a matplotlib Figure, an Axes or a seaborn grid, not "
        f"{type(fig).__name__}. Pass the figure, e.g. ax.figure.")


__all__ = ["save"]
