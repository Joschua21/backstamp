#!/usr/bin/env python
"""Tests for `backstamp.provenance`, `backstamp._pdf` and `backstamp._save`.

Run directly (no pytest needed)::

    python tests/test_provenance.py
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
import warnings
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

try:
    import pandas as pd  # noqa: E402
except ImportError:  # not a dependency; the pandas cases run where it is installed
    pd = None

import backstamp  # noqa: E402
from backstamp._pdf import (  # noqa: E402
    MAX_BLOB, MAX_TITLE, _dump, build_pdf_metadata, filter_metadata, summarize_inputs,
)
from backstamp._read import read  # noqa: E402
from backstamp._save import save  # noqa: E402
from backstamp.provenance import (  # noqa: E402
    _strip_credentials, capture_call, configure, git_commit, git_info, package_version,
    provenance, record_value,
)


def _git_repo(root: Path):
    """A repo at ``root`` with one commit of ``a.txt``; returns a ``git`` runner."""
    def run(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    run("config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("one")
    run("add", "-A")
    run("commit", "-qm", "first")
    return run


def _caught(fn, *args, **kwargs):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = fn(*args, **kwargs)
    return result, [w for w in caught if "backstamp" in str(w.message)]


# --- values ---------------------------------------------------------------


def test_scalars_pass_through():
    for value in (None, True, False, 0, -3, 2.5, "short"):
        assert record_value(value) == value, value


def test_strings_and_containers_are_kept_in_full():
    assert record_value("x" * 500) == "x" * 500
    assert record_value(list(range(50))) == list(range(50))
    assert record_value({f"k{i}": i for i in range(50)}) == {f"k{i}": i for i in range(50)}
    assert record_value({"a": [1, {"b": "x" * 500}]}) == {"a": [1, {"b": "x" * 500}]}
    assert record_value((1, 2)) == [1, 2]
    assert record_value({3, 1, 2}) == [1, 2, 3]
    assert record_value({1: "a", (1, 2): "b"}) == {"1": "a", "[1, 2]": "b"}


def test_non_finite_floats_become_strings():
    """NaN and inf are not valid JSON; json.dumps emits them anyway, so catch them here."""
    for value in (float("nan"), float("inf"), float("-inf")):
        assert isinstance(record_value(value), str)
    assert json.loads(json.dumps({"v": record_value(float("nan"))}))


def test_numpy_scalars_become_plain_numbers_and_dates_iso_text():
    for value, expected in ((np.int64(3), 3), (np.float32(1.5), 1.5), (np.bool_(True), True)):
        out = record_value(value)
        assert out == expected and type(out) is type(expected), (value, out)
    assert record_value(date(2025, 11, 24)) == "2025-11-24"
    assert record_value(datetime(2025, 11, 24, 9, 30)) == "2025-11-24T09:30:00"


def test_data_objects_become_type_and_shape():
    assert record_value(np.zeros((100, 3))) == "<ndarray (100, 3) float64>"
    fig, ax = plt.subplots()
    assert record_value(ax).startswith("<Axes")
    plt.close(fig)

    class Table:
        shape = (5, 2)

    assert record_value(Table()) == "<Table (5, 2)>"
    if pd is not None:
        assert record_value(pd.DataFrame({"a": [1, 2], "b": [3, 4]})) == "<DataFrame 2x2>"
        assert record_value(pd.Series([1, 2, 3])).startswith("<Series 3 ")


def test_unknown_objects_become_type_names_and_cycles_stop():
    class Widget:
        pass

    assert record_value(Widget()) == "<Widget>"
    loop = [1]
    loop.append(loop)
    assert record_value(loop) == [1, "<cycle>"]


def test_values_are_json_serialisable():
    payload = {"arr": np.zeros(3), "nested": [{"x": None}], "big": list(range(100)),
               "nan": float("nan"), "n": np.int64(2), "when": date(2026, 1, 1)}
    if pd is not None:
        payload["df"] = pd.DataFrame({"a": [1]})
    json.dumps(record_value(payload))  # must not raise


# --- paths and private data -----------------------------------------------


_PATH_ARGS = {
    "path": Path("/Users/me/data/sub-040/trials.parquet"),
    "text": "/Users/me/data/x.csv",
    "home": "~/data/y.csv",
    "windows": "C:\\Users\\me\\z.csv",
    "relative_text": "figures/",
    "relative_path": Path("figures/a.pdf"),
    "label": "A/B",
}


def _plot_with_paths(path, text, home, windows, relative_text, relative_path, label, *,
                     _paths=None):
    return provenance(paths=_paths, inputs=["path", "text"])


def test_paths_are_stored_by_name_by_default():
    record = _plot_with_paths(**_PATH_ARGS)
    assert record["params"] == {
        "path": "trials.parquet", "text": "x.csv", "home": "y.csv", "windows": "z.csv",
        "relative_text": "figures/", "relative_path": "a.pdf", "label": "A/B",
        "_paths": None,
    }
    assert record["inputs"] == {"path": "trials.parquet", "text": "x.csv"}


def test_paths_full_and_hide():
    full = _plot_with_paths(**_PATH_ARGS, _paths="full")
    assert full["params"]["path"] == str(_PATH_ARGS["path"])
    assert full["params"]["windows"] == "C:\\Users\\me\\z.csv"
    assert full["params"]["home"] == "~/data/y.csv"

    hidden = _plot_with_paths(**_PATH_ARGS, _paths="hide")["params"]
    for name in ("path", "text", "home", "windows", "relative_path"):
        assert hidden[name] == "<path>", name
    assert hidden["relative_text"] == "figures/" and hidden["label"] == "A/B"


def test_paths_inside_containers_are_reduced_too():
    assert record_value({"files": [Path("/a/b/c.csv"), "/d/e.csv"]}) == {
        "files": ["c.csv", "e.csv"]}


def test_redact_lists_the_name_without_the_value():
    def plot(subjids, token):
        return provenance(redact=["token"], inputs={"subject": subjids, "token": token})

    record = plot([40], "s3cret")
    assert record["params"] == {"subjids": [40], "token": "<redacted>"}
    assert record["inputs"] == {"subject": [40], "token": "<redacted>"}
    assert "s3cret" not in json.dumps(record)


def test_configure_sets_defaults_a_call_overrides_paths_and_adds_to_redact():
    def plot(data, token, user):
        return provenance(), provenance(paths="full", redact=["user"]), provenance(redact=[])

    configure(paths="hide", redact=["token"])
    try:
        configured, per_call, empty = plot(Path("/x/y.csv"), "s3cret", "me")
    finally:
        configure(paths="name", redact=[])
    assert configured["params"] == {"data": "<path>", "token": "<redacted>", "user": "me"}
    assert per_call["params"] == {"data": "/x/y.csv", "token": "<redacted>",
                                  "user": "<redacted>"}
    assert empty["params"]["token"] == "<redacted>"  # a call cannot lift a configured name


def test_bad_settings_raise():
    for call in (lambda: configure(paths="short"), lambda: provenance(paths="nope")):
        try:
            call()
        except ValueError as e:
            assert "name" in str(e)
        else:
            raise AssertionError("no ValueError")
    try:
        configure(redact=5)
    except TypeError:
        pass
    else:
        raise AssertionError("no TypeError")


# --- capturing the call ---------------------------------------------------


def _plotter(data, subjids, *more, window=30, **kwargs):
    return provenance()


def test_the_record_holds_every_parameter():
    record = _plotter(np.zeros((2, 1)), [40, 45], "extra", window=7, color="red")
    assert record["function"] == "_plotter"
    assert record["file"] == "test_provenance.py"
    assert isinstance(record["lineno"], int)
    assert record["params"] == {
        "data": "<ndarray (2, 1) float64>", "subjids": [40, 45], "more": ["extra"],
        "window": 7, "color": "red",  # color arrived through **kwargs
    }


def test_long_lists_nested_dicts_and_many_parameters_are_stored_in_full():
    config = {"a": {"b": {"c": list(range(30))}}, **{f"k{i}": i for i in range(40)}}

    def plot(trials, config, p1=1, p2=2, p3=3, p4=4, p5=5, p6=6, p7=7, p8=8, p9=9, p10=10,
             p11=11, p12=12, p13=13, p14=14, p15=15, p16=16):
        return provenance()

    params = plot(list(range(500)), config)["params"]
    assert params["trials"] == list(range(500))
    assert params["config"] == config
    assert len(params) == 18 and params["p16"] == 16


def test_a_variable_passed_in_is_recorded_by_its_contents():
    ab_data = [63, 64, 65, 66]

    def plot(subjids):
        return provenance(inputs=["subjids"])

    record = plot(subjids=ab_data)
    assert record["params"]["subjids"] == [63, 64, 65, 66]
    assert record["inputs"] == {"subjids": [63, 64, 65, 66]}
    assert "ab_data" not in json.dumps(record)


def test_capture_skips_the_saving_machinery():
    """The first frame must never be backstamp itself."""
    assert capture_call()["function"] == "test_capture_skips_the_saving_machinery"


# --- git ------------------------------------------------------------------


def test_git_commit_reports_dirty_and_tolerates_a_non_repo():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert git_commit(root) is None  # not a repo -> None, never an exception
        _git_repo(root)
        clean = git_commit(root)
        assert clean and not clean.endswith("-dirty")
        (root / "a.txt").write_text("two")
        assert git_commit(root) == f"{clean}-dirty"

    assert git_commit(None) is None
    assert git_commit(Path("/nonexistent/xyz")) is None


def test_git_info_records_branch_repo_and_a_hash_of_the_diff():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = _git_repo(root)
        run("remote", "add", "origin", "https://user:token@example.com/lab/repo.git")
        run("checkout", "-qb", "topic")
        info = git_info(root)
        assert info == {"commit": info["commit"], "branch": "topic",
                        "repo": "https://example.com/lab/repo.git"}  # clean: no diff_sha

        (root / "a.txt").write_text("two")
        first = git_info(root)["diff_sha"]
        (root / "a.txt").write_text("three")
        assert git_info(root)["diff_sha"] != first
        (root / "a.txt").write_text("two")
        assert git_info(root)["diff_sha"] == first

        run("checkout", "-q", "--detach")
        assert git_info(root)["branch"] is None
    assert git_info(None) == {}


def test_credentials_are_stripped_and_a_local_remote_is_a_path():
    assert _strip_credentials("https://x:y@github.com/a/b.git") == "https://github.com/a/b.git"
    assert _strip_credentials("https://tok@en@github.com/a/b") == "https://github.com/a/b"
    assert _strip_credentials("git@github.com:a/b.git") == "git@github.com:a/b.git"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = _git_repo(root)
        run("remote", "add", "origin", "/Users/me/repos/lab.git")
        assert provenance(anchor=root, call={"module": "x"})["repo"] == "lab.git"


def test_package_version_resolves_a_renamed_distribution():
    """Import and distribution names often differ; only packages_distributions() maps
    them, from Python 3.10."""
    with tempfile.TemporaryDirectory() as tmp:
        info = Path(tmp) / "renamed_dist-1.2.3.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: renamed-dist\nVersion: 1.2.3\n")
        (info / "top_level.txt").write_text("other_name\n")
        sys.path.insert(0, tmp)
        try:
            renamed = package_version("other_name.sub")
        finally:
            sys.path.remove(tmp)
    if sys.version_info >= (3, 10):
        assert renamed == "1.2.3", renamed

    assert package_version("matplotlib.figure")
    assert package_version("__main__") is None
    assert package_version("definitely_not_installed_xyz") is None
    assert package_version(None) is None


# --- the record -----------------------------------------------------------


def test_the_record_has_a_schema_and_versions_and_no_full_path():
    record = provenance()
    assert record["function"] == "test_the_record_has_a_schema_and_versions_and_no_full_path"
    assert record["schema"] == 1
    assert record["python"] == platform.python_version()
    assert record["backstamp"] == backstamp.__version__
    assert "path" not in record and "created_at" in record
    json.dumps(record)


def test_provenance_anchors_on_the_caller_not_on_the_library():
    """The commit must describe the code that ran, so the anchor is the caller's file."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _git_repo(root)
        module = root / "plots.py"
        module.write_text("from backstamp.provenance import provenance\n"
                          "def plot():\n    return provenance()\n")
        namespace = {"__name__": "plots"}
        exec(compile(module.read_text(), str(module), "exec"), namespace)
        record = namespace["plot"]()
        assert record["commit"] == git_commit(root)  # dirty: plots.py is untracked
        assert record["commit"].endswith("-dirty") and "diff_sha" in record


