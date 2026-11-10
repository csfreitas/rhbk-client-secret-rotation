#!/usr/bin/env python3
"""Prepare a fresh ARM64 CRC using explicit credentials and pinned official inputs.

configure only writes a dedicated local kubeconfig. install prepares the official
bundle, owned namespace and registry route. OLM manages CRDs during installation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import lab

CRDS = {'keycloaks.k8s.keycloak.org': 'Keycloak',
        'keycloakrealmimports.k8s.keycloak.org': 'KeycloakRealmImport'}
BUNDLE_FILES = {'keycloak-operator.clusterserviceversion.yaml',
                'keycloaks.k8s.keycloak.org-v1.crd.yml',
                'keycloakrealmimports.k8s.keycloak.org-v1.crd.yml'}
REGISTRY_NAMESPACE = 'openshift-image-registry'
REGISTRY_HOST = 'default-route-openshift-image-registry.apps-crc.testing'


def run(argv, timeout=300):
    """Never include raw command output in exceptions: auth may be present."""
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError('BLOCKED: '+Path(argv[0]).name+' command failed; credential-bearing output withheld')
    return result.stdout


def private_directory():
    if lab.PRIVATE.is_symlink():
        raise RuntimeError('BLOCKED: .private must not be a symlink')
    lab.PRIVATE.mkdir(mode=0o700, exist_ok=True)
    lab.PRIVATE.chmod(0o700)


def private_write(path, data):
    if path.is_symlink():
        raise RuntimeError('BLOCKED: private output must not be a symlink')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    path.chmod(0o600)


def configure(source, context):
    source = Path(source).expanduser().resolve(strict=True)
    prefix = ['oc', '--kubeconfig='+str(source), '--context='+context]
    server = run(prefix+['config', 'view', '--minify', '-o', 'jsonpath={.clusters[0].cluster.server}'], 30).decode().strip()
    if server != 'https://api.crc.testing:6443':
        raise RuntimeError('BLOCKED: the selected source context is not the local CRC API')
    # Export just the explicitly selected context, embedding certificate paths.
    data = run(prefix+['config', 'view', '--raw', '--flatten', '--minify'], 30)
    private_directory()
    destination = lab.PRIVATE/'crc.kubeconfig'
    if destination.exists() and source != destination.resolve() and destination.read_bytes() != data:
        raise RuntimeError('BLOCKED: dedicated kubeconfig already exists; inspect it before replacing credentials')
    private_write(destination, data)
    print('Configured dedicated CRC kubeconfig; no cluster resources changed')


def registry_auth_file():
    value = os.environ.get('REGISTRY_AUTH_FILE')
    if not value:
        raise RuntimeError('BLOCKED: set REGISTRY_AUTH_FILE to an authorized Red Hat registry auth file')
    path = Path(value).expanduser().resolve(strict=True)
    auth = json.loads(path.read_text())
    if not isinstance(auth.get('auths'), dict) or 'registry.redhat.io' not in auth['auths']:
        raise RuntimeError('BLOCKED: registry.redhat.io credentials are absent from REGISTRY_AUTH_FILE')
    return path


def extract_members(archive, destination):
    """Extract only regular official manifest files, never arbitrary tar paths."""
    with tarfile.open(archive, 'r:*') as bundle:
        for member in bundle:
            normalized = member.name.removeprefix('./')
            parts = normalized.split('/')
            if len(parts) != 2 or parts[0] != 'manifests' or parts[1] not in BUNDLE_FILES:
                continue
            if not member.isfile() or member.size > 16*1024*1024:
                raise RuntimeError('BLOCKED: invalid bundle manifest entry')
            stream = bundle.extractfile(member)
            if stream is None:
                raise RuntimeError('BLOCKED: unreadable bundle manifest')
            private_write(destination/parts[1], stream.read())


def extract_bundle(image, auth, destination):
    destination.mkdir(mode=0o700, exist_ok=True)
    # A failed/new extraction must never fall back to manifests from a prior bundle.
    for name in BUNDLE_FILES:
        (destination/name).unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='bundle-', dir=lab.PRIVATE) as directory:
        temp = Path(directory)
        if shutil.which('skopeo'):
            copied = temp/'image'
            run(['skopeo', 'copy', '--authfile', str(auth), '--override-os', 'linux',
                 '--override-arch', 'amd64', '--preserve-digests', 'docker://'+image, 'dir:'+str(copied)])
            manifest_bytes = (copied/'manifest.json').read_bytes()
            if hashlib.sha256(manifest_bytes).hexdigest() != image.split('@sha256:')[1]:
                raise RuntimeError('BLOCKED: bundle manifest digest mismatch')
            manifest = json.loads(manifest_bytes)
            for layer in manifest.get('layers', []):
                algorithm, digest = layer['digest'].split(':', 1)
                if algorithm != 'sha256' or len(digest) != 64 or any(x not in '0123456789abcdef' for x in digest):
                    raise RuntimeError('BLOCKED: unsupported bundle layer digest')
                archive = copied/digest
                if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
                    raise RuntimeError('BLOCKED: bundle layer checksum mismatch')
                extract_members(archive, destination)
        elif shutil.which('podman'):
            run(['podman', 'pull', '--authfile', str(auth), '--platform', 'linux/amd64', image])
            container = run(['podman', 'create', '--platform', 'linux/amd64', '--entrypoint', '/bin/true', image]).decode().strip()
            try:
                archive = temp/'bundle.tar'
                run(['podman', 'export', '--output', str(archive), container])
                extract_members(archive, destination)
            finally:
                run(['podman', 'rm', container], 30)
        else:
            raise RuntimeError('BLOCKED: install skopeo or a working podman before setup')
    if any(not (destination/name).is_file() for name in BUNDLE_FILES):
        raise RuntimeError('BLOCKED: required files missing from the pinned official bundle')


def yaml_to_json(path):
    # Use the installed OpenShift CLI parser; no third-party Python YAML dependency.
    return json.loads(lab.oc('create', '--dry-run=client', '--validate=false', '-f', str(path), '-o', 'json'))


def validate_crd(obj, name):
    spec = obj.get('spec', {})
    if obj.get('kind') != 'CustomResourceDefinition' or obj['metadata']['name'] != name:
        raise RuntimeError('BLOCKED: unexpected CRD identity')
    if spec.get('group') != 'k8s.keycloak.org' or spec.get('scope') != 'Namespaced' or spec.get('names', {}).get('kind') != CRDS[name]:
        raise RuntimeError('BLOCKED: incompatible existing CRD identity')
    versions = [v for v in spec.get('versions', []) if v['name'] == 'v2beta1' and v.get('served')]
    if len(versions) != 1:
        raise RuntimeError('BLOCKED: existing CRD does not serve v2beta1; no replacement attempted')
    properties = versions[0].get('schema', {}).get('openAPIV3Schema', {}).get('properties', {}).get('spec', {}).get('properties', {})
    required = {'startOptimized', 'features', 'bootstrapAdmin', 'image', 'instances', 'db'} if name == 'keycloaks.k8s.keycloak.org' else {'keycloakCRName', 'realm'}
    if not required.issubset(properties):
        raise RuntimeError('BLOCKED: existing CRD schema lacks fields needed by this lab')


def prepare_bundle(auth, lock):
    directory = lab.PRIVATE/'bundle'
    extract_bundle(lock['images']['bundle']['original'], auth, directory)
    csv = yaml_to_json(directory/'keycloak-operator.clusterserviceversion.yaml')
    if csv.get('kind') != 'ClusterServiceVersion' or csv.get('spec', {}).get('version') != lock['operator']['version']:
        raise RuntimeError('BLOCKED: official bundle version does not match versions.lock.yaml')
    private_write(lab.PRIVATE/'operator-csv.json', json.dumps(csv).encode())
    definitions = {}
    for name in CRDS:
        desired = yaml_to_json(directory/(name+'-v1.crd.yml'))
        validate_crd(desired, name)
        existing = lab.get('customresourcedefinition', name, None)
        if existing:
            validate_crd(existing, name)
        definitions[name] = (desired, existing)
    return definitions


def ensure_namespace():
    # A preexisting namespace must already match this checkout's recorded UID.
    lab.guard(False)
    namespace = lab.get('namespace', lab.NS, None)
    if not namespace:
        namespace = json.loads(lab.oc('create', '-f', '-', '-o', 'json', data={
            'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': lab.meta(lab.NS, False)}))
        lab.save('evidence/namespace-identity.json', {'name': lab.NS, 'uid': namespace['metadata']['uid'],
                 'cluster_uid': lab.get('namespace', 'kube-system', None)['metadata']['uid']})
    lab.guard()
    if not lab.get('serviceaccount', 'builder'):
        lab.apply({'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': lab.meta('builder')})
    for name, role, subjects in [
        ('lab-image-builder', 'system:image-builder', [{'kind': 'ServiceAccount', 'name': 'builder', 'namespace': lab.NS}]),
        ('lab-image-pullers', 'system:image-puller', [{'kind': 'Group', 'name': 'system:serviceaccounts:'+lab.NS, 'apiGroup': 'rbac.authorization.k8s.io'}])]:
        lab.apply({'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': lab.meta(name),
                   'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': role}, 'subjects': subjects})


def prepare_registry():
    config = lab.get('configs.imageregistry.operator.openshift.io', 'cluster', None)
    if not config or config.get('spec', {}).get('managementState') != 'Managed':
        raise RuntimeError('BLOCKED: the CRC internal registry must already be Managed')
    changed = not config['spec'].get('defaultRoute', False)
    if changed:
        patch = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': config['metadata']['resourceVersion']},
                 {'op': 'add', 'path': '/spec/defaultRoute', 'value': True}]
        lab.oc('patch', 'configs.imageregistry.operator.openshift.io', 'cluster', '--type=json', '-p', json.dumps(patch))
    deadline = time.monotonic()+120
    while time.monotonic() < deadline:
        route = lab.get('route', 'default-route', REGISTRY_NAMESPACE)
        if route and any(c.get('type') == 'Admitted' and c.get('status') == 'True' for ingress in route.get('status', {}).get('ingress', []) for c in ingress.get('conditions', [])):
            if route['spec'].get('host') != REGISTRY_HOST or route['spec'].get('to', {}).get('name') != 'image-registry' or route['spec'].get('tls', {}).get('termination') != 'reencrypt':
                raise RuntimeError('BLOCKED: unexpected existing registry Route')
            break
        time.sleep(2)
    else:
        raise RuntimeError('BLOCKED: registry Route was not admitted')
    ca = lab.get('configmap', 'default-ingress-cert', 'openshift-config-managed')
    pem = (ca or {}).get('data', {}).get('ca-bundle.crt', '')
    if '-----BEGIN CERTIFICATE-----' not in pem:
        raise RuntimeError('BLOCKED: public ingress CA bundle unavailable')
    private_write(lab.PRIVATE/'route-ca.crt', pem.encode())
    context = ssl.create_default_context(cafile=str(lab.PRIVATE/'route-ca.crt'))
    try:
        with urllib.request.urlopen('https://'+REGISTRY_HOST+'/v2/', context=context, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError('BLOCKED: unexpected registry response')
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise RuntimeError('BLOCKED: registry TLS endpoint unavailable') from None
    return changed


def install():
    lab.guard(False)
    auth = registry_auth_file()  # Check explicit authorization before any cluster mutation.
    private_directory()
    lock = json.loads((lab.ROOT/'versions.lock.yaml').read_text())
    prepare_bundle(auth, lock)
    ensure_namespace()
    changed = prepare_registry()
    lab.manifests()
    record = {'status': 'PASS', 'crds_managed_by': 'OLM', 'enabled_registry_default_route': changed,
              'registry_host': REGISTRY_HOST, 'bundle_image': lock['images']['bundle']['original'],
              'shared_resources_retained_on_cleanup': ['official CRDs', 'registry defaultRoute']}
    private_write(lab.PRIVATE/'setup.json', json.dumps(record, indent=2).encode())
    lab.save('evidence/setup.json', record)
    print('PASS: official bundle inspected, namespace and registry prepared; OLM installation is a separate step')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    configuration = sub.add_parser('configure')
    configuration.add_argument('--kubeconfig', required=True)
    configuration.add_argument('--context', required=True)
    sub.add_parser('install')
    args = parser.parse_args()
    if args.action == 'configure':
        configure(args.kubeconfig, args.context)
    else:
        install()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'BLOCKED: setup input, local tool or readiness failed; no credential output', file=__import__('sys').stderr)
        raise SystemExit(2)
