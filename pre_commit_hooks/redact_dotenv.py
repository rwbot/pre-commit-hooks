#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections.abc import Sequence

# Defaults / constants
DEFAULT_ENV_FILE = '.env'
DEFAULT_GITIGNORE_FILE = '.gitignore'
DEFAULT_EXAMPLE_ENV_FILE = 'example.env'
GITIGNORE_BANNER = '# Added by pre-commit hook to prevent committing secrets'

# Regex patterns
_KEY_VALUE_REGEX = re.compile(
    r'^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*)(.*)$',
)
_VAR_REF_REGEX = re.compile(r'\$\{[^}]+\}')
_OFFSET_REGEX = re.compile(r'#.*?\[([^\]]*)\]')
_URL_SCHEMA_REGEX = re.compile(r'^(https?://|postgresql://|mongodb://)')


def _atomic_write(path: str, data: str) -> None:
    """Atomically write text to file."""
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path) or '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as tmp_f:
            tmp_f.write(data)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _read_gitignore(gitignore_file: str) -> tuple[str, list[str]]:
    """Read and parse .gitignore file content."""
    try:
        if os.path.exists(gitignore_file):
            with open(gitignore_file, encoding='utf-8') as f:
                original_text = f.read()
            lines = original_text.splitlines()
        else:
            original_text = ''
            lines = []
    except OSError as exc:
        print(
            f"ERROR: unable to read {gitignore_file}: {exc}",
            file=sys.stderr,
        )
        raise
    return original_text, lines


def _normalize_gitignore_lines(
    lines: list[str],
    env_file: str,
    banner: str,
) -> list[str]:
    """Normalize .gitignore lines by removing duplicates and canonical tail."""
    # Trim trailing blank lines
    while lines and not lines[-1].strip():
        lines.pop()

    # Remove existing occurrences
    filtered: list[str] = [
        ln for ln in lines if ln.strip() not in {env_file, banner}
    ]

    if filtered and filtered[-1].strip():
        filtered.append('')  # ensure single blank before banner
    elif not filtered:
        filtered.append('')

    filtered.append(banner)
    filtered.append(env_file)
    return filtered


def ensure_env_in_gitignore(
    env_file: str,
    gitignore_file: str,
    banner: str,
) -> bool:
    """Ensure canonical banner + env tail in .gitignore."""
    try:
        original_content_str, lines = _read_gitignore(gitignore_file)
    except OSError:
        return False

    filtered = _normalize_gitignore_lines(lines, env_file, banner)
    new_content = '\n'.join(filtered) + '\n'

    # Normalize original content to a single trailing newline for comparison
    normalized_original = original_content_str
    if normalized_original and not normalized_original.endswith('\n'):
        normalized_original += '\n'
    if new_content == normalized_original:
        return False

    try:
        _atomic_write(gitignore_file, new_content)
        return True
    except OSError as exc:
        print(
            f"ERROR: unable to write {gitignore_file}: {exc}",
            file=sys.stderr,
        )
        return False


def _should_redact_key(key: str) -> bool:
    """Check if key name suggests it should be redacted."""
    key_upper = key.upper()
    return any(
        keyword in key_upper
        for keyword in ['PASSWORD', 'API', 'KEY', 'TOKEN', 'SECRET']
    )


def _extract_offset_from_comment(comment: str) -> str | None:
    """Extract offset specifier from comment (e.g., [3:], [:3], [10:], [:])."""
    match = _OFFSET_REGEX.search(comment)
    if match:
        return match.group(1)
    return None


def _parse_offset(offset_str: str) -> tuple[int | None, int | None]:
    """Parse offset string like '3:', ':3', '10:', ':', '*' into (start, end).

    Returns (start, end) where None means unbounded.
    Examples:
      '3:' -> (3, None) - keep first 3, redact rest
      ':3' -> (None, 3) - redact all except last 3
      '10:' -> (10, None) - keep first 10, redact rest
      ':' or '*' -> (None, None) - redact all
    """
    offset_str = offset_str.strip()
    if offset_str in (':', '*', ''):
        return (None, None)

    if ':' not in offset_str:
        return (None, None)

    parts = offset_str.split(':', 1)
    start = int(parts[0]) if parts[0] else None
    end = int(parts[1]) if parts[1] else None
    return (start, end)


def _redact_value(value: str, start: int | None, end: int | None) -> str:
    """Redact value based on offset.

    Args:
        value: The value to redact
        start: Keep first N chars (None = redact from beginning)
        end: Keep last N chars (None = redact to end)

    Examples:
        ('secret123', 3, None) -> 'sec*******'
        ('secret123', None, 3) -> '******123'
        ('secret123', None, None) -> '*********'
    """
    if not value:
        return value

    if start is None and end is None:
        # Redact all
        return '*' * len(value)
    elif start is not None and end is None:
        # Keep first N, redact rest (e.g., [3:])
        if start >= len(value):
            return value
        return value[:start] + '*' * (len(value) - start)
    elif start is None and end is not None:
        # Redact all except last N (e.g., [:3])
        if end >= len(value):
            return value
        redact_count = len(value) - end
        return '*' * redact_count + value[-end:]
    else:
        # Both specified - shouldn't happen with our syntax, treat as redact all
        return '*' * len(value)


