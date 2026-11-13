"""Tests for scripts/pack_replay.py (local files and a fake scout only)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "pack_replay", ROOT / "scripts" / "pack_replay.py"
)
assert _spec is not None and _spec.loader is not None
pack_replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pack_replay)

Read = pack_replay.Read
HOME = str(Path.home())


@pytest.mark.parametrize(
    "command, reads",
    [
        ("sed -n '10,20p' a.cs", [Read("/r/a.cs", 10, 20)]),
        ("cd /r/sub && sed -n 5p b.cs", [Read("/r/sub/b.cs", 5, 5)]),
        ("cd sub; cat a.cs", [Read("/r/sub/a.cs", 1, None)]),
        (
            "S=Tools/x.sh; sed -n 1286,1330p $S; echo --; sed -n 9,12p ${S}",
            [Read("/r/Tools/x.sh", 1286, 1330), Read("/r/Tools/x.sh", 9, 12)],
        ),
        (
            "sed -n '1,9p;20,$p' a.cs",
            [Read("/r/a.cs", 1, 9), Read("/r/a.cs", 20, None)],
        ),
        ("sed -n -e 3,4p a.cs", [Read("/r/a.cs", 3, 4)]),
        ("sed -n '/class/,/}/p' a.cs", [Read("/r/a.cs", 1, None)]),
        ("sed 's/a/b/' a.cs", [Read("/r/a.cs", 1, None)]),
        ("sed -i '' 's/a/b/' a.cs", []),
        ("cat a.md | cut -c1-600", [Read("/r/a.md", 1, None)]),
        ("cat a.cs 2>/dev/null", [Read("/r/a.cs", 1, None)]),
        (
            "cat > new.cs <<'EOF'\nusing X;\ncat body.cs\nEOF\ncat b.cs",
            [
                Read("/r/b.cs", 1, None),
            ],
        ),
        (
            "head -30 a.cs; head -n 5 b.cs; head c.cs",
            [
                Read("/r/a.cs", 1, 30),
                Read("/r/b.cs", 1, 5),
                Read("/r/c.cs", 1, 10),
            ],
        ),
        ("head -c 3500 brief.md", [Read("/r/brief.md", 1, None)]),
        (
            "tail -20 p.md; tail -n +7 q.md",
            [
                Read("/r/p.md", -20, None),
                Read("/r/q.md", 7, None),
            ],
        ),
        ("awk 'NR>=236 && NR<=260' f.txt", [Read("/r/f.txt", 236, 260)]),
        ("awk -F'\\t' 'NR>386' docs/p.md", [Read("/r/docs/p.md", 387, None)]),
        ("nl -ba a.cs", [Read("/r/a.cs", 1, None)]),
        ("grep -n Foo a.cs; wc -l a.cs; ls sub", []),
        ("P=~/x.log; tail -5 $P", [Read(f"{HOME}/x.log", -5, None)]),
        ("cat $NOT_SET/a.cs", []),
        ("export P=docs; cat $P/a.md", [Read("/r/docs/a.md", 1, None)]),
        ("for f in a b; do cat c.cs; done", [Read("/r/c.cs", 1, None)]),
        ("sed -n '1,5p a.cs", []),
        (
            "cat a.cs \\\n  b.cs",
            [
                Read("/r/a.cs", 1, None),
                Read("/r/b.cs", 1, None),
            ],
        ),
    ],
)
def test_bash_reads(command, reads):
    assert pack_replay.bash_reads(command, "/r") == reads


def _record(kind, content, cwd, **extra):
    return {
        "type": kind,
        "isSidechain": True,
        "agentId": "a1b2c3",
        "cwd": cwd,
        "timestamp": "2026-11-13T10:00:00.000Z",
        "message": {"role": kind, "content": content},
        **extra,
    }


def _call(use_id, name, args, cwd):
    block = {"type": "tool_use", "id": use_id, "name": name, "input": args}
    return _record("assistant", [block], cwd)


def _answer(use_id, text, cwd, file=None):
    block = {"type": "tool_result", "tool_use_id": use_id, "content": text}
    extra = {"toolUseResult": {"type": "text", "file": file}} if file else {}
    return _record("user", [block], cwd, **extra)


def _file_result(path, content, start, lines, total):
    return {
        "filePath": path,
        "content": content,
        "numLines": lines,
        "startLine": start,
        "totalLines": total,
    }


def _write_jsonl(path, records):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _implementer_records(cwd, prompt=None):
    """A brief read with Read, code read three ways, an edit, a late read."""
    brief = f"{cwd}/.sdd/task-brief.md"
    return [
        _record("user", prompt or f"Your brief is {brief}", cwd),
        _call("t1", "Read", {"file_path": brief}, cwd),
        _answer(
            "t1",
            "1\t# Task\n2\tFix Foo",
            cwd,
            _file_result(brief, "# Task\nFix Foo", 1, 2, 2),
        ),
        _call("t2", "Bash", {"command": f"cd {cwd} && sed -n 3,6p a.py"}, cwd),
        _answer("t2", "three\nfour\nfive\nsix", cwd),
        _call("t3", "Read", {"file_path": f"{cwd}/b.py", "offset": 2}, cwd),
        _answer(
            "t3",
            "2\tb2\n3\tb3",
            cwd,
            _file_result(f"{cwd}/b.py", "b2\nb3", 2, 2, 3),
        ),
        _call("t4", "Bash", {"command": "cat docs/notes.md; wc -l a.py"}, cwd),
        _answer("t4", "the notes\n 9 a.py", cwd),
        _call("t5", "Grep", {"pattern": "Foo"}, cwd),
        _answer("t5", "a.py", cwd),
        _call("t6", "Edit", {"file_path": f"{cwd}/a.py"}, cwd),
        _answer("t6", "ok", cwd),
        _call("t7", "Read", {"file_path": f"{cwd}/late.py"}, cwd),
        _answer(
            "t7", "1\tlate", cwd, _file_result("late.py", "late", 1, 1, 1)
        ),
    ]


def test_an_implementer_is_its_prompt_and_reads_before_the_first_edit(
    tmp_path,
):
    path = _write_jsonl(
        tmp_path / "s" / "subagents" / "agent-a1b2c3.jsonl",
        _implementer_records("/r"),
    )
    found = pack_replay.read_implementer(path)
    assert found.agent == "a1b2c3" and found.cwd == "/r"
    assert found.prompt == "Your brief is /r/.sdd/task-brief.md"
    assert found.reads == [
        Read("/r/.sdd/task-brief.md", 1, 2),
        Read("/r/a.py", 3, 6),
        Read("/r/b.py", 2, 3),
        Read("/r/docs/notes.md", 1, None),
    ]


def test_an_implementer_keeps_the_text_of_files_read_alone(tmp_path):
    path = _write_jsonl(
        tmp_path / "agent-a1b2c3.jsonl", _implementer_records("/r")
    )
    texts = pack_replay.read_implementer(path).texts
    # notes.md shares its call with wc, so its output is not the file.
    assert texts == {
        "/r/.sdd/task-brief.md": "# Task\nFix Foo",
        "/r/a.py": "three\nfour\nfive\nsix",
        "/r/b.py": "b2\nb3",
    }


def test_a_subagent_that_never_edits_is_not_an_implementer(tmp_path):
    records = [r for r in _implementer_records("/r") if "t6" not in str(r)]
    path = _write_jsonl(tmp_path / "agent-x.jsonl", records)
    assert pack_replay.read_implementer(path) is None


def _git(cwd, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def _repo(path, files):
    """A git repo with `files` (relative path to text) in one commit."""
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "root")
    return path


@pytest.fixture
def repos(tmp_path):
    """The replay repo, a clone of it (a lane) and an unrelated repo."""
    main = _repo(
        tmp_path / "main",
        {
            "a.py": "".join(f"a{n}\n" for n in range(1, 10)),
            "b.py": "b1\nb2\nb3\n",
            "docs/notes.md": "the notes\n",
            ".sdd/task-brief.md": "# Task\nFix Foo\n",
            ".gitignore": "Library/\n",
        },
    )
    (main / "Library").mkdir()
    (main / "Library" / "run.log").write_text("log\n")
    (main / ".claude" / "worktrees").mkdir(parents=True)
    (tmp_path / "kept").mkdir()
    _git(tmp_path, "clone", "-q", str(main), "lane")
    other = _repo(tmp_path / "other", {"a.py": "x\n"})
    return main, tmp_path / "lane", other


@pytest.mark.parametrize(
    "where, rel",
    [
        ("main/a.py", "a.py"),
        ("main/docs/notes.md", "docs/notes.md"),
        ("lane/b.py", "b.py"),
        ("other/a.py", None),
        ("main/Library/run.log", None),
        ("main/gone.py", None),
        # A lane deleted since: beside the repo, or inside it.
        ("deleted-lane/docs/notes.md", "docs/notes.md"),
        ("main/.claude/worktrees/old/b.py", "b.py"),
        ("deleted-lane/Library/run.log", None),
        # A missing folder anywhere else is not a checkout of the repo.
        ("kept/gone/a.py", None),
    ],
)
def test_reads_map_to_files_the_replay_repo_tracks(repos, where, rel):
    main, _, _ = repos
    found = pack_replay.RepoMap(main).relative(str(main.parent / where))
    assert found == rel


def test_repo_map_counts_lines_of_tracked_files(repos):
    main, _, _ = repos
    assert pack_replay.RepoMap(main).line_count("a.py") == 9


def _implementer(main, records=None, **changes):
    path = _write_jsonl(
        main.parent / "t" / "agent-a1b2c3.jsonl",
        records or _implementer_records(str(main)),
    )
    found = pack_replay.read_implementer(path)
    for name, value in changes.items():
        setattr(found, name, value)
    return found


def test_a_case_asks_the_prompt_and_the_brief_it_names(repos):
    main, _, _ = repos
    case = pack_replay.build_case(
        _implementer(main), pack_replay.RepoMap(main)
    )
    assert case.question == (
        f"Your brief is {main}/.sdd/task-brief.md\n\n# Task\nFix Foo"
    )
    assert case.briefs == [".sdd/task-brief.md"]


def test_a_case_counts_tracked_reads_but_not_the_brief(repos):
    main, _, _ = repos
    case = pack_replay.build_case(
        _implementer(main), pack_replay.RepoMap(main)
    )
    assert case.read == {
        "a.py": [[3, 6]],
        "b.py": [[2, 3]],
        "docs/notes.md": [[1, 1]],
    }


def test_a_brief_missing_from_the_transcript_is_read_from_disk(repos):
    main, _, _ = repos
    found = _implementer(main, texts={})
    case = pack_replay.build_case(found, pack_replay.RepoMap(main))
    assert case.question.endswith("\n\n# Task\nFix Foo\n")


def test_a_brief_gone_everywhere_leaves_the_prompt(repos):
    main, _, _ = repos
    (main / ".sdd" / "task-brief.md").unlink()
    found = _implementer(main, texts={})
    case = pack_replay.build_case(found, pack_replay.RepoMap(main))
    assert case.question == f"Your brief is {main}/.sdd/task-brief.md"


def test_a_long_question_is_cut(repos, monkeypatch):
    main, _, _ = repos
    monkeypatch.setattr(pack_replay, "QUESTION_CHARS", 20)
    case = pack_replay.build_case(
        _implementer(main), pack_replay.RepoMap(main)
    )
    assert case.question == f"Your brief is {main}"[:20]


def test_spans_resolve_tails_and_merge(repos):
    main, _, _ = repos
    reads = [
        Read(f"{main}/a.py", 1, 3),
        Read(f"{main}/a.py", 4, 5),
        Read(f"{main}/a.py", -2, None),
        Read(f"{main}/b.py", 2, 99),
        Read(f"{main}/b.py", 7, 9),
    ]
    found = _implementer(main, reads=reads)
    case = pack_replay.build_case(found, pack_replay.RepoMap(main))
    assert case.read == {"a.py": [[1, 5], [8, 9]], "b.py": [[2, 3]]}


def _inline_answer(main, answer):
    """What nim_scout(text="inline") returns for `answer` in `main`."""
    import sys

    sys.path.insert(0, str(ROOT))
    from castor.nim.pack import _scout_text

    return _scout_text(answer, str(main), "q", "inline")


def test_pack_spans_are_read_from_the_inline_text(repos):
    main, _, _ = repos
    answer = _inline_answer(
        main,
        '- a.py:3-4 "a3" - where Foo is set\n'
        '- b.py:2 "b2" - the caller\n'
        "[nim_agent: 3 steps]",
    )
    assert pack_replay.pack_spans(answer) == {
        "a.py": [[1, 6]],
        "b.py": [[1, 3]],
    }


@pytest.mark.parametrize(
    "answer, spans",
    [
        ("- nothing found\n[text not added: the answer cites no range]", {}),
        ("[Error: NIM request failed]", None),
    ],
)
def test_pack_spans_of_empty_and_failed_scouts(answer, spans):
    assert pack_replay.pack_spans(answer) == spans


def test_score_compares_reads_with_the_pack():
    read = {"a.py": [[3, 6]], "b.py": [[2, 3]], "docs/notes.md": [[1, 1]]}
    pack = {"a.py": [[1, 6]], "c.py": [[1, 4]]}
    assert pack_replay.score(read, pack) == pack_replay.Score(
        files_read=3,
        files_held=1,
        lines_read=7,
        lines_held=4,
        pack_lines=10,
        pack_unread=6,
    )


def _project(main, agents):
    """A projects root: one session, its subagents (id, type, edits)."""
    import os

    root = main.parent / "projects"
    session = root / "-tmp-main" / "s1"
    _write_jsonl(root / "-tmp-main" / "s1.jsonl", [_record("user", "hi", "/")])
    for age, (agent, kind, edits) in enumerate(agents):
        records = [
            {**r, "agentId": agent} for r in _implementer_records(str(main))
        ]
        if not edits:
            records = [r for r in records if "t6" not in str(r)]
        path = _write_jsonl(
            session / "subagents" / f"agent-{agent}.jsonl", records
        )
        (session / "subagents" / f"agent-{agent}.meta.json").write_text(
            f'{{"agentType": "{kind}"}}'
        )
        stamp = 1_800_000_000 - 60 * age  # the first agent is the newest
        os.utime(path, (stamp, stamp))
    return root


class FakeScout:
    """Stands in for NIM: answers every question with one fixed answer."""

    def __init__(self, main, answer):
        self.text = (
            _inline_answer(main, answer) if answer[0] != "[" else answer
        )
        self.questions = []

    def __call__(self, question):
        self.questions.append(question)
        return self.text


def _main(main, root, scout, monkeypatch, *args):
    monkeypatch.setattr(
        pack_replay, "_castor_scout", lambda repo, model, tokens: scout
    )
    results = main.parent / "results.jsonl"
    pack_replay.main(
        [
            "--repo",
            str(main),
            "--root",
            str(root),
            "--days",
            "36500",
            "--results",
            str(results),
            *args,
        ]
    )
    return results


def test_main_scouts_each_task_and_reports_the_files_held(
    repos, monkeypatch, capsys
):
    main, _, _ = repos
    root = _project(main, [("aaa111", "impl", True), ("bbb222", "x", False)])
    scout = FakeScout(main, '- a.py:3-4 "a3" - where Foo is set\n')
    _main(main, root, scout, monkeypatch)
    out = capsys.readouterr().out
    assert len(scout.questions) == 1 and "Fix Foo" in scout.questions[0]
    assert "aaa111" in out and "bbb222" not in out
    assert "files held: 1 of 3 (33.3%)" in out
    assert "lines held: 4 of 7 (57.1%)" in out
    assert "pack lines nobody read: 2 of 6 (33.3%)" in out
    assert "stop" in out


def test_a_rerun_on_the_same_results_asks_nothing(repos, monkeypatch, capsys):
    main, _, _ = repos
    root = _project(main, [("aaa111", "impl", True)])
    scout = FakeScout(main, '- a.py:3-4 "a3" - where Foo is set\n')
    _main(main, root, scout, monkeypatch)
    first = capsys.readouterr().out
    _main(main, root, scout, monkeypatch)
    assert len(scout.questions) == 1
    assert capsys.readouterr().out == first


def test_agent_type_and_limit_choose_the_newest_of_a_kind(
    repos, monkeypatch, capsys
):
    main, _, _ = repos
    root = _project(
        main,
        [
            ("aaa111", "general", True),
            ("bbb222", "impl", True),
            ("ccc333", "impl", True),
        ],
    )
    scout = FakeScout(main, '- a.py:3-4 "a3" - where Foo is set\n')
    _main(
        main, root, scout, monkeypatch, "--agent-type", "impl", "--limit", "1"
    )
    out = capsys.readouterr().out
    assert "bbb222" in out
    assert "aaa111" not in out and "ccc333" not in out


def test_a_failed_scout_is_counted_not_scored(repos, monkeypatch, capsys):
    main, _, _ = repos
    root = _project(main, [("aaa111", "impl", True)])
    _main(main, root, FakeScout(main, "[Error: NIM down]"), monkeypatch)
    out = capsys.readouterr().out
    assert "1 scout failed" in out
    assert "files held" not in out


def test_the_script_runs_from_the_command_line():
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "pack_replay.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0 and "--agent-type" in done.stdout


def test_agent_type_comes_from_the_meta_file(tmp_path):
    path = _write_jsonl(tmp_path / "agent-a.jsonl", [])
    assert pack_replay.agent_type(path) is None
    (tmp_path / "agent-a.meta.json").write_text('{"agentType": "nqc-impl"}')
    assert pack_replay.agent_type(path) == "nqc-impl"
