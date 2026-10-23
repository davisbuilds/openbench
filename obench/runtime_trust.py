"""Reject the incident CA in effective container trust stores, with a positive control.

Scope is the exact emSign ECC Root CA G3 certificate removed by
GHSA-6q48-6c7w-9gw2, not all vendor roots or cross-signed chains.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import sysconfig

BLOCKED = '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b'
PEM = re.compile(rb'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', re.S)


def inspect_bundle(data):
    certificates = PEM.findall(data)
    if not certificates:
        raise ValueError('no PEM certificates; refusing an empty trust-store check')
    fingerprints = {hashlib.sha256(ssl.PEM_cert_to_DER_cert(p.decode())).hexdigest()
                    for p in certificates}
    return {'sha256': hashlib.sha256(data).hexdigest(), 'certificates': len(certificates),
            'blocked': sorted(fingerprints & {BLOCKED})}


def prepare_system(positive, output):
    if inspect_bundle(Path(positive).read_bytes())['blocked'] != [BLOCKED]:
        raise ValueError('known-positive control did not detect the incident certificate')
    source = Path('/usr/share/ca-certificates')
    excluded = []
    for path in sorted(source.rglob('*.crt')):
        if inspect_bundle(path.read_bytes())['blocked']:
            excluded.append(path.relative_to(source).as_posix())
    config = Path('/etc/ca-certificates.conf')
    lines = config.read_text().splitlines()
    for name in excluded:
        lines = [line for line in lines if line not in (name, '!'+name)] + ['!'+name]
    config.write_text('\n'.join(lines)+'\n')
    subprocess.run(['update-ca-certificates', '--fresh'], check=True)
    stores = {}
    for path in sorted(Path('/etc/ssl/certs').iterdir()):
        if path.is_file() and PEM.search(path.read_bytes()):
            stores[str(path)] = inspect_bundle(path.read_bytes())
    pip = Path(sysconfig.get_path('purelib'))/'pip/_vendor/certifi/cacert.pem'
    stores['pip-vendored-certifi'] = inspect_bundle(pip.read_bytes())
    roots = subprocess.check_output(['node', '-e',
        "process.stdout.write(require('tls').rootCertificates.join('\\n'))"])
    stores['node-default-roots'] = inspect_bundle(roots)
    if '/etc/ssl/certs/ca-certificates.crt' not in stores or any(s['blocked'] for s in stores.values()):
        raise ValueError('effective trust store still contains the incident certificate')
    result = {'schema': 1, 'scope': 'exact incident certificate',
              'known_positive_detected': True, 'excluded_system_sources': excluded,
              'stores': stores}
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(result, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--known-positive', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    prepare_system(args.known_positive, args.output)


if __name__ == '__main__':
    main()
