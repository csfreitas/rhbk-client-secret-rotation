#!/usr/bin/env python3
"""Build an optimized RHBK image inside the owned CRC namespace; never deploy it.

render is local-only. build uploads only the snapshotted Dockerfile, pushes to the
CRC internal registry and records the completed Build's immutable reference.
wait resumes observation of a previously requested build, without starting one.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import lab

NAME = 'rhbk-optimized'
RECORD = 'rhbk-optimized-image.json'
PENDING = 'rhbk-build-pending.json'
REGISTRY = 'image-registry.openshift-image-registry.svc:5000'
SOURCE_LABEL = 'lab.rhbk.dockerfile-sha256'
BASE_LABEL = 'lab.rhbk.base-digest'
TERMINAL = {'Complete', 'Failed', 'Error', 'Cancelled'}


def require(condition, reason):
    if not condition:
        raise RuntimeError('BLOCKED: ' + reason)


def digest(value):
    return isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value) is not None


def private_record(name, value):
    require(not lab.PRIVATE.is_symlink(), 'private directory must not be a symlink')
    lab.PRIVATE.mkdir(mode=0o700, exist_ok=True)
    path = lab.PRIVATE / name
    require(not path.is_symlink(), 'private record must not be a symlink')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + name, dir=lab.PRIVATE)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(value, output, indent=2); output.write('\n')
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def inputs():
    lock = json.loads((lab.ROOT / 'versions.lock.yaml').read_text())
    record = lock['images']['rhbk']
    require(record['original'].startswith('registry.redhat.io/rhbk/keycloak-rhel9@')
            and digest(record['original'].split('@')[-1]) and digest(record['digest_arm64']),
            'RHBK base must be pinned to the official repository and platform digest')
    require(lock['platform']['architecture'] == 'arm64', 'CRC build helper requires the pinned ARM64 platform')
    source_path = lab.ROOT / 'apps/rhbk/Dockerfile'
    require(source_path.is_file() and not source_path.is_symlink(), 'regular RHBK Dockerfile is required')
    source = source_path.read_bytes()
    return {'base_digest': record['digest_arm64'], 'official_source': record['original'],
            'base_image': REGISTRY + '/' + lab.NS + '/rhbk@' + record['digest_arm64'],
            'dockerfile_sha256': hashlib.sha256(source).hexdigest(), 'source': source,
            'expected_server_version': lock['expected_server_version']}


def render(pin=None):
    pin = pin or inputs()
    config = json.loads((lab.ROOT / 'deploy/build/rhbk-buildconfig.json').read_text())
    require(config.get('kind') == 'BuildConfig' and config['metadata']['name'] == NAME,
            'unexpected build template')
    config['metadata'] = lab.meta(NAME)
    config['spec']['nodeSelector'] = {'kubernetes.io/arch': 'arm64'}
    config['spec']['strategy']['dockerStrategy']['buildArgs'] = [{'name': 'RHBK_IMAGE', 'value': pin['base_image']}]
    config['spec']['output']['imageLabels'] = [
        {'name': SOURCE_LABEL, 'value': pin['dockerfile_sha256']},
        {'name': BASE_LABEL, 'value': pin['base_digest']}]
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': 'image.openshift.io/v1', 'kind': 'ImageStream', 'metadata': lab.meta(NAME)}, config]}


def owned(obj):
    return bool(obj) and all(obj.get('metadata', {}).get('labels', {}).get(k) == v for k, v in lab.LABELS.items())


def verify_mirror(pin):
    stream = lab.get('imagestream', 'rhbk')
    image_name = 'rhbk@' + pin['base_digest']
    mirrored = lab.get('imagestreamimage', image_name)
    metadata = (stream or {}).get('metadata', {})
    # This existing input is only read. Its verified digest and guarded namespace
    # establish provenance without adopting or relabeling an older mirror.
    require(metadata.get('name') == 'rhbk' and metadata.get('namespace') == lab.NS,
            'base ImageStream identity differs from the guarded namespace')
    image_metadata = (mirrored or {}).get('metadata', {})
    require(image_metadata.get('name') == image_name and image_metadata.get('namespace') == lab.NS,
            'base ImageStreamImage identity differs from the guarded namespace or digest')
    require((mirrored or {}).get('image', {}).get('metadata', {}).get('name') == pin['base_digest'],
            'mirror the pinned official RHBK image before building')


def managed_match(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and managed_match(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(managed_match(a, b) for a, b in zip(expected, actual))
    return expected == actual


def config_matches(expected, actual):
    if not isinstance(actual, dict):
        return False
    actual = dict(actual)
    # OpenShift omits this empty list in API responses (omitempty).
    # A nonempty trigger list must still fail the comparison.
    actual.setdefault('triggers', [])
    return managed_match(expected, actual)


def validate_source(spec, pin):
    source = spec.get('source', {})
    require(source.get('type') == 'Binary' and not any(source.get(key) for key in
            ('git', 'dockerfile', 'contextDir', 'images', 'secrets', 'configMaps', 'sourceSecret'))
            and not source.get('binary', {}).get('asFile'), 'unexpected Build source')
    strategy = spec.get('strategy', {}).get('dockerStrategy', {})
    override = strategy.get('from')
    require(not override or (override.get('kind') == 'DockerImage' and override.get('name') == pin['base_image']),
            'Docker strategy overrides the pinned final base image')
    require(not strategy.get('volumes') and not spec.get('postCommit'), 'unexpected Build volume or post-commit hook')


def assert_build(build, config, pin):
    require(owned(build), 'Build does not belong to this laboratory')
    meta = build['metadata']
    require(meta.get('namespace') == lab.NS and re.fullmatch(NAME + r'-[0-9]+', meta.get('name', '')),
            'unexpected Build identity')
    owners = meta.get('ownerReferences', [])
    relation = build.get('status', {}).get('config', {})
    require(any(x.get('kind') == 'BuildConfig' and x.get('name') == NAME and x.get('uid') == config['metadata']['uid'] for x in owners)
            or (relation.get('name') == NAME and relation.get('uid') == config['metadata']['uid']),
            'Build is not linked to the current owned BuildConfig')
    spec = build.get('spec', {})
    validate_source(spec, pin)
    desired = config['spec']
    for field in ('strategy', 'output', 'nodeSelector'):
        require(managed_match(desired[field], spec.get(field)), 'Build input or output differs from the reviewed configuration')
    labels = {item['name']: item['value'] for item in spec['output'].get('imageLabels', [])}
    require(labels.get(SOURCE_LABEL) == pin['dockerfile_sha256'] and labels.get(BASE_LABEL) == pin['base_digest'],
            'Build source labels differ from the current source')


def observe_build(name, timeout=900):
    require(re.fullmatch(NAME + r'-[0-9]+', name or ''), 'invalid RHBK Build name')
    require(isinstance(timeout, int) and 1 <= timeout <= 1800, 'build wait must be between 1 and 1800 seconds')
    lab.guard()
    pin = inputs(); verify_mirror(pin)
    config = lab.get('buildconfig', NAME)
    require(owned(config), 'BuildConfig is not owned by this lab')
    require(config_matches(render()['items'][1]['spec'], config.get('spec')), 'BuildConfig no longer matches the source')
    validate_source(config['spec'], pin)
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        build = lab.get('build', name)
        assert_build(build, config, pin)
        phase = build.get('status', {}).get('phase')
        if phase != previous:
            print(name + ': ' + str(phase), flush=True); previous = phase
        if phase in TERMINAL:
            if phase != 'Complete':
                (lab.PRIVATE / PENDING).unlink(missing_ok=True)
            require(phase == 'Complete', 'RHBK Build did not complete; inspect its events, no deployment attempted')
            break
        time.sleep(3)
    else:
        raise RuntimeError('BLOCKED: RHBK Build observation timed out; use wait for the same Build, no deployment attempted')
    actual = build['status'].get('output', {}).get('to', {}).get('imageDigest')
    require(digest(actual), 'completed Build did not report an immutable output digest')
    tag = lab.get('imagestreamtag', NAME + ':lab')
    require((tag or {}).get('image', {}).get('metadata', {}).get('name') == actual,
            'output tag differs from the completed Build digest; no reference recorded')
    namespace = json.loads((lab.ROOT / 'evidence/namespace-identity.json').read_text())
    record = {k: v for k, v in pin.items() if k != 'source'}
    record.update({'status': 'BUILD_COMPLETE', 'image': REGISTRY + '/' + lab.NS + '/' + NAME + '@' + actual,
                   'digest': actual, 'build': name, 'build_uid': build['metadata']['uid'],
                   'namespace_uid': namespace['uid'], 'cluster_uid': namespace['cluster_uid'],
                   'namespace': lab.NS, 'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   'runtime_validation': 'NOT_RUN'})
    private_record(RECORD, record)
    lab.save('evidence/rhbk-image.json', record)
    (lab.PRIVATE / PENDING).unlink(missing_ok=True)
    print('BUILD_COMPLETE: immutable image recorded locally; Keycloak was not changed')
    return record


def build(timeout=900):
    require(isinstance(timeout, int) and 1 <= timeout <= 1800, 'build wait must be between 1 and 1800 seconds')
    lab.guard()
    pin = inputs(); verify_mirror(pin)
    require(not (lab.PRIVATE / PENDING).exists(), 'a build request is pending; inspect it and use wait for the same Build')
    active = lab.get('builds') or {}
    require(not any(x.get('metadata', {}).get('labels', {}).get('openshift.io/build-config.name') == NAME
                    and x.get('status', {}).get('phase') not in TERMINAL for x in active.get('items', [])),
            'an RHBK Build is already active; resume its observation')
    resources = render(pin)
    lab.apply(resources)
    config = lab.get('buildconfig', NAME)
    require(owned(config) and config_matches(resources['items'][1]['spec'], config.get('spec')), 'BuildConfig readback differs')
    validate_source(config['spec'], pin)
    private_record(PENDING, {'phase': 'REQUESTED', 'dockerfile_sha256': pin['dockerfile_sha256'],
                            'base_digest': pin['base_digest']})
    # No repository context, kubeconfig, secret or incidental file is uploaded.
    with tempfile.TemporaryDirectory(prefix='rhbk-context-') as directory:
        (Path(directory) / 'Dockerfile').write_bytes(pin['source'])
        name = lab.oc('start-build', NAME, '-n', lab.NS, '--from-dir=' + directory, '-o', 'name').strip().split('/')[-1]
    require(re.fullmatch(NAME + r'-[0-9]+', name or ''), 'Build response unavailable; inspect existing Builds before retrying')
    private_record(PENDING, {'phase': 'STARTED', 'build': name, 'dockerfile_sha256': pin['dockerfile_sha256'],
                            'base_digest': pin['base_digest']})
    return observe_build(name, timeout)


def load_reference():
    """Local validation for CR rendering; deployment must call lab.guard separately."""
    path = lab.PRIVATE / RECORD
    require(path.is_file() and not path.is_symlink(), 'build the optimized RHBK image before rendering its CR')
    record = json.loads(path.read_text()); pin = inputs()
    require(record.get('status') == 'BUILD_COMPLETE' and digest(record.get('digest')), 'optimized image build record is incomplete')
    for key in ('base_digest', 'official_source', 'dockerfile_sha256', 'expected_server_version'):
        require(record.get(key) == pin[key], 'optimized image record is stale; rebuild after changing its inputs')
    require(record.get('image') == REGISTRY + '/' + lab.NS + '/' + NAME + '@' + record['digest'], 'recorded image is outside the CRC lab')
    namespace = json.loads((lab.ROOT / 'evidence/namespace-identity.json').read_text())
    require(record.get('namespace') == lab.NS and record.get('namespace_uid') == namespace.get('uid')
            and record.get('cluster_uid') == namespace.get('cluster_uid'), 'optimized image belongs to another cluster or namespace')
    return record['image']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('render')
    start = sub.add_parser('build'); start.add_argument('--timeout', type=int, default=900)
    resume = sub.add_parser('wait'); resume.add_argument('--build', required=True); resume.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    if args.action == 'render':
        lab.save('evidence/rhbk-buildconfig.json', render())
        print('RENDERED: evidence/rhbk-buildconfig.json; no cluster request was made')
    elif args.action == 'build': build(args.timeout)
    else: observe_build(args.build, args.timeout)


if __name__ == '__main__':
    try: main()
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'BLOCKED: image build validation failed; raw output withheld', file=sys.stderr)
        raise SystemExit(2)
