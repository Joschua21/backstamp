"""The provenance record: which code ran, called how, on which data.

`backstamp.save` and `backstamp.metadata` embed it in a figure; `provenance()` returns it
for any other output.

- The commit is anchored on the calling code's file. backstamp is installed as a library,
  so its own file would stamp every figure with the backstamp commit.
- Nothing here raises on a bad environment. A detached HEAD, a missing ``git``, an
  unreadable frame or an unknown object costs a field or becomes a ``<TypeName>``
  placeholder, never the save.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import platform
import re
import subprocess
import warnings
from datetime import date, datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

# The record layout, stored as ``schema``.
SCHEMA = 1

# How many function names `chain` keeps, nearest first. Four reaches past a local
# ``_save_fig`` closure and the analysis function around it.
MAX_CHAIN = 4

# backstamp's own frames, never the answer to "who made this".
_SKIP_MODULES = ("backstamp.provenance", "backstamp._pdf", "backstamp._save")

_GIT_TIMEOUT = 5

PATHS = ("name", "full", "hide")

_config = {"paths": "name", "redact": ()}


# --- settings -------------------------------------------------------------


def configure(*, paths: str | None = None, redact=None) -> None:
    """Set ``paths`` and ``redact`` for every later record in this process.

    - An argument left out keeps its setting.
    - ``paths`` on a `save`, `metadata` or `provenance` call overrides this one. ``redact``
      on a call adds to this list, so a name configured here is always redacted.
    """
    if paths is not None:
        _config["paths"] = _check_paths(paths)
    if redact is not None:
        _config["redact"] = _check_redact(redact)


def _options(paths=None, redact=None) -> tuple:
    """``(paths, redact)`` for one call: `configure`'s, overridden or added to by the call's."""
    redacted = _config["redact"]
    if redact is not None:
        redacted += tuple(n for n in _check_redact(redact) if n not in redacted)
    return (_check_paths(paths) if paths is not None else _config["paths"], redacted)


def _check_paths(paths):
    if paths not in PATHS:
        raise ValueError(f"paths must be one of {', '.join(map(repr, PATHS))}, not {paths!r}")
    return paths


def _check_redact(redact) -> tuple:
    names = (redact,) if isinstance(redact, str) else redact
    if not isinstance(names, (list, tuple, set, frozenset)) or not all(
            isinstance(n, str) for n in names):
        raise TypeError(f"redact takes a list of parameter names, not {redact!r}")
    return tuple(names)


def check_inputs(inputs):
    """``inputs`` as a dict or a tuple of parameter names; anything else raises."""
    if inputs is None or isinstance(inputs, dict):
        return inputs
    names = (inputs,) if isinstance(inputs, str) else inputs
    if isinstance(names, (list, tuple)) and all(isinstance(n, str) for n in names):
        return tuple(names)
    raise TypeError(
        f"inputs takes a dict, or a list of the plotting function's parameter names, "
        f"not {type(inputs).__name__}")


def _warn(message: str) -> None:
    """Warn at the first frame outside backstamp."""
    level, frame = 1, inspect.currentframe()
    while frame is not None and frame.f_globals.get("__name__") in _SKIP_MODULES:
        frame, level = frame.f_back, level + 1
    warnings.warn(message, UserWarning, stacklevel=level)


# --- git ------------------------------------------------------------------


def _git(args, cwd, *, text=True):
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=text,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() if text else out.stdout


def _git_dir(anchor) -> Path | None:
    if anchor is None:
        return None
    anchor = Path(anchor)
    cwd = anchor.parent if anchor.is_file() else anchor
    return cwd if cwd.exists() else None


def git_commit(anchor) -> str | None:
    """Short commit of the repo holding ``anchor``, ending ``-dirty`` when it has changes.

    - Untracked files count as changes: a new module the code imports changes what ran.
    """
    cwd = _git_dir(anchor)
    commit = _git(["rev-parse", "--short", "HEAD"], cwd) if cwd else None
    if not commit:
        return None
    status = _git(["status", "--porcelain", "--untracked-files=normal"], cwd)
    return f"{commit}-dirty" if status else commit


def git_info(anchor) -> dict:
    """``commit``, ``branch``, ``repo`` and, when dirty, ``diff_sha`` of the repo holding ``anchor``.

    - ``diff_sha`` hashes ``git diff HEAD``, so dirty figures can be matched to one
      working-tree state. Untracked files are not in it.
    - ``repo`` is ``origin``'s URL without credentials; ``branch`` is ``None`` on a
      detached HEAD. Outside a repo the dict is empty.
    """
    commit = git_commit(anchor)
    if commit is None:
        return {}
    cwd = _git_dir(anchor)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    info = {
        "commit": commit,
        "branch": None if branch == "HEAD" else branch,
        "repo": _strip_credentials(_git(["remote", "get-url", "origin"], cwd)),
    }
    if commit.endswith("-dirty"):
        diff = _git(["diff", "HEAD", "--no-color", "--no-ext-diff"], cwd, text=False)
        if diff is not None:
            info["diff_sha"] = hashlib.sha256(diff).hexdigest()[:12]
    return info


def _strip_credentials(url: str | None) -> str | None:
    """``https://user:token@host/x`` -> ``https://host/x``. ``git@host:x`` has none."""
    if not url:
        return None
    return re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/]*@", r"\1", url)


