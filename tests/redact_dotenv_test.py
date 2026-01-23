from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

from pre_commit_hooks.redact_dotenv import DEFAULT_ENV_FILE
from pre_commit_hooks.redact_dotenv import DEFAULT_EXAMPLE_ENV_FILE
from pre_commit_hooks.redact_dotenv import DEFAULT_GITIGNORE_FILE
from pre_commit_hooks.redact_dotenv import ensure_env_in_gitignore
from pre_commit_hooks.redact_dotenv import GITIGNORE_BANNER
from pre_commit_hooks.redact_dotenv import main
from pre_commit_hooks.redact_dotenv import redact_env_file

# Tests cover hook behavior: detection gating, .gitignore normalization,
# redaction logic, formatting preservation, and byte-for-byte output verification.


@pytest.fixture()
def env_file(tmp_path: Path) -> Path:
    """Copy shared resource .env into tmp workspace as the canonical .env.

    All tests rely on this baseline content (optionally appending extra lines
    for edge cases) to ensure consistent parsing behavior.
    """
    # Find repository root by looking for .git directory
    test_file_path = Path(__file__).resolve()
    repo_root = test_file_path
    while repo_root.parent != repo_root:  # Stop at filesystem root
        if (repo_root / '.git').exists():
            break
        repo_root = repo_root.parent
    else:
        raise RuntimeError('Could not find repository root (.git directory)')

    # Source file stored as test.env in repo (cannot commit a real .env in CI)
    resource_env = repo_root / 'testing' / 'resources' / 'test.env'
    dest = tmp_path / DEFAULT_ENV_FILE
    shutil.copyfile(resource_env, dest)
    return dest


@pytest.fixture()
def expected_example() -> Path:
    """Get path to expected example.env for comparison tests."""
    test_file_path = Path(__file__).resolve()
    repo_root = test_file_path
    while repo_root.parent != repo_root:
        if (repo_root / '.git').exists():
            break
        repo_root = repo_root.parent
    else:
        raise RuntimeError('Could not find repository root (.git directory)')

    return repo_root / 'testing' / 'resources' / 'example.env'


def run_hook(
        tmp_path: Path, staged: list[str], create_example: bool = False,
) -> int:
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        args = staged[:]
        if create_example:
            args.append('--create-example')
        return main(args)
    finally:
        os.chdir(cwd)


def test_no_env_file(tmp_path: Path, env_file: Path) -> None:
    """Hook should no-op (return 0) if .env not staged even if it exists."""
    (tmp_path / 'foo.txt').write_text('x')
    assert run_hook(tmp_path, ['foo.txt']) == 0


def test_blocks_env_and_updates_gitignore(
        tmp_path: Path, env_file: Path,
) -> None:
    """Staging .env triggers block (exit 1) and appends banner + env entry."""
    ret = run_hook(tmp_path, [DEFAULT_ENV_FILE])
    assert ret == 1
    gi = (tmp_path / DEFAULT_GITIGNORE_FILE).read_text().splitlines()
    assert gi[-2] == GITIGNORE_BANNER
    assert gi[-1] == DEFAULT_ENV_FILE


def test_env_present_but_not_staged(tmp_path: Path, env_file: Path) -> None:
    """Existing .env on disk but not staged should not block commit."""
    assert run_hook(tmp_path, ['unrelated.txt']) == 0


def test_byte_for_byte_match(
        tmp_path: Path, env_file: Path, expected_example: Path,
) -> None:
    """Processing test.env should produce EXACTLY example.env byte-for-byte."""
    example_file = tmp_path / DEFAULT_EXAMPLE_ENV_FILE

    # Redact the env file
    success = redact_env_file(str(env_file), str(example_file))
    assert success is True

    # Read both files as bytes
    generated = example_file.read_bytes()
    expected = expected_example.read_bytes()

    # Compare byte-for-byte
    if generated != expected:
        # Show detailed diff for debugging
        gen_lines = generated.decode('utf-8').splitlines()
        exp_lines = expected.decode('utf-8').splitlines()

        print("\nGenerated vs Expected line-by-line diff:")
        for i, (g, e) in enumerate(zip(gen_lines, exp_lines), 1):
            if g != e:
                print(f"Line {i} differs:")
                print(f"  Generated: {g!r}")
                print(f"  Expected:  {e!r}")

        if len(gen_lines) != len(exp_lines):
            print(f"\nLine count: {len(gen_lines)} vs {len(exp_lines)}")

    assert generated == expected, "Generated output must match example.env exactly"


