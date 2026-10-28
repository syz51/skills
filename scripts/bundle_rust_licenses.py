#!/usr/bin/env python3
"""Bundle the actual locked dependencies' notices with distributed Rust binaries."""
import argparse
import json
from pathlib import Path
import subprocess


def bundle(metadata):
    sections = []
    root_id = metadata['resolve']['root']
    nodes = {node['id']: node for node in metadata['resolve'].get('nodes', [])}
    included = set()
    pending = [root_id]
    while pending:
        current = pending.pop()
        if current in included:
            continue
        included.add(current)
        pending.extend(dep['pkg'] for dep in nodes.get(current, {}).get('deps', []))
    for package in sorted(metadata['packages'], key=lambda p: (p['name'], p['version'])):
        if package['id'] == root_id or nodes and package['id'] not in included:
            continue
        root = Path(package['manifest_path']).resolve().parent
        candidates = {p for p in root.iterdir() if p.is_file()
                      and p.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE'))}
        if package.get('license_file'):
            candidates.add(root / package['license_file'])
        for directory in ('LICENSES', 'licenses'):
            folder = root / directory
            if folder.is_dir():
                candidates.update(p for p in folder.rglob('*') if p.is_file())
        if not candidates:
            raise ValueError('Dependency lacks bundled license evidence: ' + package['name'])
        sections.append(f"{package['name']} {package['version']} ({package.get('license') or 'see notice'})\n")
        for path in sorted(candidates):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or resolved.stat().st_size > 1024 * 1024:
                raise ValueError('Unsafe dependency notice: ' + package['name'])
            sections.append(f"--- {path.relative_to(root).as_posix()} ---\n" + resolved.read_text(encoding='utf-8') + '\n')
    return '\n'.join(sections)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target', help='Rust target triple; defaults to the current toolchain host')
    args = parser.parse_args()
    target = args.target
    if target is None:
        details = subprocess.check_output(['rustc', '-vV'], text=True, timeout=30)
        target = next(line.removeprefix('host: ') for line in details.splitlines() if line.startswith('host: '))
    metadata = json.loads(subprocess.check_output(['cargo', 'metadata', '--locked', '--filter-platform', target, '--format-version', '1'], timeout=180))
    args.output.write_text(bundle(metadata), encoding='utf-8')


if __name__ == '__main__':
    main()
