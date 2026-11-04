"""Install pinned Chrome for Testing bytes at Playwright's full/headless paths.

This runs only while building a new immutable browser image. The package's
original browsers.json remains intact; provenance distinguishes its cache
revision and declared version from the replacement binaries actually installed.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def extract_verified_archive(archive, expected_digest, destination, root):
    """Authenticate bytes and reject paths/types outside the expected root."""
    if digest_file(archive) != expected_digest:
        raise ValueError('Chrome for Testing SHA-256 mismatch')
    destination = Path(destination)
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            name = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (name.is_absolute() or '..' in name.parts or not name.parts
                    or name.parts[0] != root or '\\' in entry.filename
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError('Unexpected Chrome for Testing archive entry')
        for entry in source.infolist():
            target = destination.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as incoming, target.open('wb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                target.chmod((entry.external_attr >> 16) & 0o777 or 0o644)


def browser_packages(node_modules):
    """Include the CLI alpha core and the library's nested stable core."""
    packages = []
    for metadata in sorted(Path(node_modules).rglob('package.json')):
        if metadata.parent.name != 'playwright-core':
            continue
        package = metadata.parent
        manifest = json.loads((package / 'browsers.json').read_text())
        declared = {item['name']: item for item in manifest['browsers']}
        packages.append({'path': str(package), 'version': json.loads(metadata.read_text())['version'],
                         'declared': {name: declared[name] for name in ('chromium', 'chromium-headless-shell')}})
    if not packages:
        raise ValueError('No installed Playwright core manifests')
    return packages


def wrap_full_browser(executable):
    """Give only Chrome a private config directory on a read-only runtime."""
    executable = Path(executable)
    vendor = executable.with_name(executable.name + '.vendor')
    if vendor.exists():
        raise ValueError('Browser vendor executable already exists')
    vendor_sha256 = digest_file(executable)
    executable.rename(vendor)
    executable.write_text(
        '#!/bin/sh\n'
        'XDG_CONFIG_HOME=$(mktemp -d /tmp/openbench-chrome-config.XXXXXX) || exit 1\n'
        'export XDG_CONFIG_HOME\n'
        'exec ' + shlex.quote(str(vendor)) + ' "$@"\n')
    executable.chmod(0o755)
    return {'vendor_executable': str(vendor), 'vendor_executable_sha256': vendor_sha256,
            'launcher_sha256': digest_file(executable),
            'private_config': 'per-launch mode-0700 temporary directory under /tmp'}