def package_version(module_name: str | None) -> str | None:
    """Installed version of the distribution providing ``module_name``'s top package.

    - Found through ``packages_distributions()``, since import and distribution names
      often differ. Python 3.9 lacks it and tries the import name instead.
    """
    if not module_name:
        return None
    root = module_name.split(".")[0]
    try:
        from importlib.metadata import PackageNotFoundError, version
    except Exception:
        return None

    try:
        from importlib.metadata import packages_distributions

        for dist in packages_distributions().get(root, ()):
            try:
                return version(dist)
            except PackageNotFoundError:
                continue
    except ImportError:
        pass  # Python 3.9

    for candidate in (root, root.replace("_", "-")):
        try:
            return version(candidate)
        except Exception:
            continue
    return None


# --- values ---------------------------------------------------------------

# Text read as a path: absolute POSIX, home-relative, a Windows drive, or a UNC share.
_ABSOLUTE = re.compile(r"/|~[/\\]|[A-Za-z]:[/\\]|\\\\")
_WINDOWS = re.compile(r"[A-Za-z]:|\\\\")

# backstamp's own placeholders, as opposed to a summarised object.
_MARKERS = ("<redacted>", "<path>", "<cycle>")


def record_value(value, *, paths: str = "name", ranges: bool = False):
    """One value as JSON-safe data, in full.

    - Numbers, text, booleans, ``None`` and containers of these are kept. numpy scalars
      become plain numbers, dates ISO text and dict keys text; sets are sorted.
    - Paths follow ``paths``: a `Path`, or text starting ``/``, ``~/``, ``C:\\`` or ``\\\\``.
    - Data objects become their type and shape (``<DataFrame 50x2>``), any other object
      ``<TypeName>``.
    - ``ranges``: a tuple of two values becomes the range ``"start/end"``, as in ``inputs``.
    """
    return _value(value, paths, ranges, ())