# --- inputs ---------------------------------------------------------------


def test_inputs_numeric_and_text_ids_and_text_dates_are_stored_as_given():
    record = provenance(inputs={"subject": [40, "mouseA"], "date": ["20261002", "03oct2026"]})
    assert record["inputs"] == {"subject": [40, "mouseA"], "date": ["20261002", "03oct2026"]}


def test_inputs_ranges():
    """A tuple of two is a range, stored ``start/end``; a list of two is two values."""
    record = provenance(inputs={
        "date": ("20251124", "20251130"),
        "days": (20251124, 20251130),
        "text": "20251124-20251130",
        "pair": ["20251124", "20251130"],
        "triple": (1, 2, 3),
    })
    assert record["inputs"] == {
        "date": "20251124/20251130", "days": "20251124/20251130", "text": "20251124-20251130",
        "pair": ["20251124", "20251130"], "triple": [1, 2, 3],
    }


def test_inputs_mappings():
    """Mapping keys become text in JSON: 40 reads back as "40"."""
    record = provenance(inputs={
        "subject": {40: ("20251124", "20251130"), 41: ["20260101", "20260115"],
                    "mouseA": {"session": [1, 2]}},
    })
    assert record["inputs"] == {"subject": {
        "40": "20251124/20251130", "41": ["20260101", "20260115"],
        "mouseA": {"session": [1, 2]},
    }}


