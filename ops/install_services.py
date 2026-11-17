"""Generate relocatable local systemd units; containers use the same CLI directly."""
import argparse
from pathlib import Path
import subprocess
import sys


def quoted(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--core-python', default=str(root / '.venv-embedding/bin/python'))
    parser.add_argument('--mcp-python', default=str(root / '.venv/bin/python'))
    args = parser.parse_args()
    units = root / '.local/systemd'
    units.mkdir(parents=True, exist_ok=True)
    for role in ['core', 'mcp']:
        interpreter = Path(getattr(args, role + '_python')).absolute()
        if not interpreter.is_file():
            raise SystemExit(f'Missing Python interpreter for {role}; specify --{role}-python')
        core = role == 'core'
        (units / f'memu-{role}.service').write_text(f'''[Unit]
Description=MemULife independent {role} service
After=network.target

[Service]
Type=notify
NotifyAccess=main
WorkingDirectory={str(root).replace('%', '%%')}
ExecStart={quoted(interpreter)} -m memu {role}
Environment=PYTHONUNBUFFERED=1
Environment={quoted('MEMU_ENV_FILE=' + str(root / '.env'))}
MemoryMax={'3G' if core else '256M'}
MemoryHigh={'2800M' if core else '192M'}
MemorySwapMax=0
CPUQuota={'200%' if core else '50%'}
TasksMax={'96' if core else '32'}
OOMPolicy=stop
KillMode=control-group
TimeoutStartSec=90
TimeoutStopSec={'900' if core else '15'}
Restart=no
UMask=0077

[Install]
WantedBy=default.target
''')
    for role in ['core', 'mcp']:
        subprocess.run(['systemctl', '--user', 'link', '--force', str(units / f'memu-{role}.service')], check=True)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    # Stop the old frontend before handing repository ownership to the independent core.
    subprocess.run(['systemctl', '--user', 'stop', 'memu-mcp.service'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', 'memu-core.service', 'memu-mcp.service'], check=True)
    subprocess.run(['systemctl', '--user', 'restart', 'memu-core.service'], check=True)
    subprocess.run(['systemctl', '--user', 'start', 'memu-mcp.service'], check=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
