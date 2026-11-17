"""Describe tool activity without feeding source code, shell transcripts or polls to memory."""
from pathlib import PurePosixPath
import re
import shlex

from .local_ai_events import parsed, REFERENCE_TOOLS

ANSWER_PREFIXES = ('User has answered your questions:', 'Your questions have been answered:')
READ_COMMANDS = {'ls', 'pwd', 'find', 'head', 'tail', 'wc', 'rg', 'grep', 'cat', 'sed', 'awk',
                 'stat', 'du', 'df', 'ps', 'top', 'free', 'nvidia-smi', 'which', 'type', 'whoami',
                 'hostname', 'uname', 'readlink', 'lsblk', 'sleep', 'echo', 'printf', 'true', 'test', '['}
SHELL_TOOLS = {'bash', 'shell', 'exec_command', 'shell_command', 'run_shell_command'}


def shell_kind(command, depth=0):
    """A parser for classification only. Never execute or expand command text."""
    if not isinstance(command, str) or depth > 4:
        return 'unknown'
    if re.search(r'\b(?:until|while)\b', command) and not re.search(r'\b(?:python\d*|train|pytest|pip\d*|cp|scp|mv|rm)\b', command):
        return 'poll'
    # Redirects/substitutions can change semantics; keep only a bounded outcome, never assume read-only.
    if re.search(r'(?<!2)>(?!&1)|\$\(|`|\s-(?:exec|delete|ok)\b', command):
        return 'unknown'
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|')
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return 'unknown'
    segments, part = [], []
    for word in words + [';']:
        if word in {';', '&&', '||', '|'}:
            if part:
                segments.append(part); part = []
        else:
            part.append(word)
    kinds = []
    for args in segments:
        while args and re.match(r'^[A-Za-z_]\w*=', args[0]):
            args = args[1:]
        if not args:
            continue
        executable = PurePosixPath(args[0]).name
        if executable in {'env', 'sudo', 'timeout'}:
            pos = 1
            while pos < len(args) and (args[pos].startswith('-') or '=' in args[pos] or
                                      (executable == 'timeout' and re.fullmatch(r'\d+[smhd]?', args[pos]))):
                pos += 1
            kinds.append(shell_kind(shlex.join(args[pos:]), depth + 1)); continue
        if executable in {'bash', 'sh', 'zsh'} and any(flag in args for flag in ('-c', '-lc')):
            pos = next(i for i, word in enumerate(args) if word in {'-c', '-lc'})
            kinds.append(shell_kind(args[pos+1] if pos+1 < len(args) else '', depth + 1)); continue
        if executable == 'ssh':
            pos = 1
            while pos < len(args) and args[pos].startswith('-'):
                pos += 2 if args[pos] in {'-p', '-i', '-o', '-J', '-F', '-l'} else 1
            remote = args[pos+1:]
            kinds.append(shell_kind(remote[0] if len(remote) == 1 else shlex.join(remote), depth + 1)); continue
        if executable == 'cd':
            kinds.append('read'); continue
        if executable in READ_COMMANDS:
            # sed -i and awk programs can mutate; do not classify them as simple reads.
            kinds.append('unknown' if (executable == 'sed' and any(a.startswith('-i') for a in args[1:])) or
                         (executable == 'awk' and any(x in command for x in ('system(', 'getline'))) else 'read')
        elif executable in {'scp', 'rsync', 'cp', 'mv', 'mkdir', 'touch', 'rm'}:
            kinds.append('change')
        elif executable in {'pytest', 'ruff', 'mypy', 'make', 'python', 'python3', 'uv', 'pip', 'pip3', 'npm', 'cargo'}:
            kinds.append('work')
        elif executable == 'git':
            kinds.append('read' if len(args) > 1 and args[1] in {'status', 'diff', 'log', 'show', 'ls-files', 'rev-parse'} else 'change')
        else:
            kinds.append('unknown')
    return 'read' if kinds and all(kind == 'read' for kind in kinds) else ('poll' if kinds and all(kind in {'read','poll'} for kind in kinds) else 'work')


def classify(call):
    tool = call.get('tool', '').lower().split('.')[-1]
    if tool in REFERENCE_TOOLS:
        return 'read'
    if tool in SHELL_TOOLS:
        args = parsed(call['body'])
        return shell_kind(args.get('command', args.get('cmd', ''))) if isinstance(args, dict) else 'unknown'
    return 'change' if tool in {'edit', 'write', 'apply_patch', 'multiedit'} else 'work'


def describe(call):
    if not call:
        return ''
    args = parsed(call['body'])
    if not isinstance(args, dict):
        return call.get('tool', '')
    tool = call.get('tool', '')
    path = args.get('file_path', args.get('path'))
    if path and tool.lower() in {'edit', 'write', 'multiedit'}:
        return f'{tool} {str(path)[:240]}'  # Code/diff bytes stay exclusively in the raw audit.
    return tool + (': ' + str(args['description'])[:160] if args.get('description') else '')


def result_excerpt(body, limit=220):
    value = parsed(body)
    if isinstance(value, dict):
        body = str(value.get('output', value.get('stdout', value.get('result', body))))
    lines = [line for line in body.splitlines() if line.strip() and not re.match(
        r'^(?:Chunk ID:|Wall time:|Original token count:|Session ID:|\s*\d+%\|)', line)]
    notable = [line for line in lines if re.search(r'(?i)error|failed|traceback|conflict|success|passed|accuracy|loss[=:]|f1[=:]|created|updated|saved', line)]
    summary = '\n'.join((notable[:2] + notable[-2:]) if len(notable) > 4 else (notable or lines[-3:]))
    return summary if len(summary) <= limit else summary[:limit//2] + '〔节选；完整结果留在审计〕' + summary[-limit//2:]