def install():
    if platform.system() != 'Linux':
        raise ValueError('The browser image installer supports Linux only')
    target_platform = {'x86_64': 'linux64', 'aarch64': 'linux-arm64'}.get(platform.machine())
    if not target_platform:
        raise ValueError('Unsupported browser image architecture')
    base = Path(__file__).resolve().parent
    lock_path = base / 'fixed-browser-lock.json'
    lock = json.loads(lock_path.read_text())
    if lock['schema'] != 1 or lock['version'] != '156.0.8078.80':
        raise ValueError('Review the installer before changing the fixed browser pin')
    packages = browser_packages(base / 'node_modules')
    cache = Path(os.environ['PLAYWRIGHT_BROWSERS_PATH'])
    if not cache.is_absolute():
        raise ValueError('The image browser cache must be absolute')
    provenance = {'schema': 1, 'replacement_version': lock['version'], 'platform': target_platform,
                  'source_lock_sha256': digest_file(lock_path), 'browsers': []}
    expected = {package['path']: [] for package in packages}
    for name in ('chromium', 'chromium-headless-shell'):
        replacement = lock['platforms'][target_platform][name]
        expected_url = (f"https://storage.googleapis.com/chrome-for-testing-public/{lock['version']}/"
                        f"{target_platform}/{replacement['root']}.zip")
        if replacement['url'] != expected_url or not re.fullmatch('[a-f0-9]{64}', replacement['sha256']):
            raise ValueError('Unexpected pinned browser source')
        with tempfile.TemporaryDirectory(prefix='fixed-browser-') as directory:
            archive = Path(directory) / 'browser.zip'
            with urllib.request.urlopen(replacement['url'], timeout=120) as incoming, archive.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            extracted = Path(directory) / 'extracted'
            extract_verified_archive(archive, replacement['sha256'], extracted, replacement['root'])
            executable = extracted / replacement['root'] / replacement['executable']
            version = subprocess.check_output([str(executable), '--version'], text=True).strip()
            if not re.search(r'(?<![0-9.])' + re.escape(lock['version']) + r'(?![0-9.])', version):
                raise ValueError('Replacement executable version differs from the pin')
            installed = set()
            for package in packages:
                declared = package['declared'][name]
                revision = declared['revision']
                if not re.fullmatch('[0-9]+', revision):
                    raise ValueError('Unexpected Playwright cache revision')
                destination = cache / (name.replace('-', '_') + '-' + revision)
                if destination not in installed:
                    # Only explicit cache entries inside the new image are replaced.
                    if destination.exists():
                        shutil.rmtree(destination)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(extracted, destination)
                    (destination / 'INSTALLATION_COMPLETE').touch()
                    installed.add(destination)
                installed_executable = str(destination / replacement['root'] / replacement['executable'])
                expected[package['path']].append(installed_executable)
                provenance['browsers'].append({
                    'playwright_core_path': package['path'], 'playwright_core': package['version'],
                    'name': name, 'cache_revision': revision,
                    'package_declared_version': declared['browserVersion'],
                    **replacement, 'reported_version': version,
                    'installed_executable': installed_executable,
                })
    # Full Chrome initializes Crashpad under XDG_CONFIG_HOME even with an
    # isolated Playwright profile. The worker's HOME is intentionally read-only.
    # Scope this override to Chrome; agent CLI/auth configuration stays intact.
    launchers = {}
    for browser in provenance['browsers']:
        if browser['name'] == 'chromium':
            executable = browser['installed_executable']
            if executable not in launchers:
                launchers[executable] = wrap_full_browser(executable)
            browser['launcher'] = launchers[executable]
    # The CLI defaults to Playwright's supported Chrome channel on Linux.
    # Provide that path within this image, pointing at the same pinned full browser.
    full = subprocess.check_output(['node', '-e', "console.log(require('playwright').chromium.executablePath())"],
                                   cwd=base, text=True).strip()
    if full not in {paths[0] for paths in expected.values()}:
        raise ValueError('The stable Playwright library resolves an unpatched browser')
    chrome = Path('/opt/google/chrome/chrome')
    chrome.parent.mkdir(parents=True, exist_ok=True)
    chrome.symlink_to(full)
    provenance['chrome_channel_executable'] = str(chrome)
    # Capture actual spawn paths for every installed library, including the CLI
    # alpha. This stops at spawn; finished-image sandbox/worker/CLI qualification
    # remains required before campaign admission.
    program = r'''
const childProcess = require('node:child_process');
const groups = {};
let paths;
childProcess.spawn = file => { paths.push(file); throw Error('browser-path-control'); };
(async () => {
  for (const core of JSON.parse(process.argv[1])) {
    paths = groups[core] = [];
    const {chromium} = require(core);
    for (const options of [{headless:false}, {headless:true}, {channel:'chrome'}]) {
      try { await chromium.launch(options); throw Error('unexpected launch'); }
      catch (error) { if (!String(error).includes('browser-path-control')) throw error; }
    }
  }
  console.log(JSON.stringify(groups));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    for paths in expected.values():
        paths.append(str(chrome))
    observed = json.loads(subprocess.check_output(['node', '-e', program, json.dumps(list(expected))], cwd=base, text=True))
    if observed != expected:
        raise ValueError(f'Playwright default launch paths differ: {observed!r} != {expected!r}')
    provenance['launch_path_control'] = 'captured every core full/headless/chrome spawn target; browsers not started'
    (base / 'fixed-browser-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    print(json.dumps(provenance))


if __name__ == '__main__':
    install()
