#!/usr/bin/env python
"""Tests for `backstamp.read`, `Record` and the ``backstamp`` command.

Run directly (no pytest needed)::

    python tests/test_read.py
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import backstamp  # noqa: E402
from backstamp._cli import clean_paths, input_values, value_matches  # noqa: E402
from backstamp._cli import main as backstamp_main  # noqa: E402
from backstamp._read import Record, _text, read  # noqa: E402
from backstamp._save import save  # noqa: E402
from backstamp.provenance import provenance  # noqa: E402

PLOTS = '''\
import matplotlib.pyplot as plt
import backstamp


def plot_accuracy(subjids, dates, fig_dir, window=50, data=None):
    fig, ax = plt.subplots()
    ax.plot([1, 2])
    path = backstamp.save(fig, "accuracy", fig_dir=fig_dir, inputs={"subject": subjids, "date": dates})
    plt.close(fig)
    return path
'''


def _git(root: Path, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def _plots_repo(root: Path) -> dict:
    """A committed repo at ``root`` holding ``plots.py``; returns the module's namespace."""
    _git(root, "init", "-q")
    for key, value in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(root, "config", key, value)
    _git(root, "remote", "add", "origin", "https://github.com/lab/plots.git")
    (root / "plots.py").write_text(PLOTS)
    (root / ".gitignore").write_text("__pycache__/\nfigs/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "plots")
    namespace = {"__name__": "plots"}
    exec(compile(PLOTS, str(root / "plots.py"), "exec"), namespace)
    return namespace


def _pdf(path: Path, **info) -> Path:
    """A one-axes PDF with the given info-dictionary entries."""
    fig, ax = plt.subplots()
    fig.savefig(path, metadata=info)
    plt.close(fig)
    return path


def _stamped(fig_dir, name, record, inputs=None) -> Path:
    fig, ax = plt.subplots()
    path = save(fig, name, fig_dir=fig_dir, provenance=record, inputs=inputs)
    plt.close(fig)
    return path


def _run(*argv) -> tuple:
    """``(exit code, stdout, stderr)`` of ``backstamp *argv``, in this process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = backstamp_main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _command(*argv, stdin=None):
    """``python -m backstamp *argv`` in a fresh process."""
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-m", "backstamp", *argv], input=stdin, env=env,
                          capture_output=True, text=True, encoding="utf-8")


# --- statuses -------------------------------------------------------------


def test_the_four_statuses():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fig, ax = plt.subplots()
        assert read(save(fig, "x", fig_dir=root)).status == "ok"
        plt.close(fig)
        assert read(_pdf(root / "bare.pdf")).status == "unstamped"
        assert read(root / "nope.pdf").status == "missing"
        (root / "junk.pdf").write_bytes(b"not a pdf")
        (root / "empty.pdf").write_bytes(b"")
        for path in (root / "junk.pdf", root / "empty.pdf", root):
            assert read(path).status == "unreadable", path


def test_a_junk_file_is_unreadable_quietly():
    """pypdf logs ``invalid pdf header`` for a junk file; `read` must not."""
    from pypdf import PdfReader

    seen = []
    handler = logging.Handler()
    handler.emit = seen.append
    logger = logging.getLogger("pypdf")
    logger.addHandler(handler)
    stderr = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(stderr):
            junk = Path(tmp) / "junk.pdf"
            junk.write_bytes(b"not a pdf")
            assert read(junk).status == "unreadable"
            quiet = not seen and not stderr.getvalue()
            try:
                PdfReader(str(junk))
            except Exception:
                pass
    finally:
        logger.removeHandler(handler)
    assert quiet and logger.level == logging.NOTSET
    assert seen, "pypdf no longer logs here, so this test checks nothing"


def test_a_subject_from_another_program_is_unstamped():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        text = read(_pdf(root / "a.pdf", Title="Figure 3", Subject="Grant figure",
                         Creator="Illustrator"))
        assert text.status == "unstamped"
        assert text.title == "Figure 3" and text.creator == "Illustrator"
        assert "unstamped" in str(text) and "Illustrator" in str(text)
        assert read(_pdf(root / "b.pdf", Subject="[1, 2]")).status == "unstamped"


def test_info_strings_are_read_through_indirect_references():
    class Reference:
        def get_object(self):
            return "resolved"

    assert _text(Reference()) == "resolved"
    assert _text(None) is None and _text(b"caf\xe9") == "café"


# --- fields ---------------------------------------------------------------


def test_a_record_reads_into_its_fields():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plots = _plots_repo(root)
        head = _git(root, "rev-parse", "--short", "HEAD").stdout.decode().strip()
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
        clean = read(plots["plot_accuracy"]([40, "mouseµ"], ("20251124", "20251130"),
                                            root / "figs", data=np.zeros((3, 4))))
        (root / "plots.py").write_text(PLOTS + "\n# edited\n")
        dirty = read(plots["plot_accuracy"]([40], ["20251124"], root / "figs"))

    assert clean.status == "ok" and clean.schema == 1 and clean.created
    assert (clean.commit, clean.dirty, clean.diff_sha) == (head, False, None)
    assert clean.branch == branch and clean.repo == "https://github.com/lab/plots.git"
    assert clean.title == "accuracy sub-40,mouseµ 20251124-20251130"
    assert clean.inputs == {"subject": [40, "mouseµ"], "date": "20251124/20251130"}
    assert clean.call == {
        "function": "plot_accuracy", "module": "plots", "file": "plots.py", "line": 8,
        "chain": clean.call["chain"],
        "params": {"subjids": [40, "mouseµ"], "dates": ["20251124", "20251130"],
                   "fig_dir": "figs", "window": 50, "data": "<ndarray (3, 4) float64>"},
    }
    assert clean.call["chain"][:2] == ["plot_accuracy", "test_a_record_reads_into_its_fields"]
    assert clean.versions == {"package": None, "python": platform.python_version(),
                              "backstamp": backstamp.__version__}
    assert clean.cut == [] and clean.extra == {}
    assert dirty.commit == head and dirty.dirty and len(dirty.diff_sha) == 12


def test_to_dict_is_json_and_holds_every_field():
    with tempfile.TemporaryDirectory() as tmp:
        rec = read(_stamped(tmp, "x", provenance(), inputs={"subject": [1]}))
    data = json.loads(json.dumps(rec.to_dict()))
    assert list(data) == ["path", "status", "title", "creator", "schema", "created", "commit",
                          "dirty", "diff_sha", "branch", "repo", "call", "inputs", "versions",
                          "cut", "extra"]
    assert data["call"]["function"] == "test_to_dict_is_json_and_holds_every_field"
    assert data["inputs"] == {"subject": [1]} and data["status"] == "ok"
    assert list(Record("p", "missing").to_dict()["call"]) == [
        "function", "module", "file", "line", "chain", "params"]


def test_a_record_cut_to_its_code_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rec = read(_stamped(tmp, "x", {**provenance(), "note": "x" * 40_000}))
    assert rec.status == "ok" and rec.call["function"] and rec.call["params"] is None
    assert rec.cut == ["every field but the code"]
    assert "every field but the code" in str(rec)
    assert "the parameters are not in the record" in rec.reproduce()


def test_str_shows_the_record_and_shortens_long_values():
    def plot(subjids, trials, token):
        return provenance(redact=["token"], inputs=["subjids"])

    with tempfile.TemporaryDirectory() as tmp:
        rec = read(_stamped(tmp, "acc", plot([40, 45], list(range(500)), "s3cret")))
    text = str(rec)
    lines = text.splitlines()
    assert lines[0] == rec.path
    assert "  title     acc subjids=40,45" in lines
    assert "  inputs    subjids = [40, 45]" in lines
    made_by = f"  made by   {__name__}:plot  (test_read.py:"
    assert any(line.startswith(made_by) for line in lines), lines
    assert "            token   = \"<redacted>\"" in lines
    trials = next(line for line in lines if "trials" in line)
    assert trials.endswith("...") and len(trials) < 250
    assert lines[-1].startswith("  (long values shortened")
    assert "s3cret" not in text
    page = rec._repr_html_()
    assert "<table>" in page and "&lt;redacted&gt;" in page


# --- reproduce ------------------------------------------------------------


def test_reproduce_lists_what_keeps_it_from_an_exact_rebuild():
    def plot(data, files, token, window=5):
        return provenance(redact=["token"])

    record = {**plot(np.zeros(3), [Path("/a/b.csv"), "<path>"], "s3cret"),
              "commit": "abc1234-dirty", "diff_sha": "0123456789ab",
              "repo": "git@github.com:lab/plots.git", "module": "lab.plots"}
    with tempfile.TemporaryDirectory() as tmp:
        text = read(_stamped(tmp, "x", record)).reproduce()
    assert "uncommitted changes (diff 0123456789ab)" in text
    assert ("placeholder, not the value, for data (<ndarray (3,) float64>), "
            "files (holds <path>), token (<redacted>): pass the real values") in text
    assert "git clone git@github.com:lab/plots.git\ncd plots\ngit checkout abc1234\n" in text
    assert "from lab.plots import plot\n" in text
    assert "    data=...,  # <ndarray (3,) float64>\n" in text
    assert "    window=5,\n" in text


def test_reproduce_without_repo_commit_or_module():
    with tempfile.TemporaryDirectory() as tmp:
        script = read(_stamped(tmp, "a", {"schema": 1, "function": "plot", "module": "__main__",
                                          "file": "make.py", "params": {}}))
        local = read(_stamped(tmp, "b", {"schema": 1, "function": "f", "module": "m",
                                         "commit": "abc1234", "repo": "lab.git"}))
        top = read(_stamped(tmp, "c", {"schema": 1, "function": "<module>", "file": "run.py"}))
        missing = read(Path(tmp) / "nope.pdf")
    text = script.reproduce()
    assert "The record names no repository" in text and "names no commit" in text
    assert "make.py ran as a script" in text
    assert "plot = runpy.run_path('make.py')['plot']\n\nplot()" in text
    compile(text.split("# Then, in Python")[1].split("\n", 1)[1], "<reproduce>", "exec")
    assert "stored as 'lab.git'" in local.reproduce()
    assert "cd <your clone of the repository>\ngit checkout abc1234" in local.reproduce()
    assert "top level of run.py" in top.reproduce()
    assert missing.reproduce().endswith("missing, no record to reproduce from")


def test_reproduce_repeats_the_call():
    """The Python part, run in the checkout, saves the same figure with the same record."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plots = _plots_repo(root)
        first = read(plots["plot_accuracy"]([40, 41], ["20251124"], root / "figs", window=7))
        Path(first.path).unlink()
        code = first.reproduce().split("# Then, in Python")[1].split("\n", 1)[1]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(SRC), str(root)]),
               "MPLBACKEND": "Agg"}
        subprocess.run([sys.executable, "-c", code], cwd=root, env=env, check=True)
        again = read(root / "figs" / "accuracy.pdf")
    assert again.status == "ok" and not again.dirty and again.commit == first.commit
    assert again.call["params"] == first.call["params"] and again.inputs == first.inputs


