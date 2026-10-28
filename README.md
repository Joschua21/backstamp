# backstamp

A saved figure says nothing about the code that made it. Weeks later, finding the commit,
the function, the parameters and the data behind a plot means searching old scripts, or
making it again.

backstamp writes that record into the figure file when it is saved, and reads it back.

## Install

```
pip install backstamp
```

Python 3.9 or later. Installs matplotlib and pypdf.

## Save

```python
import backstamp

def plot_accuracy(subjids, dates, window=50, norm=1.0):
    fig, ax = plt.subplots()
    ...
    backstamp.save(fig, "accuracy", fig_dir="figures/", inputs={"subject": subjids, "date": dates})
```

This writes `figures/accuracy.pdf`. Call `save` inside the plotting function: the record
describes the function that calls it.

`save` takes a figure, an axes (what pandas' `.plot` returns) or a seaborn grid (what
`relplot` and `catplot` return).

## What the record holds

- the commit of the repository the calling code lives in, marked `-dirty` when it has
  uncommitted changes, with a hash of the diff; the branch, and the `origin` URL without
  credentials
- the installed version of the calling package, the Python version and the backstamp
  version
- the function that called `save`: its module, file, line, and the functions enclosing it
- every parameter of that function, in full. Data objects are stored as their type and
  shape (`<DataFrame 50x2>`, `<ndarray (3, 4) float64>`), not their values.
- the inputs, as given (below)
- when the figure was made

The record identifies the data when the plotting function takes IDs (subjects, dates,
file names) and loads the data itself. A function handed a DataFrame records only its
shape.

Parameters are recorded with the values they have when `save` runs. A parameter
reassigned inside the function is recorded with its new value.

The record is JSON in the PDF's `Subject` field, with a one-line summary in `Title`, e.g.
`accuracy sub-40,45 20251124-20260203`.

## Inputs

`inputs=` records what data went in. Keys are free-form, and values are stored as given:

```python
inputs={"subject": [40, "mouseA"], "date": ["20261002", "03oct2026"]}
inputs={"date": ("20251124", "20251130")}           # a tuple of two is a range
inputs={"date": "20251124-20251130"}                # so is text
inputs={"subject": {40: ("20251124", "20251130"),   # each subject to its dates
                    41: ("20260101", "20260115")}}
inputs={"subject": {40: {"session": [1, 2]}}}       # or to further keys
inputs=["subjids", "dates"]                         # copy these parameters from the call
```

- A tuple of two values is stored as the range `"start/end"`. Use a list for two separate
  values.
- JSON stores mapping keys as text, so `40` reads back as `"40"`.
- A parameter name the function does not have gives a warning.
- `subject`, `date` and `session` are summarised in the `Title` as `sub-40,45`, the date
  span, and `ses-1,2`. Any other key shows as `key=value`.

## Your own `savefig`

`backstamp.metadata()` returns the record as a `metadata=` dict, for a save you make
yourself:

```python
def plot_accuracy(subjids, dates):
    ...
    fig.savefig("figures/accuracy.pdf", metadata=backstamp.metadata(inputs=["subjids", "dates"]))
    with PdfPages("figures/all.pdf", metadata=backstamp.metadata("all")) as pdf:
        ...
```

Call it inside the plotting function, as with `save`. The first argument heads the
`Title`; it defaults to the function's name.

## Paths and private data

Every parameter ends up in the PDF. Two settings control what that exposes:

| `paths=` | `/Users/me/data/sub-040/trials.parquet` is stored as |
| --- | --- |
| `"name"` (default) | `trials.parquet` |
| `"full"` | the full path |
| `"hide"` | `<path>` |

- A path is a `Path` object, or text starting `/`, `~/`, `C:\` or `\\`. Relative text such
  as `"figures/"` is kept as it is.
- `redact=["token"]` stores the named parameters, and inputs of that name, as
  `<redacted>`.
- `backstamp.configure(paths=..., redact=...)` sets both for the rest of the process.
  `paths=` on a call to `save` or `metadata` overrides it; `redact=` on a call adds to it,
  so a configured name is always redacted.

Other text, such as names in an ordinary string parameter, is stored as it is.

## Record size

A record is at most 30,000 characters. A larger one still saves, with a warning naming the
figure and the size. backstamp drops the data summaries first, then cuts parameters,
largest first, then inputs, and lists what it cut. The code fields are never cut.

## File names

The file is `{name}.pdf`. `filename=` takes a string, or a function of the name and the
inputs:

```python
backstamp.save(fig, "accuracy", fig_dir="figures/", inputs={"subject": [40, 45]},
               filename=lambda name, inputs: f"{name}_sub-{'-'.join(map(str, inputs['subject']))}.pdf")
```

## Read

```python
backstamp.read_figure_metadata("figures/accuracy.pdf")
# {"commit": "a1b2c3d", "function": "plot_accuracy",
#  "inputs": {"subject": [40, 45], "date": [...]},
#  "params": {"subjids": [40, 45], "dates": [...], "window": 50, "norm": 1.0}, ...}
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
