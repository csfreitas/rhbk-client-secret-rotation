#!/usr/bin/env python3
"""Mirror three pinned ARM64 inputs into CRC with authenticated, validated TLS."""
import base64
import json
import ssl
import subprocess
import sys
import lab
import setup

NAMES = ('rhbk', 'python', 'postgres')


def source_image(record):
    repository = record['original'].split('@', 1)[0]
    if ':' in repository.rsplit('/', 1)[-1]:
        repository = repository.rsplit(':', 1)[0]
    digest = record['digest_arm64']
    if not digest.startswith('sha256:') or len(digest) != 71:
        raise RuntimeError('BLOCKED: invalid pinned ARM64 image digest')
    return repository+'@'+digest


def main():
    lab.guard()
    names = sys.argv[1:] or list(NAMES)
    if not names or any(name not in NAMES for name in names):
        raise RuntimeError('BLOCKED: mirror accepts only rhbk, python and postgres')
    if not (lab.PRIVATE/'setup.json').is_file():
        raise RuntimeError('BLOCKED: make setup must complete before mirroring')
    auth = json.loads(setup.registry_auth_file().read_text())
    token = lab.oc('create', 'token', 'builder', '-n', lab.NS, '--duration=1h').strip()
    mirror_auth = {'auths': {'registry.redhat.io': auth['auths']['registry.redhat.io'],
                           setup.REGISTRY_HOST: {'auth': base64.b64encode(('builder:'+token).encode()).decode()}}}
    auth_path = lab.PRIVATE/'mirror-auth.json'
    certs = ssl.create_default_context().get_ca_certs(binary_form=True)
    pem = ''.join(ssl.DER_cert_to_PEM_cert(cert) for cert in certs)
    pem += (lab.PRIVATE/'route-ca.crt').read_text()
    ca_path = lab.PRIVATE/'registry-ca.pem'
    setup.private_write(ca_path, pem.encode())
    lock = json.loads((lab.ROOT/'versions.lock.yaml').read_text())
    mirrored = {}
    try:
        setup.private_write(auth_path, json.dumps(mirror_auth).encode())
        for name in names:
            lab.apply({'apiVersion': 'image.openshift.io/v1', 'kind': 'ImageStream', 'metadata': lab.meta(name)})
            source = source_image(lock['images'][name])
            destination = setup.REGISTRY_HOST+'/'+lab.NS+'/'+name+':pinned'
            setup.run(['oc', *lab.kubeconfig_args(), 'image', 'mirror', '--registry-config='+str(auth_path),
                       '--certificate-authority='+str(ca_path), '--filter-by-os=linux/arm64', source, destination], 600)
            image = lab.get('imagestreamtag', name+':pinned')
            actual = (image or {}).get('image', {}).get('metadata', {}).get('name')
            if actual != lock['images'][name]['digest_arm64']:
                raise RuntimeError('BLOCKED: local mirrored image digest does not match the pin')
            mirrored[name] = {'source': source, 'digest': actual}
            print('Verified local mirror: '+name)
    finally:
        auth_path.unlink(missing_ok=True)
    lab.save('evidence/mirror.json', {'status': 'PASS', 'images': mirrored})


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'BLOCKED: registry mirror failed; no credential output', file=sys.stderr)
        raise SystemExit(2)
