"""Record how a figure was made inside the figure file itself.

- `save`: write a figure as PDF with its record embedded.
- `metadata`: the record as a ``metadata=`` dict, for your own ``savefig`` call.
- `configure`: set ``paths`` and ``redact`` once per process.
- `read`: read the record back, as a `Record`.
- `backstamp.provenance.provenance`: the record alone, for outputs other than figures.
"""
from ._pdf import metadata
from ._read import Record, read
from ._save import save
from .provenance import configure

__version__ = "0.1.0.dev0"

__all__ = ["save", "metadata", "configure", "read", "Record", "__version__"]
