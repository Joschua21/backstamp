# backstamp

A saved figure says nothing about the code that made it. Weeks later, finding the commit,
the function and the parameters behind a plot means searching old scripts, or making it
again.

backstamp writes that record into the figure file when it is saved, and reads it back.

## Install

```
pip install backstamp
```

Python 3.9 or later. Installs matplotlib and pypdf.

## Save

```python
import backstamp

def plot_accuracy(trials, window=50):
    fig, ax = plt.subplots()
    ...
    backstamp.save(fig, "accuracy", fig_dir="figures/")
```

This writes `figures/accuracy.pdf` with a record of:

- the commit of the repository the calling code lives in, marked `-dirty` when it has
  uncommitted changes
- the installed version of the calling package
- the function that called `save`: its module, file, line, and the functions enclosing it
- that function's parameters. Data objects are stored as their type and shape
  (`<DataFrame 50x2>`, `<ndarray (3, 4) float64>`), not their values.
- when the figure was made

Parameters are recorded with the values they have when `save` runs, so a parameter
reassigned inside the function is recorded with its new value.

The record is JSON in the PDF's `Subject` field, with a one-line summary in `Title`.

## Read

```python
from backstamp.metadata import read_figure_metadata

read_figure_metadata("figures/accuracy.pdf")
# {"commit": "a1b2c3d", "function": "plot_accuracy",
#  "params": {"trials": "<DataFrame 50x2>", "window": 50}, ...}
```

Returns `{}` for a figure without a record, a file that is not a readable PDF, or a missing
file.

## Wrapping `save`

The record names the nearest calling function outside backstamp. A function that wraps
`save` passes its own module, so the record names the plotting function instead of the
wrapper:

```python
backstamp.save(fig, name, fig_dir=out_dir, skip_modules=(__name__,))
```

## Licence

BSD-3-Clause
