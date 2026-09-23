"""Fail closed on known private identifiers, local paths and credential shapes."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {'.git', '.venv', '__pycache__', '.pytest_cache', '.ruff_cache',
           'data', 'secrets', 'tmp', 'cache', 'dist', 'build', 'validation'}
PRIVATE_WORD_HASHES = {
    '0f937e60148316557dbe0dce3e862e77d4d8157bfea24ae4b4c1fa367297c2d9',
    '8cfde6efdfc4ed5ab1f6acbbd1ba49bf31932f84d0a4c090eb41c7d151e8b180',
    '59458508a0827cff5f80ed091ebd8808fbe67c97357b58ca00a278e7359dec20',
    '42388b56e53eb0ec8097e1711c07519e48c607a575a70725a3af3000b747ba45',
    'a6355c4b22aebd011dc2080fdaa38f862db908ef2543b71bce6835c647bdfd66',
}
PRIVATE_PHRASE_HASHES = {
    'd60c4449fc37b3accde5367778b1b1a943db7231098ffe74228ad606c3bee4c7',
    '81a2d45d98cefb89a4f1ac16692261d23aebb252410fee403adce287693ad852',
    '6f71636a40a9b16a87b6c7d99b38936687e64652ccb2587ee152daaac6f355c0',
    '939ef890f460e806fad0faf70945f539b8f5e7556fb796aee77056e8b10d7a5e',
    'c9e6db1f368b785550ac58e75e73d7a35b95ff67af8fbdd926ea3e45f4a9cfe0',
}
SECRET_PATTERNS = [
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'AIza[0-9A-Za-z_-]{30,}'),
    re.compile(r'gh[pousr]_[0-9A-Za-z]{30,}'),
    re.compile(r'ya29\.[0-9A-Za-z._-]{30,}'),
    re.compile(r'sk-[0-9A-Za-z]{30,}'),
    re.compile(r'"(?:client_secret|refresh_token|access_token|api_key)"\s*:\s*"(?!PLACEHOLDER|YOUR_)[^"\s]{8,}"'),
]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _issues(content: str) -> list[str]:
    issues = []
    lower = content.casefold()
    words = re.findall(r'[a-z0-9]+', lower)
    if any(_digest(word) in PRIVATE_WORD_HASHES for word in words):
        issues.append('private identifier')
    if any(_digest(' '.join(words[i:i+size])) in PRIVATE_PHRASE_HASHES
           for size in (2, 3, 4) for i in range(max(0, len(words)-size+1))):
        issues.append('private phrase')
    if re.search(r'/(?:Users|home)/[A-Za-z0-9._-]+/', content):
        issues.append('absolute user path')
    if re.search(r'\b[Mm][2-7](?:\b|[_-])', content):
        issues.append('internal milestone')
    emails = re.findall(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', content)
    if any(not value.lower().endswith(('@example.org', '@example.com', '@example.net')) for value in emails):
        issues.append('email address')
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        issues.append('credential-shaped value')
    return issues


def _files() -> list[Path]:
    return [p for p in ROOT.rglob('*') if p.is_file() and not any(part in IGNORED for part in p.relative_to(ROOT).parts)
            and not p.name.endswith('.pyc')]


def main() -> int:
    failures = []
    files = _files()
    for path in files:
        data = path.read_bytes()
        rel = str(path.relative_to(ROOT))
        if b'\0' in data:
            failures.append((rel, 'unreviewed binary'))
            continue
        for issue in _issues(data.decode('utf-8', errors='replace')):
            failures.append((rel, issue))
    if (ROOT / '.git').exists():
        history = subprocess.run(['git', 'log', '--all', '-p', '--pretty=format:'], cwd=ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        for issue in _issues(history.decode('utf-8', errors='replace')):
            failures.append(('<git history>', issue))
    for rel, issue in failures:
        print(f'FAIL {rel}: {issue}')
    print(f'PUBLIC_SCAN={"FAIL" if failures else "PASS"}; files={len(files)}; findings={len(failures)}')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