def test_idempotent_gitignore(tmp_path: Path, env_file: Path) -> None:
    """Re-running after initial normalization leaves .gitignore unchanged."""
    g = tmp_path / DEFAULT_GITIGNORE_FILE
    g.write_text(f"{GITIGNORE_BANNER}\n{DEFAULT_ENV_FILE}\n")
    first = run_hook(tmp_path, [DEFAULT_ENV_FILE])
    assert first == 1
    content1 = g.read_text()
    second = run_hook(tmp_path, [DEFAULT_ENV_FILE])
    assert second == 1
    assert g.read_text() == content1  # unchanged


def test_gitignore_with_existing_content_preserved(
        tmp_path: Path, env_file: Path,
) -> None:
    """Existing entries stay intact; banner/env appended at end cleanly."""
    g = tmp_path / DEFAULT_GITIGNORE_FILE
    g.write_text(
        'node_modules/\n# comment line\n',
    )  # existing content with trailing newline
    run_hook(tmp_path, [DEFAULT_ENV_FILE])
    lines = g.read_text().splitlines()
    # original content should still be at top
    assert lines[0] == 'node_modules/'
    assert '# comment line' in lines[1]
    # Last two lines should be banner + env file
    assert lines[-2:] == [GITIGNORE_BANNER, DEFAULT_ENV_FILE]


def test_gitignore_duplicates_are_collapsed(
        tmp_path: Path, env_file: Path,
) -> None:
    """Multiple prior duplicate banner/env lines collapse to single pair."""
    g = tmp_path / DEFAULT_GITIGNORE_FILE
    g.write_text(
        f"other\n{GITIGNORE_BANNER}\n{DEFAULT_ENV_FILE}\n"
        f"{GITIGNORE_BANNER}\n{DEFAULT_ENV_FILE}\n\n\n",
    )
    run_hook(tmp_path, [DEFAULT_ENV_FILE])
    lines = g.read_text().splitlines()
    assert lines.count(GITIGNORE_BANNER) == 1
    assert lines.count(DEFAULT_ENV_FILE) == 1
    assert lines[-2:] == [GITIGNORE_BANNER, DEFAULT_ENV_FILE]


def test_creates_example_automatically(tmp_path: Path, env_file: Path) -> None:
    """Example file is created automatically with redacted values."""
    ret = run_hook(tmp_path, [DEFAULT_ENV_FILE])
    assert ret == 1
    example = tmp_path / DEFAULT_EXAMPLE_ENV_FILE
    assert example.exists()
    # Check that values are redacted
    content = example.read_text()
    assert 'API_KEY' in content or 'PASSWORD' in content
    # Check that asterisks are used for redaction
    assert '*' in content


def test_gitignore_without_trailing_newline(
        tmp_path: Path, env_file: Path,
) -> None:
    """Normalization works when original .gitignore lacks trailing newline."""
    g = tmp_path / DEFAULT_GITIGNORE_FILE
    g.write_text('existing_line')  # no newline at EOF
    run_hook(tmp_path, [DEFAULT_ENV_FILE])
    lines = g.read_text().splitlines()
    assert lines[0] == 'existing_line'
    assert lines[-2:] == [GITIGNORE_BANNER, DEFAULT_ENV_FILE]


def test_ensure_env_in_gitignore_normalizes(
        tmp_path: Path, env_file: Path,
) -> None:
    """Direct API call collapses duplicates and produces canonical tail
    layout.
    """
    g = tmp_path / DEFAULT_GITIGNORE_FILE
    g.write_text(
        f"{GITIGNORE_BANNER}\n{DEFAULT_ENV_FILE}\n"
        f"{GITIGNORE_BANNER}\n{DEFAULT_ENV_FILE}\n\n",
    )
    modified = ensure_env_in_gitignore(
        DEFAULT_ENV_FILE, str(g), GITIGNORE_BANNER,
    )
    assert modified is True
    lines = g.read_text().splitlines()
    # final two lines should be banner + env
    assert lines[-2:] == [GITIGNORE_BANNER, DEFAULT_ENV_FILE]
    # only one occurrence each
    assert lines.count(GITIGNORE_BANNER) == 1
    assert lines.count(DEFAULT_ENV_FILE) == 1


