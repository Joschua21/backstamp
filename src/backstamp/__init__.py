"""Record how a figure was made inside the figure file itself.

- `save`: write a figure as PDF with its record embedded.
- `metadata.read_figure_metadata`: read the record back.
- `provenance`: the record itself -- commit, version, and the call that made the figure.
"""
from .save import save

__version__ = "0.0.1"

__all__ = ["save", "__version__"]
