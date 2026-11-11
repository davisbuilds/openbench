"""Install each locked Playwright core's upstream Chromium and record its identity.

The full-browser launcher supplies only Chrome's private configuration directory;
Playwright's downloaded browser bytes and manifests remain unchanged.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess

def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


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
    base = Path(__file__).resolve().parent
    packages = browser_packages(base / 'node_modules')
    provenance = {'schema': 2, 'source': 'locked Playwright upstream browser manifests',
                  'browsers': []}
    for package in packages:
        core = Path(package['path'])
        subprocess.run(['node', str(core / 'cli.js'), 'install', 'chromium'],
                       env=dict(os.environ, PLAYWRIGHT_SKIP_BROWSER_GC='1'), check=True)
        program = "console.log(require(process.argv[1]).chromium.executablePath())"
        executable = subprocess.check_output(['node', '-e', program, str(core)], text=True).strip()
        reported = subprocess.check_output([executable, '--version'], text=True).strip()
        declared = package['declared']['chromium']['browserVersion']
        if declared not in reported.split():
            raise ValueError('Installed full browser differs from its upstream manifest')
        provenance['browsers'].append({**package, 'reported_version': reported,
                                      'installed_executable': executable,
                                      'launcher': wrap_full_browser(executable)})
    # The CLI defaults to the supported Chrome channel. Reuse the stable
    # library's upstream full browser without installing another distribution.
    full = subprocess.check_output(['node', '-e',
        "console.log(require('playwright').chromium.executablePath())"], cwd=base, text=True).strip()
    chrome = Path('/opt/google/chrome/chrome')
    chrome.parent.mkdir(parents=True, exist_ok=True)
    chrome.symlink_to(full)
    provenance['chrome_channel_executable'] = str(chrome)
    (base / 'browser-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')


if __name__ == '__main__':
    install()