def test_inputs_named_parameters_are_copied_in_full():
    def plot(subjids, dates, window=50):
        return provenance(inputs=["subjids", "dates"])

    record = plot(list(range(40, 80)), ("20251124", "20251130"))
    assert record["inputs"] == {"subjids": list(range(40, 80)), "dates": "20251124/20251130"}


def test_an_unknown_input_name_warns_at_the_caller():
    def plot(subjids):
        return provenance(inputs=["subjids", "subjid"])

    record, caught = _caught(plot, [40])
    assert record["inputs"] == {"subjids": [40]}
    assert len(caught) == 1, caught
    assert "plot()" in str(caught[0].message) and "'subjid'" in str(caught[0].message)
    assert caught[0].filename == __file__


def test_inputs_of_the_wrong_type_raise():
    for bad in (40, [1, 2], {"a"}):
        try:
            provenance(inputs=bad)
        except TypeError as e:
            assert "inputs" in str(e)
        else:
            raise AssertionError(f"no TypeError for {bad!r}")


# --- the Title ------------------------------------------------------------


def test_title_short_forms_for_the_recommended_keys():
    assert summarize_inputs("accuracy", {"subject": [40, 45], "date": ["20260203", "20251124"]}
                            ) == "accuracy sub-40,45 20251124-20260203"
    assert summarize_inputs("x", {"subject": "mouseA", "date": 20251124, "session": [1, 2]}
                            ) == "x sub-mouseA 20251124 ses-1,2"
    assert summarize_inputs("plain") == "plain"


