# backstamp

A saved figure says nothing about the code that made it. Weeks later, finding the commit,
the function, the parameters and the data behind a plot means searching old scripts, or
making it again.

backstamp writes that record into the figure file when it is saved, and reads it back.

## Install

```
pip install backstamp
```

Python 3.9 or later. Installs matplotlib and pypdf, and the `backstamp` command.

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
  uncommitted changes or untracked files, with a hash of the diff to tracked files; the
  branch, and the `origin` URL without credentials
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
largest first, then inputs, and lists what it cut. The code fields are never cut. Reading
the figure back names the cut values too.

## File names

The file is `{name}.pdf`. `filename=` takes a string, or a function of the name and the
inputs:

```python
backstamp.save(fig, "accuracy", fig_dir="figures/", inputs={"subject": [40, 45]},
               filename=lambda name, inputs: f"{name}_sub-{'-'.join(map(str, inputs['subject']))}.pdf")
```

## Read

```
$ backstamp show figures/accuracy.pdf
figures/accuracy.pdf
  title     accuracy sub-40,45 20251124-20260203
  inputs    subject = [40, 45]
            date    = ["20251124", "20260203"]
  made by   lab.plots:plot_accuracy  (plots.py:212)
  chain     plot_accuracy <- main
  params    subjids = [40, 45]
            dates   = ["20251124", "20260203"]
            trials  = "<DataFrame 50x2>"
            window  = 50
            norm    = 1.0
  commit    a1b2c3d-dirty  diff 3f2a9c1e0b7d  on main  https://github.com/me/lab.git
  created   2026-10-05T14:02:11+01:00
  versions  lab 0.3.0, python 3.12.13, backstamp 0.1.0
```

- With no path, `backstamp show` waits for one: drop a figure into the terminal window, or
  paste its path, and press Enter. Several files dropped at once are read together. An
  empty line or Ctrl-D quits.
- It also takes folders, searched with their subfolders, and globs.
- `--json` prints every field. `--reproduce` prints the commands that rebuild the figure.
- `python -m backstamp` runs the same command.

### Find figures

`backstamp ls` lists the figures in a folder and its subfolders, one row each:

```
$ backstamp ls figures/
name            function       commit         inputs                       created
accuracy        plot_accuracy  a1b2c3d-dirty  sub-40,45 20251124-20260203  2026-10-05
sub-40/latency  plot_latency   a1b2c3d        sub-40 20251124-20251130     2026-10-06

$ backstamp ls figures/ --dirty --subject 40
name      function       commit         inputs                       created
accuracy  plot_accuracy  a1b2c3d-dirty  sub-40,45 20251124-20260203  2026-10-05
```

| Filter | Keeps figures |
| --- | --- |
| `--subject ID`, `--date DATE`, `--session ID` | with that value in the input of that name |
| `--input KEY=VALUE` | with `VALUE` in the input `KEY` |
| `--function NAME` | made by `NAME`, or inside a call to `NAME` |
| `--commit SHA` | made at that commit; a prefix is enough |
| `--dirty` | made with uncommitted changes |
| `--unstamped` | without a record |

- A figure must pass every filter given. Each can be repeated.
- Values match as text, inside mappings too: under `subject`, a mapping's keys are subjects
  and its values their dates.
- Whole numbers match by value, so `--subject 40` finds `"040"`.
- A date, `YYYYMMDD` or ISO, finds a stored range that holds it, and can be a range itself:
  `--date 20251101-20251130`. A range in any other format matches its ends exactly.

### Rebuild a figure

```
$ backstamp show --reproduce figures/accuracy.pdf
# figures/accuracy.pdf
# Not an exact rebuild:
# - The code had uncommitted changes (diff 3f2a9c1e0b7d): the checkout is not the code that ran.
# - The record holds a placeholder, not the value, for trials (<DataFrame 50x2>): pass the real values.

git clone https://github.com/me/lab.git
cd lab
git checkout a1b2c3d

# Then, in Python, with this checkout installed (e.g. pip install -e .):
from lab.plots import plot_accuracy

plot_accuracy(
    subjids=[40, 45],
    dates=['20251124', '20260203'],
    trials=...,  # <DataFrame 50x2>
    window=50,
    norm=1.0,
)
```

The commands check out the recorded commit and repeat the call with every parameter. Above
them is what keeps them from rebuilding the figure exactly: uncommitted changes, a
parameter stored as a summary or placeholder, a value cut to fit, a script run directly
rather than imported, or a repository or commit the record does not name.

### In Python

```python
rec = backstamp.read("figures/accuracy.pdf")
rec.status              # "ok"
rec.commit, rec.dirty   # "a1b2c3d", True
rec.call["function"]    # "plot_accuracy"; also module, file, line, chain and params
rec.inputs              # {"subject": [40, 45], "date": ["20251124", "20260203"]}
print(rec)              # the block backstamp show prints
print(rec.reproduce())
rec.to_dict()           # every field, as JSON-safe data
```

`read` never raises. `rec.status` says what it found:

| `status` | The file is |
| --- | --- |
| `"ok"` | a figure with a record |
| `"unstamped"` | a PDF without a record, such as one another program saved |
| `"unreadable"` | not a PDF |
| `"missing"` | not there |

- `commit` is the hash alone. `dirty` says the code had uncommitted changes, and
  `diff_sha` identifies the changes to tracked files. Untracked files alone leave it
  `None`.
- The other fields are `branch`, `repo`, `created`, `title`, and `versions` (`package`,
  `python`, `backstamp`). A field the record does not hold is `None`.
- `cut` names the values cut to fit the size limit, such as `params.trials`.
- `extra` holds any field passed in `save(provenance=...)` without an attribute of its own.
- In Jupyter, a record displays as a table.

## Wrapping `save`

The record names the nearest calling function outside backstamp. A function that wraps
`save` passes its own module, so the record names the plotting function instead of the
wrapper:

```python
backstamp.save(fig, name, fig_dir=out_dir, skip_modules=(__name__,))
```

## Licence

BSD-3-Clause
