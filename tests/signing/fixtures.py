"""公开安全回归只使用临时自签证书，不依赖或重新分发 Apple CA 材料。"""
import functools
import hashlib
from pathlib import Path
import subprocess
import tempfile


@functools.lru_cache(maxsize=1)
def synthetic_certificates():
    certificates = []
    with tempfile.TemporaryDirectory(prefix='synthetic-certificate-') as directory:
        root = Path(directory)
        for alias in ('G2', 'G3', 'G4', 'G5', 'G6', 'DEV-ID-G1', 'DEV-ID-G2'):
            certificate = root / 'certificate.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                            'ec_paramgen_curve:prime256v1', '-nodes', '-days', '1',
                            '-subj', '/CN=Public CI fixture ' + alias, '-keyout', str(root / 'key.pem'),
                            '-out', str(certificate)], capture_output=True, check=True, timeout=10)
            der = subprocess.check_output(['openssl', 'x509', '-in', str(certificate), '-outform', 'DER'])
            filename = ('AppleWWDRCA' + alias if alias.startswith('G') else
                        'DeveloperIDCA' if alias == 'DEV-ID-G1' else 'DeveloperIDG2CA') + '.pem'
            certificates.append((alias, filename, hashlib.sha256(der).hexdigest(), certificate.read_bytes()))
    return certificates


def write_synthetic_apple_certificates(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    lines = []
    for alias, filename, digest, data in synthetic_certificates():
        (destination / filename).write_bytes(data)
        lines.append(f'{alias} {digest} {filename} https://www.apple.com/certificateauthority/{alias}.cer')
    manifest = destination / 'manifest.txt'
    manifest.write_text('\n'.join(lines) + '\n')
    return manifest