def test_title_date_ranges_and_formats():
    assert summarize_inputs("x", {"date": ["20251124/20251130", "20260101-20260115"]}
                            ) == "x 20251124-20260115"
    assert summarize_inputs("x", {"date": "2025-11-24/2025-11-30"}) == "x 2025-11-24-2025-11-30"
    # Formats that do not sort are listed as given.
    assert summarize_inputs("x", {"date": ["20261002", "03oct2026"]}) == "x 20261002,03oct2026"
    assert summarize_inputs("x", {"date": "03oct2026/10oct2026"}) == "x 03oct2026-10oct2026"


def test_title_reads_mappings():
    assert summarize_inputs("x", {"subject": {"40": "20251124/20251130",
                                              "41": "20260101/20260115"}}
                            ) == "x sub-40,41 20251124-20260115"
    assert summarize_inputs("x", {"subject": {"40": {"session": [1, 2]},
                                              "41": {"session": [2, 3]}}}
                            ) == "x sub-40,41 ses-1,2,3"


def test_title_other_keys_one_line_and_capped():
    assert summarize_inputs("x", {"condition": ["A", "B"], "subject": 1}
                            ) == "x sub-1 condition=A,B"
    long = summarize_inputs("x", {"note": "line\nbreak", "subject": list(range(500))})
    assert "\n" not in long and len(long) == MAX_TITLE and long.endswith("...")