def _value(value, paths, ranges, seen):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)  # NaN is not valid JSON
    if isinstance(value, str):
        return _path(value, paths) if _ABSOLUTE.match(value) else str(value)
    if isinstance(value, os.PathLike):
        try:
            return _path(os.fspath(value), paths, value)
        except Exception:
            return "<path>"
    if isinstance(value, date):
        return value.isoformat()
    if type(value).__module__ == "numpy" and getattr(value, "ndim", None) == 0:
        try:
            return _value(value.item(), paths, ranges, seen)
        except Exception:
            pass
    described = _describe(value)
    if described is not None:
        return described
    if not isinstance(value, (dict, list, tuple, set, frozenset)):
        return f"<{type(value).__name__}>"

    if id(value) in seen:
        return "<cycle>"
    seen = (*seen, id(value))
    if isinstance(value, dict):
        return {_key(k, paths, ranges): _value(v, paths, ranges, seen)
                for k, v in value.items()}
    if ranges and type(value) is tuple and len(value) == 2:
        ends = [_value(v, paths, False, seen) for v in value]
        if all(isinstance(e, (str, int, float)) and not isinstance(e, bool) for e in ends):
            return f"{ends[0]}/{ends[1]}"
    items = _sorted(value) if isinstance(value, (set, frozenset)) else value
    return [_value(v, paths, ranges, seen) for v in items]


def _key(key, paths, ranges) -> str:
    out = _value(key, paths, ranges, ())
    return out if isinstance(out, str) else json.dumps(out)


def _sorted(items) -> list:
    try:
        return sorted(items)
    except TypeError:
        return sorted(items, key=repr)


def _path(text: str, paths: str, obj=None) -> str:
    if paths == "full":
        return text
    if paths == "hide":
        return "<path>"
    if isinstance(obj, PurePath):
        name = obj.name
    else:
        name = (PureWindowsPath if _WINDOWS.match(text) else PurePosixPath)(text).name
    return name or text


def _describe(value) -> str | None:
    """Type and shape of a data or plotting object, without importing its library."""
    cls = type(value)
    module, name = cls.__module__.split(".")[0], cls.__name__
    try:
        if module == "pandas" and name == "DataFrame":
            return f"<DataFrame {value.shape[0]}x{value.shape[1]}>"
        if module == "pandas" and name == "Series":
            return f"<Series {len(value)} {value.dtype}>"
        if module == "numpy" and name == "ndarray":
            return f"<ndarray {tuple(value.shape)} {value.dtype}>"
        if module == "matplotlib":
            return f"<{name}>"
        shape = getattr(value, "shape", None)
        if isinstance(shape, tuple):
            return f"<{name} {tuple(shape)}>"
    except Exception:
        return f"<{name}>"
    return None


def is_summary(value) -> bool:
    """Whether ``value`` is a ``<TypeName …>`` summary of an object `record_value` could not keep."""
    return (isinstance(value, str) and value not in _MARKERS
            and re.fullmatch(r"<[A-Za-z_]\w*( [^<>]*)?>", value) is not None)


# --- the calling frame ----------------------------------------------------


def capture_call(*, skip_modules=()) -> dict | None:
    """The nearest frame outside backstamp and ``skip_modules``: the call that made the output.

    Returns ``function``, ``module``, ``file``, ``lineno``, ``chain``, ``args`` and ``path``.

    - ``chain``: up to `MAX_CHAIN` function names, nearest first. ``function`` is often a
      local ``_save_fig`` closure; ``chain`` keeps the analysis around it. It ends at the
      first ``<module>``: the frames above a script's, cell's or module's top level are
      whatever ran it, such as IPython.
    - ``args``: every parameter's current value, unconverted.
    - ``path``: the file's full path, the git anchor. The record does not store it.
    """
    skip = set(_SKIP_MODULES) | set(skip_modules)
    try:
        stack = inspect.stack(0)
    except Exception:
        return None
    try:
        kept = [
            entry for entry in stack[1:]
            if (entry.frame.f_globals.get("__name__", "") not in skip)
            and not entry.frame.f_globals.get("__name__", "").startswith(
                ("importlib", "runpy"))
        ]
        if not kept:
            return None
        entry = kept[0]
        chain = [e.function for e in kept[:MAX_CHAIN]]
        if "<module>" in chain:
            chain = chain[: chain.index("<module>") + 1]
        return {
            "function": entry.function,
            "module": entry.frame.f_globals.get("__name__", ""),
            "file": Path(entry.filename).name,
            "path": entry.filename,
            "lineno": entry.lineno,
            "chain": chain,
            "args": _frame_args(entry.frame),
        }
    except Exception:
        return None
    finally:
        # Frame references would keep large plotting locals alive through a cycle.
        del stack


