"""Record how a figure was made inside the figure file itself.

- `save`: write a figure as PDF with its record embedded.
- `metadata`: the record as a ``metadata=`` dict, for your own ``savefig`` call.
- `configure`: set ``paths`` and ``redact`` once per process.
- `helper`: mark a save helper, so the record names the function that calls it.
- `read`: read the record back, as a `Record`.
- `read_all`: one `Record` per figure, for the figures inside decks and pdfLaTeX PDFs.
- `strip`: a copy of a PDF without its records, for publishing.
- `backstamp.provenance.provenance`: the record alone, for outputs other than figures.
"""
from ._pdf import metadata
from ._read import Record, read, read_all
from ._save import save
from ._strip import strip
from .provenance import configure, helper

__version__ = "0.1.1"

__all__ = ["save", "metadata", "configure", "helper", "read", "read_all", "Record", "strip",
           "__version__"]