# --- record size ----------------------------------------------------------


def test_a_record_over_the_cap_warns_cuts_the_largest_and_still_saves():
    def plot(trials, frame, fig_dir, window=50):
        fig, ax = plt.subplots()
        path = save(fig, "big", fig_dir=fig_dir)
        plt.close(fig)
        return path

    with tempfile.TemporaryDirectory() as tmp:
        path, caught = _caught(plot, list(range(10_000)), np.zeros(3), tmp)
        got = read(path)
    assert len(caught) == 1, caught
    message = str(caught[0].message)
    assert "big.pdf" in message and "30,000" in message and "params['trials']" in message
    assert caught[0].filename == __file__
    params = got.call["params"]
    assert params["trials"].startswith("<cut, ") and got.cut == ["params.trials"]
    assert "frame" not in params  # a data summary, dropped first
    assert params["window"] == 50 and got.call["function"] == "plot"


def test_inputs_are_cut_after_parameters_and_the_code_fields_never():
    record = {"schema": 1, "commit": "abc1234", "function": "f", "chain": ["f"],
              "params": {"a": 1}, "inputs": {"subject": list(range(10_000))}}
    blob, caught = _caught(_dump, record, "fig.pdf")
    parsed = json.loads(blob)
    assert len(blob) <= MAX_BLOB and len(caught) == 1
    assert parsed["inputs"]["subject"].startswith("<cut, ") and parsed["params"] == {"a": 1}

    blob, caught = _caught(_dump, {**record, "note": "x" * 40_000}, "fig.pdf")
    parsed = json.loads(blob)
    assert len(blob) <= MAX_BLOB
    assert parsed == {"schema": 1, "commit": "abc1234", "function": "f", "chain": ["f"]}
    assert "every field but the code" in str(caught[0].message)


def test_blob_stays_ascii_so_the_pdf_string_encoding_is_predictable():
    """Any non-ASCII character makes matplotlib write the string as UTF-16BE."""
    blob = _dump({"params": record_value({"unicode": "µ ± σ °C"})}, "x")
    blob.encode("ascii")  # must not raise


# --- saving ---------------------------------------------------------------


def test_pdf_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        fig, ax = plt.subplots()
        ax.plot([1, 2, 3], [1, 4, 9])
        path = save(fig, "accuracy", fig_dir=tmp,
                    inputs={"subject": [40, 66], "date": [20251124, 20260203]})
        plt.close(fig)
        assert path.exists() and path.name == "accuracy.pdf"

        got = read(path)
        assert got.call["function"] == "test_pdf_round_trip"
        assert got.call["file"] == "test_provenance.py"
        assert got.inputs == {"subject": [40, 66], "date": [20251124, 20260203]}
        assert got.title == "accuracy sub-40,66 20251124-20260203"
        assert got.schema == 1 and got.created