def _redact_line(line: str) -> str:
    """Redact a single .env line according to the rules."""
    # Check if line matches KEY=VALUE pattern
    match = _KEY_VALUE_REGEX.match(line)
    if not match:
        # Not a KEY=VALUE line, return as-is
        return line

    prefix = match.group(1)  # KEY= part with spacing
    rest = match.group(2)     # VALUE and optional comment

    # Extract key name from prefix
    key_match = re.search(r'([A-Za-z_][A-Za-z0-9_]*)', prefix)
    if not key_match:
        return line
    key = key_match.group(1)

    # Split rest into value and comment
    # Handle both inline comments and no comments
    value = rest
    comment = ''

    # Find comment (starts with #)
    comment_idx = rest.find('#')
    if comment_idx != -1:
        value = rest[:comment_idx]
        comment = rest[comment_idx:]

    # Check if we should NOT redact
    if comment and 'DNR' in comment.upper():
        return line

    # Check if value contains variable reference
    if _VAR_REF_REGEX.search(value):
        return line

    # Check if we should redact
    should_redact = False
    if comment and 'REDACT' in comment.upper():
        should_redact = True
    elif _should_redact_key(key):
        should_redact = True

    if not should_redact:
        return line

    # Determine redaction offset
    offset_str = None
    if comment:
        offset_str = _extract_offset_from_comment(comment)

    # Strip value for processing
    value_stripped = value.rstrip()
    trailing_space = value[len(value_stripped):]

    # Remove quotes if present
    value_clean = value_stripped.strip('"').strip("'").strip()

    # Check for URL and preserve schema
    url_match = _URL_SCHEMA_REGEX.match(value_clean)
    url_prefix = ''
    if url_match:
        url_prefix = url_match.group(1)
        value_clean = value_clean[len(url_prefix):]

    # Determine offset to use
    if offset_str:
        start, end = _parse_offset(offset_str)
    elif 'PASSWORD' in key.upper():
        # PASSWORD -> redact all
        start, end = None, None
    else:
        # Default for API/KEY/TOKEN/SECRET -> [3:]
        start, end = 3, None

    # Redact the value
    redacted = _redact_value(value_clean, start, end)

    # Reconstruct with URL prefix if present
    if url_prefix:
        redacted = url_prefix + redacted

    # Reconstruct the line
    # Check if original value was quoted
    if value_stripped.startswith('"') and value_stripped.endswith('"'):
        redacted = f'"{redacted}"'
    elif value_stripped.startswith("'") and value_stripped.endswith("'"):
        redacted = f"'{redacted}'"

    return prefix + redacted + trailing_space + comment


def redact_env_file(src_path: str, dest_path: str) -> bool:
    """Redact .env file and write to destination."""
    try:
        with open(src_path, encoding='utf-8') as f:
            content = f.read()
    except OSError as exc:
        print(f"ERROR: unable to read {src_path}: {exc}", file=sys.stderr)
        return False

    # Process line by line to preserve all formatting
    lines = content.splitlines(keepends=True)
    redacted_lines = [_redact_line(line.rstrip('\r\n')) for line in lines]

    # Reconstruct with original line endings
    result = []
    for i, redacted in enumerate(redacted_lines):
        if i < len(lines):
            # Preserve original line ending
            orig_line = lines[i]
            if orig_line.endswith('\r\n'):
                result.append(redacted + '\r\n')
            elif orig_line.endswith('\n'):
                result.append(redacted + '\n')
            elif orig_line.endswith('\r'):
                result.append(redacted + '\r')
            else:
                result.append(redacted)
        else:
            result.append(redacted)

    result_str = ''.join(result)

    try:
        _atomic_write(dest_path, result_str)
        return True
    except OSError as exc:
        print(
            f"ERROR: unable to write '{dest_path}': {exc}",
            file=sys.stderr,
        )
        return False


def _file_needs_redaction(file_path: str) -> bool:
    """Check if file contains any non-redacted values that should be redacted."""
    try:
        with open(file_path, encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return False

    for line in lines:
        redacted = _redact_line(line.rstrip('\n'))
        if redacted != line.rstrip('\n'):
            return True

    return False


def _has_env(filenames: list[str], env_file: str) -> bool:
    """Return True if any staged path refers to target env file by basename."""
    return any(os.path.basename(name) == env_file for name in filenames)


def _print_failure(
    env_file: str,
    gitignore_file: str,
    example_created_or_updated: bool,
    gitignore_modified: bool,
) -> None:
    """Print failure message."""
    print(f"Blocked committing {env_file}.")
    if gitignore_modified:
        print(f"Updated {gitignore_file}.")
    if example_created_or_updated:
        print(f'Updated {DEFAULT_EXAMPLE_ENV_FILE}.')
    print(f"Remove {env_file} from the commit and retry.")


def main(argv: Sequence[str] | None = None) -> int:
    """Hook entry-point."""
    parser = argparse.ArgumentParser(
        description='Blocks committing .env files and maintains redacted example.env.',
    )
    parser.add_argument(
        'filenames',
        nargs='*',
        help='Staged filenames (supplied by pre-commit).',
    )
    args = parser.parse_args(argv)

    env_file = DEFAULT_ENV_FILE
    repo_root = os.getcwd()
    gitignore_file = os.path.join(repo_root, DEFAULT_GITIGNORE_FILE)
    example_file = os.path.join(repo_root, DEFAULT_EXAMPLE_ENV_FILE)
    env_abspath = os.path.join(repo_root, env_file)

    # Check if .env is staged
    env_staged = _has_env(args.filenames, env_file)

    # Handle example.env
    example_created_or_updated = False
    if os.path.exists(env_abspath):
        if os.path.exists(example_file):
            # example.env exists - check if it needs redaction
            if _file_needs_redaction(example_file):
                example_created_or_updated = redact_env_file(env_abspath, example_file)
        else:
            # example.env does not exist - create it
            example_created_or_updated = redact_env_file(env_abspath, example_file)

    # If .env is staged, block the commit
    if env_staged:
        gitignore_modified = ensure_env_in_gitignore(
            env_file,
            gitignore_file,
            GITIGNORE_BANNER,
        )

        _print_failure(
            env_file,
            gitignore_file,
            example_created_or_updated,
            gitignore_modified,
        )
        return 1

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