def _frame_args(frame) -> dict:
    """Every parameter of ``frame``'s function by name, ``*args`` and ``**kwargs`` included."""
    try:
        info = inspect.getargvalues(frame)
    except Exception:
        return {}
    args = {name: info.locals.get(name) for name in info.args if name not in ("self", "cls")}
    if info.varargs:
        args[info.varargs] = list(info.locals.get(info.varargs) or ())
    if info.keywords:
        for name, value in (info.locals.get(info.keywords) or {}).items():
            args[str(name)] = value
    return args


def resolve_inputs(inputs, source) -> dict | None:
    """``inputs`` as a dict of values as given.

    - A list of names is looked up in ``source``: the ``args`` of a `capture_call` result,
      or the ``params`` of a record. A name not found warns and is left out.
    """
    inputs = check_inputs(inputs)
    if inputs is None or isinstance(inputs, dict):
        return inputs
    source = source if isinstance(source, dict) else {}
    args = source.get("args", source.get("params")) or {}
    out = {}
    for name in inputs:
        if name in args:
            out[name] = args[name]
        else:
            _warn(f"backstamp: {source.get('function', 'the calling function')}() has no "
                  f"parameter {name!r}, so it is not in the inputs")
    return out


def inputs_record(inputs: dict, paths: str = "name", redact=()) -> dict:
    """``inputs`` as stored: JSON-safe, ``(start, end)`` tuples as ``"start/end"``."""
    return {_key(k, paths, False): "<redacted>" if k in redact
            else record_value(v, paths=paths, ranges=True) for k, v in inputs.items()}


# --- the record -----------------------------------------------------------


def provenance(*, anchor=None, call: dict | None = None, inputs=None, paths=None,
               redact=None, extra: dict | None = None, skip_modules=()) -> dict:
    """The record of what produced an output, as JSON-safe data.

    - ``call``: a `capture_call` result; captured here when left out.
    - ``anchor``: a file in the repo whose commit is recorded; the calling code's file by
      default.
    - ``inputs``, ``paths``, ``redact``, ``skip_modules``: as in `backstamp.save`.
    - ``extra``: fields added to the record as given.
    """
    from . import __version__

    paths, redact = _options(paths, redact)
    if call is None:
        call = capture_call(skip_modules=skip_modules)
    inputs = resolve_inputs(inputs, call)
    if anchor is None and call is not None:
        anchor = call.get("path")

    record = {
        "schema": SCHEMA,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **git_info(anchor),
        "version": package_version(call.get("module") if call else None),
        "python": platform.python_version(),
        "backstamp": __version__,
    }
    repo = record.get("repo")
    if repo and (_ABSOLUTE.match(repo) or repo.startswith("file://")):
        record["repo"] = _path(repo[len("file://"):] if repo.startswith("file://") else repo,
                               paths)
    if call is not None:
        record.update({k: call[k] for k in ("function", "module", "file", "lineno", "chain")
                       if k in call})
    if inputs is not None:
        record["inputs"] = inputs_record(inputs, paths, redact)
    if call is not None and "args" in call:
        record["params"] = {name: "<redacted>" if name in redact
                            else record_value(value, paths=paths)
                            for name, value in call["args"].items()}
    elif call is not None and "params" in call:
        record["params"] = call["params"]
    if extra:
        record.update(extra)
    return {k: v for k, v in record.items() if v is not None}


__all__ = [
    "provenance", "configure", "capture_call", "git_commit", "git_info", "package_version",
    "record_value", "resolve_inputs", "inputs_record", "check_inputs", "is_summary",
    "SCHEMA", "MAX_CHAIN", "PATHS",
]