def test_save_takes_a_figure_an_axes_or_a_seaborn_grid():
    fig, ax = plt.subplots()
    in_subfigure = plt.figure().subfigures(1, 2)[0].subplots()
    grid = type("FacetGrid", (), {"__module__": "seaborn.axisgrid"})()
    grid.figure = fig
    with tempfile.TemporaryDirectory() as tmp:
        for i, obj in enumerate((fig, ax, in_subfigure, grid)):
            assert save(obj, f"f{i}", fig_dir=tmp).exists()
        for bad in (None, [ax], np.array([ax]), "fig"):
            try:
                save(bad, "x", fig_dir=tmp)
            except TypeError as e:
                assert "Figure, an Axes or a seaborn grid" in str(e)
            else:
                raise AssertionError(f"no TypeError for {bad!r}")
    plt.close("all")


def test_filename_a_string_or_a_callable_and_a_new_fig_dir():
    seen = []

    def name_it(name, inputs):
        seen.append((name, inputs))
        return f"{name}_sub-{inputs['subjids'][0]}.pdf"

    def plot(subjids, fig_dir):
        fig, ax = plt.subplots()
        path = save(fig, "acc", fig_dir=fig_dir, filename=name_it, inputs=["subjids"])
        plt.close(fig)
        return path

    with tempfile.TemporaryDirectory() as tmp:
        assert plot([40, 45], tmp) == Path(tmp) / "acc_sub-40.pdf"
        assert seen == [("acc", {"subjids": [40, 45]})]

        fig, ax = plt.subplots()
        path = save(fig, "x", fig_dir=Path(tmp) / "new", filename="x_sub-040.pdf")
        plt.close(fig)
        assert path == Path(tmp) / "new" / "x_sub-040.pdf" and path.exists()


def test_explicit_provenance_overrides_introspection_and_gets_the_inputs():
    explicit = {"function": "the_real_plotter", "note": "explicit"}
    with tempfile.TemporaryDirectory() as tmp:
        fig, ax = plt.subplots()
        path = save(fig, "x", fig_dir=tmp, inputs={"subject": 1}, provenance=explicit)
        plt.close(fig)
        got = read(path)
    assert got.call["function"] == "the_real_plotter" and got.extra == {"note": "explicit"}
    assert got.inputs == {"subject": 1}
    assert explicit == {"function": "the_real_plotter", "note": "explicit"}  # not mutated


def test_creator_names_the_package_or_backstamp():
    assert build_pdf_metadata("x", {"module": "lab.plots", "version": "1.2"}
                              )["Creator"] == "lab.plots 1.2"
    assert build_pdf_metadata("x", {"module": "__main__"}
                              )["Creator"] == f"backstamp {backstamp.__version__}"


def test_metadata_in_your_own_savefig_and_pdfpages():
    from matplotlib.backends.backend_pdf import PdfPages

    def plot_accuracy(subjids, out):
        fig, ax = plt.subplots()
        fig.savefig(out / "own.pdf", metadata=backstamp.metadata(inputs=["subjids"]))
        with PdfPages(out / "pages.pdf", metadata=backstamp.metadata("pages")) as pdf:
            pdf.savefig(fig)
        plt.close(fig)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        _, caught = _caught(plot_accuracy, [40], out)
        own = read(out / "own.pdf")
        pages = read(out / "pages.pdf")
    assert not caught
    assert own.call["function"] == "plot_accuracy" and own.inputs == {"subjids": [40]}
    assert own.title == "plot_accuracy subjids=40"
    assert pages.call["function"] == "plot_accuracy" and pages.title == "pages"


def test_filter_metadata_drops_keys_matplotlib_would_reject():
    out = filter_metadata({"Title": "t", "Subject": "s", "Custom": "x"})
    assert out == {"Title": "t", "Subject": "s"}


