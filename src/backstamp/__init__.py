"""Record how a figure was made inside the figure file itself.

- `save`: write a figure as PDF with its record embedded.
- `metadata`: the record as a ``metadata=`` dict, for your own ``savefig`` call.
- `configure`: set ``paths`` and ``redact`` once per process.
- `read_figure_metadata`: read the record back.
- `backstamp.provenance.provenance`: the record alone, for outputs other than figures.
"""
from ._pdf import metadata, read_figure_metadata
from ._save import save
from .provenance import configure

__version__ = "0.1.0.dev0"

__all__ = ["save", "metadata", "configure", "read_figure_metadata", "__version__"]