# --- the command line -----------------------------------------------------


def test_clean_paths_undoes_what_terminals_add():
    with tempfile.TemporaryDirectory() as tmp:
        a = str(Path(tmp) / "my figure (1).pdf")
        b = str(Path(tmp) / "Abbildung µ ü.pdf")
        for path in (a, b):
            Path(path).write_bytes(b"")
        escaped = a.replace(" ", "\\ ").replace("(", "\\(").replace(")", "\\)")
        assert clean_paths(a + "\n") == [a]                       # pasted as is
        assert clean_paths(escaped + " \n") == [a]                # Terminal, iTerm
        assert clean_paths(f"'{a}' ") == [a]                      # VS Code
        assert clean_paths(f'"{b}"\r\n') == [b]                   # Windows Terminal
        assert clean_paths(Path(b).as_uri()) == [b]               # a file:// URL
        assert clean_paths(f"{escaped} '{b}'") == [a, b]          # two dropped at once
        assert clean_paths(f"\x1b[200~{b}\x1b[201~") == [b]       # bracketed paste
    assert clean_paths("  \n") == [] and clean_paths("") == []
    assert clean_paths("'unclosed") == ["'unclosed"]


def test_show_reads_dropped_paths_until_an_empty_line():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "my figures"
        folder.mkdir()
        fig, ax = plt.subplots()
        a = save(fig, "accuracy µ", fig_dir=folder)
        b = save(fig, "second", fig_dir=folder)
        plt.close(fig)
        lines = [str(a).replace(" ", "\\ "), f"'{b}' ", "", str(a)]
        done = _command("show", stdin="\n".join(lines) + "\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("made by") == 2
    assert done.stdout.startswith(f"{a}\n") and f"\n{b}\n" in done.stdout
    assert done.stderr.startswith("Drop a figure here or paste its path")
    assert "pypdf" not in done.stderr and "Traceback" not in done.stderr


def test_show_takes_files_folders_and_globs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sub").mkdir()
        fig, ax = plt.subplots()
        one = save(fig, "one", fig_dir=root)
        save(fig, "two", fig_dir=root / "sub")
        plt.close(fig)
        _pdf(root / ".hidden.pdf")

        code, out, _ = _run("show", "--json", str(one))
        assert code == 0 and json.loads(out)["call"]["function"] == "test_show_takes_files_folders_and_globs"
        code, out, _ = _run("show", "--json", str(root))
        assert [Path(r["path"]).name for r in json.loads(out)] == ["one.pdf", "two.pdf"]
        code, out, _ = _run("show", "--json", str(root / "**" / "t*.pdf"))
        assert [Path(r["path"]).name for r in json.loads(out)] == ["two.pdf"]
        code, out, _ = _run("show", "--reproduce", str(one))
        assert code == 0 and out.startswith(f"# {one}\n")
        code, out, _ = _run("show", str(one), str(root / "nope.pdf"), str(root / "*.png"))
        assert code == 1 and out.count("missing: no such file") == 2
        (root / "empty").mkdir()
        code, _, err = _run("show", str(root / "empty"))
        assert code == 1 and "no PDFs" in err


def test_python_m_backstamp_runs_the_command():
    done = _command()
    assert done.returncode == 2 and "show" in done.stdout and "ls" in done.stdout


def _names(out: str) -> list:
    return [line.split()[0] for line in out.splitlines()[1:]]


def test_ls_lists_recursively_and_filters():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sub").mkdir()
        base = {"schema": 1, "created_at": "2026-10-05T14:02:11+01:00", "params": {}}
        _stamped(root, "a", {**base, "commit": "abc1234-dirty", "function": "plot_a",
                             "chain": ["plot_a", "run_all"]},
                 inputs={"subject": ["040", 41], "date": ("20251124", "20251130")})
        _stamped(root / "sub", "b", {**base, "commit": "def5678", "function": "plot_b"},
                 inputs={"subject": {40: ("2025-11-24", "2025-11-30"),
                                     41: {"session": [1, 2]}}})
        _stamped(root, "c", {**base, "commit": "abc9999", "function": "plot_c"},
                 inputs={"subject": "mouseA", "date": ["03oct2026"],
                         "condition": {"A": [1, 2]}})
        _pdf(root / "bare.pdf")
        _pdf(root / ".hidden.pdf")
        (root / "junk.pdf").write_bytes(b"not a pdf")

        def ls(*flags):
            return _names(_run("ls", str(root), *flags)[1])

        code, out, _ = _run("ls", str(root))
        assert code == 0 and out.splitlines()[0].split() == [
            "name", "function", "commit", "inputs", "created"]
        assert _names(out) == ["a", "bare", "c", "junk", "sub/b"]
        assert "abc1234-dirty" in out and "(unstamped)" in out and "(unreadable)" in out
        assert "2026-10-05" in out

        assert ls("--subject", "40") == ["a", "sub/b"]            # "040" by value
        assert ls("--subject", "41", "--session", "2") == ["sub/b"]
        assert ls("--date", "20251126") == ["a", "sub/b"]         # inside both ranges
        assert ls("--date", "2025-11-24") == ["a", "sub/b"]
        assert ls("--date", "20251101-20251123") == []
        assert ls("--date", "03oct2026") == ["c"]
        assert ls("--input", "condition=A") == ["c"]
        assert ls("--input", "condition=2") == ["c"]
        assert ls("--input", "subject=mouseA") == ["c"]
        assert ls("--function", "run_all") == ["a"]
        assert ls("--function", "plot_b") == ["sub/b"]
        assert ls("--commit", "abc") == ["a", "c"]
        assert ls("--commit", "abc1234-dirty") == ["a"]
        assert ls("--dirty") == ["a"] and ls("--dirty", "--subject", "mouseA") == []
        assert ls("--unstamped") == ["bare"]
        assert _run("ls", str(root), "--date", "19990101")[0] == 1


def test_value_matching():
    assert value_matches("040", "40") and value_matches("40", "040")
    assert not value_matches("mouseA", "mousea") and not value_matches("4", "40")
    assert value_matches("20251124/20251130", "20251130")
    assert value_matches("2025-11-24/2025-11-30", "20251127")
    assert value_matches("20251124-20251130", "2025-11-20/2025-11-24")
    assert not value_matches("20251124/20251130", "20251201")
    assert value_matches("03oct2026/10oct2026", "03oct2026")
    assert not value_matches("03oct2026/10oct2026", "05oct2026")
    assert not value_matches("1/5", "3")  # only dates match inside a range
    assert input_values({"subject": {"40": "20251124/20251130", "41": {"session": [1]}},
                         "condition": {"A": ["x"]}}) == {
        "subject": ["40", "41"], "date": ["20251124/20251130"], "session": ["1"],
        "condition": ["A", "x"]}


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
        plt.close("all")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