def test_saving_never_warns_about_infodict_keys():
    with tempfile.TemporaryDirectory() as tmp:
        fig, ax = plt.subplots()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            save(fig, "x", fig_dir=tmp, metadata={"Bogus": "dropped"})
        plt.close(fig)
        assert not [w for w in caught if "infodict" in str(w.message)]


# --- round trips ----------------------------------------------------------


def test_round_trip_keeps_parens_and_backslashes():
    """PDF literal strings escape both."""
    with tempfile.TemporaryDirectory() as tmp:
        fig, ax = plt.subplots()
        note = r"parens ( ) and \ backslash"
        path = save(fig, "x", fig_dir=tmp, provenance={"function": "f", "note": note})
        plt.close(fig)
        assert read(path).extra["note"] == note


def test_round_trip_keeps_long_and_non_ascii_arguments():
    with tempfile.TemporaryDirectory() as tmp:
        fig, ax = plt.subplots()
        path = save(fig, "x", fig_dir=tmp, provenance={
            "function": "f",
            "params": record_value({"many": list(range(99)), "unicode": "µ ± σ °C"}),
        })
        plt.close(fig)
        got = read(path)
        assert got.call["function"] == "f"
        assert got.call["params"]["many"] == list(range(99))
        assert got.call["params"]["unicode"] == "µ ± σ °C"


# --- wrappers and the chain -----------------------------------------------


def test_a_wrapper_can_exclude_itself():
    """A thin wrapper around save must not be recorded as the plotter.

    Capturing inside the wrapper is not enough -- capture_call still returns the
    wrapper's own frame. Only the skip list fixes it.
    """
    import types

    # The wrapper and the plotter need separate module namespaces, or skipping hides both.
    wrapper_mod = types.ModuleType("fake_repo.io.save")
    wrapper_mod.provenance = provenance
    exec(
        "def save_figure():\n"
        "    return provenance(), provenance(skip_modules=('fake_repo.io.save',))\n",
        wrapper_mod.__dict__,
    )

    def real_plotter():
        return wrapper_mod.save_figure()

    naive, skipped = real_plotter()
    assert naive["function"] == "save_figure"      # the bug this guards against
    assert skipped["function"] == "real_plotter"   # what we actually want


def test_save_forwards_skip_modules():
    import types

    wrapper_mod = types.ModuleType("fake_repo.io.save")
    wrapper_mod.save = save
    exec(
        "def save_figure(fig, fig_dir):\n"
        "    return save(fig, 'x', fig_dir=fig_dir, skip_modules=(__name__,))\n",
        wrapper_mod.__dict__,
    )

    def real_plotter(fig_dir):
        fig, ax = plt.subplots()
        path = wrapper_mod.save_figure(fig, fig_dir)
        plt.close(fig)
        return path

    with tempfile.TemporaryDirectory() as tmp:
        assert read(real_plotter(tmp)).call["function"] == "real_plotter"


def test_chain_reaches_past_an_inner_save_closure():
    """``function`` is the nearest frame not skipped, often a local closure; the chain
    keeps the function around it."""
    def run_movement_stats_batch():
        def _save_fig():  # the closure that actually calls save
            return provenance()
        return _save_fig()

    record = run_movement_stats_batch()
    assert record["function"] == "_save_fig"
    assert record["chain"][:2] == ["_save_fig", "run_movement_stats_batch"]
    assert "test_chain_reaches_past_an_inner_save_closure" in record["chain"]


def test_chain_survives_the_pdf_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        def analysis():
            def _save_fig():
                fig, ax = plt.subplots()
                path = save(fig, "x", fig_dir=tmp)
                plt.close(fig)
                return path
            return _save_fig()

        got = read(analysis())
        assert got.call["chain"][:2] == ["_save_fig", "analysis"]


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append(test.__name__)
            print(f"  [FAIL] {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  [ok]   {test.__name__}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