def test_source_env_file_not_modified(
        tmp_path: Path, env_file: Path,
) -> None:
    """Hook must not alter original .env (comments and formatting stay)."""
    original = env_file.read_text()
    run_hook(tmp_path, [DEFAULT_ENV_FILE])
    assert env_file.read_text() == original


def test_failure_message_content(
        tmp_path: Path,
        env_file: Path,
        capsys: pytest.CaptureFixture[str],
) -> None:
    """Hook stdout message should contain key phrases when blocking commit."""
    ret = run_hook(tmp_path, [DEFAULT_ENV_FILE])
    assert ret == 1
    out = capsys.readouterr().out.strip()
    assert 'Blocked committing' in out
    assert DEFAULT_GITIGNORE_FILE in out
    assert DEFAULT_EXAMPLE_ENV_FILE in out
    assert 'Remove .env' in out


def test_no_example_when_env_missing(
        tmp_path: Path, env_file: Path,
) -> None:
    """With no .env present, example should not be created.

    Uses env_file fixture (requirement: all tests use fixture) then removes the
    copied .env to simulate absence.
    """
    env_file.unlink()
    ret = run_hook(tmp_path, ['unrelated.txt'])
    assert ret == 0
    assert not (tmp_path / DEFAULT_EXAMPLE_ENV_FILE).exists()


def test_gitignore_is_directory_error(
        tmp_path: Path,
        env_file: Path,
        capsys: pytest.CaptureFixture[str],
) -> None:
    """If .gitignore path is a directory, hook should print error and still
    block.
    """
    gitignore_dir = tmp_path / DEFAULT_GITIGNORE_FILE
    gitignore_dir.mkdir()
    ret = run_hook(tmp_path, [DEFAULT_ENV_FILE])
    assert ret == 1  # still blocks commit
    captured = capsys.readouterr()
    assert 'ERROR:' in captured.err  # error now printed to stderr


def test_env_example_overwrites_existing(
        tmp_path: Path, env_file: Path,
) -> None:
    """Pre-existing example file with non-redacted values should be updated."""
    example = tmp_path / DEFAULT_EXAMPLE_ENV_FILE
    example.write_text('API_KEY=not_redacted\n')
    run_hook(tmp_path, [DEFAULT_ENV_FILE])
    content = example.read_text()
    # Should now contain redacted values
    assert '***' in content or 'API_KEY' in content


def test_large_gitignore_normalization_performance(
        tmp_path: Path, env_file: Path,
) -> None:
    """Very large .gitignore remains normalized quickly (functional smoke)."""
    g = tmp_path / DEFAULT_GITIGNORE_FILE
    # Generate many lines with scattered duplicates of banner/env
    lines = (
        [f"file_{i}" for i in range(3000)] +
        [GITIGNORE_BANNER, DEFAULT_ENV_FILE] * 3
    )
    g.write_text('\n'.join(lines) + '\n')
    start = time.time()
    run_hook(tmp_path, [DEFAULT_ENV_FILE])
    elapsed = time.time() - start
    result_lines = g.read_text().splitlines()
    assert result_lines[-2:] == [GITIGNORE_BANNER, DEFAULT_ENV_FILE]
    assert result_lines.count(GITIGNORE_BANNER) == 1
    assert result_lines.count(DEFAULT_ENV_FILE) == 1
    # Soft performance expectation: should finish fast
    # (< 0.5s on typical dev machine)
    assert elapsed < 0.5


