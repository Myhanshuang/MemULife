"""Create fresh deployment credentials without printing them or overwriting files."""
import argparse
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.env'))
    args = parser.parse_args()
    template = Path(__file__).resolve().parents[1] / 'deploy/compose.env.example'
    content = template.read_text()
    for name in ('DATABASE_PASSWORD', 'OWNER_TOKEN', 'AGENT_TOKEN'):
        content = content.replace('GENERATE_' + name, secrets.token_hex(32))
    with args.output.open('x', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)
    print(f'Created {args.output} (0600). Configure model endpoints before starting. Credentials were not printed.')


if __name__ == '__main__':
    main()
