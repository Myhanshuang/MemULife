"""Identify literal Git invocations without evaluating shell or Codex wrapper code."""
import ast
import re
import shlex

from .local_ai_events import parsed

# Python's expression parser also accepts the literal call/object subset used
# by these Codex wrappers. Only inspect explicitly supported evaluation paths.
def _commands(node):
    if isinstance(node, ast.Await):
        yield from _commands(node.value)
    elif isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        if name == 'tools.exec_command' and len(node.args) == 1 and isinstance(node.args[0], ast.Dict):
            args = node.args[0]
            fields = {k.id if isinstance(k, ast.Name) else getattr(k, 'value', None): v
                      for k, v in zip(args.keys, args.values)}
            cmd = fields.get('cmd')
            if isinstance(cmd, ast.Constant) and isinstance(cmd.value, str):
                yield cmd.value
        elif name in {'text', 'notify', 'Promise.all', 'Promise.allSettled'}:
            for arg in node.args:
                yield from _commands(arg)
    elif isinstance(node, ast.List):
        for item in node.elts:
            yield from _commands(item)


def git_operations(call):
    tool = call.get('tool', '').split('.')[-1].lower()
    args = parsed(call['body'])
    commands = []
    if tool in {'bash', 'shell', 'exec_command', 'shell_command', 'run_shell_command'} and isinstance(args, dict):
        commands = [args.get('cmd', args.get('command'))]
    elif tool == 'exec' and isinstance(args, str) and len(args) <= 32000:
        try:
            tree = ast.parse(args.strip().rstrip(';'), mode='eval')
            commands = list(_commands(tree.body))
        except (SyntaxError, RecursionError, ValueError):
            return []
    operations = []
    for command in commands:
        if not isinstance(command, str) or any(x in command for x in ('<<', '`', '$(', '\n')):
            continue
        # Split on real shell operators, preserving quoted literals as one token.
        tokens = re.findall(r"""(?:[^\s;&|'"\\]|\\.|'(?:[^']*)'|"(?:\\.|[^"\\])*")+|[;&|]+""", command)
        segment = []
        for token in tokens + [';']:
            if token.startswith('#'):
                break
            if token[0] not in ';&|':
                segment.append(token)
                continue
            if segment and segment[0] in {'git', '/usr/bin/git'}:
                try:
                    words = shlex.split(' '.join(segment))
                except ValueError:
                    break
                pos = 1
                while pos < len(words) and words[pos].startswith('-'):
                    pos += 2 if words[pos] in {'-C', '-c', '--git-dir', '--work-tree', '--namespace'} else 1
                if pos < len(words) and re.fullmatch('[a-z][a-z0-9-]{0,63}', words[pos]):
                    operations.append('git ' + words[pos])
            segment = []
    return operations