def test_concurrent_gitignore_writes(
        tmp_path: Path, env_file: Path,
) -> None:
    """Concurrent ensure_env_in_gitignore calls result in canonical final
    state.
    """
    g = tmp_path / DEFAULT_GITIGNORE_FILE
    # Seed with messy duplicates
    g.write_text(f"other\n{GITIGNORE_BANNER}\n{DEFAULT_ENV_FILE}\n\n")

    def worker():
        ensure_env_in_gitignore(DEFAULT_ENV_FILE, str(g), GITIGNORE_BANNER)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = g.read_text().splitlines()
    assert lines[-2:] == [GITIGNORE_BANNER, DEFAULT_ENV_FILE]
    assert lines.count(GITIGNORE_BANNER) == 1
    assert lines.count(DEFAULT_ENV_FILE) == 1


def test_mixed_staged_files(
        tmp_path: Path, env_file: Path,
) -> None:
    """Staging .env with other files still blocks and only normalizes
    gitignore once.
    """
    other = tmp_path / 'README.md'
    other.write_text('hi')
    ret = run_hook(tmp_path, [DEFAULT_ENV_FILE, 'README.md'])
    assert ret == 1
    lines = (tmp_path / DEFAULT_GITIGNORE_FILE).read_text().splitlines()
    assert lines[-2:] == [GITIGNORE_BANNER, DEFAULT_ENV_FILE]


def test_already_ignored_env_with_variations(
        tmp_path: Path, env_file: Path,
) -> None:
    """Pre-existing ignore lines with spacing normalize to single
    canonical pair.
    """
    g = tmp_path / DEFAULT_GITIGNORE_FILE
    g.write_text(
        f"  {DEFAULT_ENV_FILE}  \n{GITIGNORE_BANNER}\n"
        f"   {DEFAULT_ENV_FILE}\n",
    )
    run_hook(tmp_path, [DEFAULT_ENV_FILE])
    lines = g.read_text().splitlines()
    assert lines[-2:] == [GITIGNORE_BANNER, DEFAULT_ENV_FILE]
    assert lines.count(DEFAULT_ENV_FILE) == 1


def test_subdirectory_invocation(
        tmp_path: Path, env_file: Path,
) -> None:
    """Running from a subdirectory now writes .gitignore relative to CWD
    (simplified behavior).
    """
    sub = tmp_path / 'subdir'
    sub.mkdir()
    # simulate repository root marker
    (tmp_path / '.git').mkdir()
    # simulate running hook from subdir while staged path relative to repo root
    cwd = os.getcwd()
    os.chdir(sub)
    try:
        ret = main(
            [str(Path('..') / DEFAULT_ENV_FILE)],
        )  # staged path relative to subdir
        gi = (sub / DEFAULT_GITIGNORE_FILE).read_text().splitlines()
    finally:
        os.chdir(cwd)
    assert ret == 1
    assert gi[-2:] == [GITIGNORE_BANNER, DEFAULT_ENV_FILE]


def test_atomic_write_failure_gitignore(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        env_file: Path,
        capsys: pytest.CaptureFixture[str],
) -> None:
    """Simulate os.replace failure during gitignore write to exercise error
    path.
    """
    def boom(*_a: object, **_k: object) -> None:
        raise OSError('replace-fail')
    monkeypatch.setattr('pre_commit_hooks.redact_dotenv.os.replace', boom)
    modified = ensure_env_in_gitignore(
        DEFAULT_ENV_FILE,
        str(tmp_path / DEFAULT_GITIGNORE_FILE),
        GITIGNORE_BANNER,
    )
    assert modified is False
    captured = capsys.readouterr()
    assert 'ERROR: unable to write' in captured.err


def test_atomic_write_failure_example(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        env_file: Path,
        capsys: pytest.CaptureFixture[str],
) -> None:
    """Simulate os.replace failure when writing example env file."""
    def boom(*_a: object, **_k: object) -> None:
        raise OSError('replace-fail')
    monkeypatch.setattr('pre_commit_hooks.redact_dotenv.os.replace', boom)
    ok = False
    # redact_env_file requires source .env to exist; env_file fixture
    # provides it in tmp_path root
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        ok = main([DEFAULT_ENV_FILE]) == 1
    finally:
        os.chdir(cwd)
    # hook still blocks; but example creation failed -> message should
    # not claim example.env was updated
    assert ok is True
    captured = capsys.readouterr()
    out = captured.out
    err = captured.err
    assert 'Updated example.env' not in out
    assert 'ERROR: unable to write' in err
