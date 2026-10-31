"""Explicit clean release builder/verifier. No data directory is ever copied.

Build consumes a fixed runtime/package lock, server-maintained source manifest,
two native launcher binaries and signed prerequisite installers. It fails closed
on missing inputs; --verify checks every payload byte and unexpected files.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sys
import zipfile


class ReleaseError(ValueError): pass
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def canonical(name): return re.sub(r'[-_.]+', '-', name).lower()


def select_source_files(source, manifest):
    source = Path(source).resolve(); paths = set()
    for pattern in manifest['source_globs']: paths.update(source.glob(pattern))
    for name in manifest['source_files']:
        path = source / name
        if not path.is_file(): raise ReleaseError('required source missing: ' + name)
        paths.add(path)
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ReleaseError('source path escaped root')
        if 'data' in path.relative_to(source).parts or '.git' in path.parts or '__pycache__' in path.parts:
            raise ReleaseError('customer/developer data entered source allowlist')
    return sorted(path for path in paths if path.is_file())


def scan_clean(root):
    root = Path(root).resolve()
    for path in root.rglob('*'):
        if path.is_symlink() or not path.resolve().is_relative_to(root): raise ReleaseError('release contains linked path')
        relative = path.relative_to(root)
        if relative.parts[0] in ('data', '.git', '.venv') or path.name in ('session_secret', 'config.yaml', 'private.pem'):
            raise ReleaseError('customer data is forbidden in release: ' + str(relative))
        if path.is_file() and path.suffix.lower() in ('.py', '.json', '.yaml', '.html', '.ps1', '.pth'):
            raw = path.read_bytes()
            if b'127.0.0.1:7897' in raw or re.search(rb'(?:[A-Za-z]:[\\/]Users[\\/]Administrator[\\/]|[Ee]:[\\/]order-bot)', raw):
                raise ReleaseError('developer path/proxy leaked into payload: ' + str(relative))


def write_inventory(root, version):
    root = Path(root); scan_clean(root)
    files = {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob('*'))
             if p.is_file() and p.name != 'release-inventory.json'}
    value = {'schema': 1, 'version': version, 'files': files}
    (root / 'release-inventory.json').write_text(json.dumps(value, sort_keys=True, indent=2), encoding='utf-8')
    return value


def verify_inventory(root):
    root = Path(root).resolve(); scan_clean(root)
    value = json.loads((root / 'release-inventory.json').read_text(encoding='utf-8'))
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p.name != 'release-inventory.json'}
    if actual != set(value['files']): raise ReleaseError('release has missing or unlisted files')
    for name, digest in value['files'].items():
        if sha(root / name) != digest: raise ReleaseError('release hash mismatch: ' + name)
    return {'status': 'ok', 'version': value['version'], 'file_count': len(actual)}


def make_runtime_lock(site_packages, output):
    site = Path(site_packages).resolve(); packages = []
    for dist in importlib.metadata.distributions(path=[str(site)]):
        if canonical(dist.metadata['Name']) == 'pip': continue
        files = {}
        for entry in dist.files or []:
            relative = Path(str(entry))
            if relative.is_absolute() or '..' in relative.parts or relative.suffix == '.pyc' or '__pycache__' in relative.parts:
                continue
            if relative.name in ('direct_url.json', 'INSTALLER', 'RECORD', 'REQUESTED'): continue
            path = site / relative
            if path.is_file(): files[relative.as_posix()] = sha(path)
        if not files: raise ReleaseError('distribution has no recorded runtime files')
        packages.append({'name': dist.metadata['Name'], 'version': dist.version,
                         'license': dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'See bundled distribution metadata/licence files',
                         'files': dict(sorted(files.items()))})
    Path(output).write_text(json.dumps({'schema': 1, 'python': sys.version.split()[0],
        'packages': sorted(packages, key=lambda x: canonical(x['name']))}, sort_keys=True, indent=2), encoding='utf-8')


def _copy(source, target):
    if not Path(source).is_file() or Path(source).is_symlink(): raise ReleaseError('required file missing/linked: ' + str(source))
    Path(target).parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)


def build_release(source, destination, version, *, runtime_root, site_packages, launcher,
                  sandbox_launcher=None, prerequisites=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    runtime_root, site = Path(runtime_root).resolve(), Path(site_packages).resolve()
    launcher = Path(launcher)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', version): raise ReleaseError('invalid version')
    if not runtime_root.joinpath('python.exe').is_file() or not launcher.is_file(): raise ReleaseError('missing native launcher or standalone Python runtime')
    if not sandbox_launcher or not Path(sandbox_launcher).is_file(): raise ReleaseError('missing AppContainer native launcher')
    if destination.exists(): raise ReleaseError('output already exists; choose a new staging directory')
    manifest = json.loads(source.joinpath('packaging/release_manifest.json').read_text(encoding='utf-8'))
    lock = json.loads(source.joinpath('packaging/runtime-lock.json').read_text(encoding='utf-8'))
    selected = select_source_files(source, manifest)
    if not runtime_root.joinpath('LICENSE.txt').is_file(): raise ReleaseError('missing Python licence')
    prerequisites = Path(prerequisites or '')
    for name in manifest['prerequisites']:
        if not prerequisites.joinpath(name).is_file(): raise ReleaseError('missing signed prerequisite installer: ' + name)
    for name in manifest.get('license_files', []):
        if not source.joinpath(name).is_file(): raise ReleaseError('missing redistribution notice: ' + name)
    destination.mkdir(parents=True)
    release = destination / 'releases' / version
    for path in selected: _copy(path, release / path.relative_to(source))
    _copy(launcher, destination / 'OrderBot.exe')
    _copy(sandbox_launcher, destination / 'agent-tools/launcher/AppContainerLauncher.exe')
    _copy(source / 'packaging/bootstrap.py', destination / 'bootstrap.py')
    notices = [{'name': 'Python', 'version': lock['python'], 'license_file': 'runtime/LICENSE.txt', 'source': manifest['official_sources']['python']},
               {'name': 'WeCom Finance SDK', 'license': 'Official enterprise WeChat SDK terms; native binary supplied with project',
                'source': 'https://developer.work.weixin.qq.com/document/path/91774'},
               {'name': 'OpenSSL 3', 'license_file': 'licenses/OpenSSL-LICENSE.txt',
                'source': 'https://github.com/openssl/openssl/blob/openssl-3.0/LICENSE.txt'},
               {'name': 'curl', 'license_file': 'licenses/curl-COPYING.txt',
                'source': 'https://github.com/curl/curl/blob/master/COPYING'},
               {'name': 'Microsoft WebView2 / Visual C++ Runtime', 'license': 'Microsoft redistributable terms', 'sources': [manifest['official_sources']['webview2'], manifest['official_sources']['vc_runtime']]}]
    for runtime_name in ('runtime', 'worker-runtime'):
        runtime = destination / runtime_name
        for name in manifest['runtime_files']: _copy(runtime_root / name, runtime / name)
        for path in sorted(runtime_root.joinpath('DLLs').glob('*')):
            if path.is_file() and path.suffix.lower() in ('.dll', '.pyd'): _copy(path, runtime / 'DLLs' / path.name)
        archive = runtime / 'python312.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
            for path in sorted(runtime_root.joinpath('Lib').rglob('*.py')):
                rel = path.relative_to(runtime_root / 'Lib')
                if set(rel.parts) & {'site-packages', '__pycache__', 'test', 'tests', 'idlelib', 'turtledemo', 'ensurepip'}: continue
                info = zipfile.ZipInfo(rel.as_posix(), date_time=(2000,1,1,0,0,0)); info.compress_type = zipfile.ZIP_DEFLATED
                zipped.writestr(info, path.read_bytes())
        pth = 'python312.zip\nDLLs\n.\n' + ('Lib/site-packages\nimport site\n' if runtime_name == 'runtime' else '')
        runtime.joinpath('python312._pth').write_text(pth, encoding='ascii')
    for package in lock['packages']:
        for name, digest in package['files'].items():
            path = site / name
            if not path.is_file() or sha(path) != digest: raise ReleaseError('runtime lock mismatch: ' + package['name'] + '/' + name)
            _copy(path, destination / 'runtime/Lib/site-packages' / name)
        notices.append({key: package[key] for key in ('name','version','license')})
    # pywin32's wheel uses a .pth bootstrap. Paths are relative to this package,
    # never developer installations. Ship all recorded wheel DLLs/metadata.
    for name in manifest['prerequisites']: _copy(prerequisites / name, destination / 'prerequisites' / name)
    for name in manifest.get('license_files', []): _copy(source / name, destination / 'licenses' / Path(name).name)
    release_files = {p.relative_to(release).as_posix(): sha(p) for p in sorted(release.rglob('*')) if p.is_file()}
    release.joinpath('release-files.json').write_text(json.dumps({'version': version, 'files': release_files}, sort_keys=True), encoding='utf-8')
    destination.joinpath('current.json').write_text(json.dumps({'current': version}), encoding='utf-8')
    destination.joinpath('THIRD-PARTY-NOTICES.json').write_text(json.dumps(notices, sort_keys=True, indent=2), encoding='utf-8')
    for path in destination.rglob('*'):
        if path.is_file(): os.utime(path, (946684800, 946684800))
    write_inventory(destination, version)
    return verify_inventory(destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', type=Path); parser.add_argument('--lock', type=Path)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path); parser.add_argument('--version')
    parser.add_argument('--runtime', type=Path); parser.add_argument('--site-packages', type=Path)
    parser.add_argument('--launcher', type=Path); parser.add_argument('--sandbox-launcher', type=Path)
    parser.add_argument('--prerequisites', type=Path)
    args = parser.parse_args()
    if args.verify: outcome = verify_inventory(args.verify)
    elif args.lock:
        make_runtime_lock(args.site_packages, args.lock); outcome = {'status': 'locked'}
    else:
        outcome = build_release(args.source, args.output, args.version, runtime_root=args.runtime,
            site_packages=args.site_packages, launcher=args.launcher,
            sandbox_launcher=args.sandbox_launcher, prerequisites=args.prerequisites)
    print(json.dumps(outcome, ensure_ascii=False))

if __name__ == '__main__': main()
