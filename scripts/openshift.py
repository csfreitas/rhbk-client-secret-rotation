#!/usr/bin/env python3
"""Deploy the demo beside an existing RHBK. No CRC, Operator or RHBK mutations.

configure is read-only remotely; render writes reviewable manifests; apply is the
explicit deployment boundary. Existing credentials are imported, never rotated
by this installer. Credentials and API error bodies are never printed.
"""
from __future__ import annotations

import argparse
import base64
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / '.private'
LABELS = {'app.kubernetes.io/part-of': 'rhbk-secret-rotation-lab',
          'lab.rhbk/owner': 'client-secret-rotation'}
IDENTITY = 'openshift-identity.json'
CONFIG = 'openshift.json'
META = ('client.secret.creation.time', 'client.secret.expiration.time',
        'client.secret.rotated.creation.time', 'client.secret.rotated.expiration.time')
UNSET = object()


class Blocked(RuntimeError):
    """Only fixed, nonsensitive messages belong here."""


def require(condition, reason):
    if not condition:
        raise Blocked('BLOCKED: ' + reason)


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not path.is_symlink() and not path.parent.is_symlink(), 'private path is a symlink')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data if isinstance(data, bytes) else data.encode())
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def read_file(path, sensitive=False):
    path = Path(path).expanduser().resolve(strict=False) if not Path(path).expanduser().is_symlink() else None
    require(path is not None and path.is_file(), 'required regular input file is unavailable')
    if sensitive:
        require(path.stat().st_mode & 0o077 == 0, 'credential input must have mode 0600 or stricter')
    return path.read_bytes()


def credential(path):
    value = read_file(path, sensitive=True).decode().rstrip('\r\n')
    require(bool(value) and len(value) <= 16384 and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            'credential file must contain one nonempty value')
    return value


def name(value, limit=63):
    return isinstance(value, str) and len(value) <= limit and bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', value))


def https_url(value, api=False):
    require(isinstance(value, str), 'HTTPS URL is required')
    parsed = urlsplit(value)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and (not api or parsed.path in ('', '/')),
            'HTTPS URL must have no credentials, query or fragment')
    return value.rstrip('/')


def validate_config(raw):
    cfg = copy.deepcopy(raw)
    for key in ('kubeconfig', 'context', 'expected_server', 'namespace', 'architecture', 'rhbk_url',
                'realm', 'client_id', 'admin_client_id', 'expected_server_version', 'ca_file',
                'admin_secret_file', 'initial_secret_file', 'images'):
        require(key in cfg, 'configuration is incomplete')
    cfg['expected_server'] = https_url(cfg['expected_server'], api=True)
    cfg['rhbk_url'] = https_url(cfg['rhbk_url'])
    require(cfg['architecture'] in ('amd64', 'arm64'), 'architecture must be amd64 or arm64')
    require(name(cfg['namespace']) and cfg['namespace'] not in {'default', 'kube-system', 'kube-public'}
            and not cfg['namespace'].startswith(('openshift', 'kube-')), 'use a dedicated application namespace')
    for key in ('realm', 'client_id', 'admin_client_id', 'context'):
        require(isinstance(cfg[key], str) and 0 < len(cfg[key]) <= 255
                and cfg[key] == cfg[key].strip()
                and not any(ord(c) < 32 or ord(c) == 127 for c in cfg[key]), 'invalid identifier')
    require(cfg['realm'] != 'master' and cfg['admin_client_id'] != cfg['client_id'],
            'use a dedicated realm and independent administrator')
    for key in ('kubeconfig', 'ca_file', 'admin_secret_file', 'initial_secret_file'):
        require(isinstance(cfg[key], str) and bool(cfg[key]), 'input file path is required')
        cfg[key] = str(Path(cfg[key]).expanduser().absolute())
    require(isinstance(cfg['expected_server_version'], str) and
            re.fullmatch(r'26\.\d+\.\d+\.redhat-\d+', cfg['expected_server_version']),
            'pin the exact RHBK 26.x product build')
    for key, default, limit in (
        ('secret_prefix', 'm2m-credential', 48), ('cronjob_name', 'rotation-controller', 52),
        ('state_name', 'rotation-state', 63), ('deployment_name', 'm2m-probe', 63),
        ('lease_name', 'rotation-client', 63), ('profile_name', 'external-rotation-profile', 63),
        ('policy_name', 'external-rotation-policy', 63), ('policy_role', 'external-rotation-managed', 63)):
        cfg.setdefault(key, default)
        require(name(cfg[key], limit), 'invalid configured resource name')
    require(isinstance(cfg['images'], dict), 'provide both workload image digests')
    for image in ('probe', 'controller'):
        value = cfg['images'].get(image, '')
        require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}', value),
                'workload images must be immutable registry references with sha256 digests')
    defaults = {'principal_ttl': 86400, 'rotated_ttl': 120, 'remaining_window': 0,
                'interval': 300, 'hold': 45, 'rollout': 60, 'margin': 15, 'epsilon': 5}
    supplied = cfg.get('rotation', {})
    require(isinstance(supplied, dict) and not set(supplied) - set(defaults), 'invalid timing fields')
    timing = {**defaults, **supplied}
    require(all(isinstance(v, int) and not isinstance(v, bool) for v in timing.values()), 'timings must be integer seconds')
    require(timing['principal_ttl'] > timing['rotated_ttl'] > 0 and
            0 <= timing['remaining_window'] < timing['principal_ttl'] and
            min(timing[k] for k in ('interval', 'rollout', 'margin', 'epsilon')) > 0 and timing['hold'] >= 0,
            'invalid positive timing budget')
    require(timing['hold'] + timing['rollout'] + timing['margin'] <= timing['rotated_ttl'],
            'hold plus rollout plus margin exceeds overlap')
    require(timing['interval'] > timing['rotated_ttl'] + timing['epsilon'],
            'rotation interval must exceed overlap and clock margin')
    require(timing['interval'] + 60 + timing['margin'] < timing['principal_ttl'],
            'principal lifetime must cover interval, Cron polling and margin')
    cfg['rotation'] = timing
    cfg.setdefault('image_pull_secrets', [])
    require(isinstance(cfg['image_pull_secrets'], list) and all(name(x) for x in cfg['image_pull_secrets']),
            'image pull secret names are invalid')
    return cfg


def load_config():
    return validate_config(json.loads(read_file(PRIVATE / CONFIG, sensitive=True)))


def run_oc(kubeconfig, context, args, data=None, check=True):
    require(Path(kubeconfig).is_file() and not Path(kubeconfig).is_symlink(), 'dedicated kubeconfig is missing')
    try:
        # API timeout would append a query parameter to the proxied /status URL.
        timeout = '0' if '--raw' in args and any('/proxy/' in str(x) for x in args) else '30s'
        result = subprocess.run(['oc', '--kubeconfig=' + str(kubeconfig), '--context=' + context,
                                 '--request-timeout=' + timeout, *args],
                                input=json.dumps(data).encode() if data is not None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked('BLOCKED: oc transport failed; mutation outcome may be unknown') from None
    if check and result.returncode:
        raise Blocked('BLOCKED: oc operation failed; raw API errors withheld')
    return result.stdout.decode()


class Backend:
    """Explicit backend for the shared control and observation CLIs."""
    def __init__(self, config=None):
        self.CONFIG = validate_config(config) if config is not None else load_config()
        self.ROOT, self.PRIVATE, self.LABELS = ROOT, PRIVATE, LABELS
        self.NS = self.CONFIG['namespace']
        for key in ('cronjob_name', 'state_name', 'deployment_name', 'lease_name', 'secret_prefix', 'realm'):
            setattr(self, key.upper(), self.CONFIG[key])
        self.TARGET_CLIENT_ID = self.CONFIG['client_id']

    def oc(self, *args, data=None, check=True):
        return run_oc(self.CONFIG['kubeconfig'], self.CONFIG['context'], list(args), data, check)

    def get(self, kind, name=None, ns=UNSET):
        if ns is UNSET:
            ns = self.NS
        args = ['get', kind] + ([name] if name else []) + (['-n', ns] if ns else [])
        raw = self.oc(*args, '-o', 'json', '--ignore-not-found')
        return json.loads(raw) if raw.strip() else None

    def identity(self):
        return json.loads(read_file(PRIVATE / IDENTITY, sensitive=True))

    def cluster(self):
        view = json.loads(self.oc('config', 'view', '--minify', '-o', 'json'))
        clusters = view.get('clusters', [])
        require(len(clusters) == 1 and clusters[0]['cluster'].get('server', '').rstrip('/') == self.CONFIG['expected_server'],
                'selected context does not match expected API server')
        require(clusters[0]['cluster'].get('insecure-skip-tls-verify') is not True,
                'Kubernetes TLS verification must remain enabled')
        system = self.get('namespace', 'kube-system', None)
        require(system and system.get('metadata', {}).get('uid'), 'cluster UID unavailable')
        return system['metadata']['uid']

    def guard(self, require_namespace=True, allow_pending_namespace=False):
        identity = self.identity()
        require(self.cluster() == identity.get('cluster_uid') and identity.get('server') == self.CONFIG['expected_server']
                and identity.get('namespace') == self.NS, 'recorded cluster identity differs')
        namespace = self.get('namespace', self.NS, None)
        if namespace:
            owned = all(namespace['metadata'].get('labels', {}).get(k) == v for k, v in self.labels().items())
            pending = (allow_pending_namespace and not identity.get('namespace_uid') and
                       identity.get('namespace_create_intent') == identity.get('installation_id'))
            require(owned and (namespace['metadata'].get('uid') == identity.get('namespace_uid') or pending),
                    'namespace was not created by this configured installation')
        else:
            require(not require_namespace and not identity.get('namespace_uid'), 'recorded namespace is missing')
        return namespace

    def labels(self):
        return {**LABELS, 'lab.rhbk/installation': self.identity()['installation_id']}

    def meta(self, name, ns=True):
        return {'name': name, 'labels': self.labels(), **({'namespace': self.NS} if ns else {})}

    def save(self, path, obj):
        relative = Path(path)
        require(not relative.is_absolute() and relative.parts and relative.parts[0] == 'evidence'
                and '..' not in relative.parts, 'only local evidence can be saved')
        if len(relative.parts) < 2 or relative.parts[1] != 'openshift':
            relative = Path('evidence/openshift').joinpath(*relative.parts[1:])
        target = (ROOT / relative).resolve()
        require(target.is_relative_to((ROOT / 'evidence/openshift').resolve()), 'only local OpenShift evidence can be saved')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(obj, indent=2) + '\n')

    def apply(self, obj):
        # Shared control.py creates bounded Jobs from the existing CronJob.
        self.guard()
        require(obj.get('kind') == 'Job' and obj.get('metadata', {}).get('namespace') == self.NS
                and obj['metadata'].get('name', '').startswith('rotation-')
                and all(obj['metadata'].get('labels', {}).get(k) == v for k, v in self.labels().items()),
                'backend only creates owned manual controller Jobs')
        require(self.get('job', obj['metadata']['name']) is None, 'manual Job already exists')
        self.oc('create', '-f', '-', data=obj)


def controller_module():
    spec = importlib.util.spec_from_file_location('portable_controller_http', ROOT / 'apps/rotation-controller/controller.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_rhbk(cfg):
    """All remote RHBK operations are reads or token grants; never client mutation."""
    module = controller_module()
    http = module.HTTP(cfg['rhbk_url'], cfg['ca_file'])
    base = '/admin/realms/' + quote(cfg['realm'], safe='')
    token_path = '/realms/' + quote(cfg['realm'], safe='') + '/protocol/openid-connect/token'
    secret = credential(cfg['admin_secret_file'])
    _, auth, _ = http.request('POST', token_path, {'grant_type': 'client_credentials',
        'client_id': cfg['admin_client_id'], 'client_secret': secret}, form=True)
    require(auth.get('access_token'), 'administrator token was not issued')
    token = auth['access_token']

    def get(path):
        return http.request('GET', path, bearer=token)[1]

    server = get('/admin/serverinfo')
    actual_version = (server.get('systemInfo') or {}).get('version')
    require(actual_version, 'serverinfo omitted the build; configure/apply require read-only view-system role and scope, subject to the installed build')
    require(actual_version == cfg['expected_server_version'], 'actual RHBK build differs from configuration')
    features = server.get('features', [])
    normalize = lambda x: str(x).lower().replace('_', '-').split(':')[0]
    matches = [x for x in features if normalize(x.get('name')) == 'client-secret-rotation']
    profile_info = server.get('profileInfo', {})
    enabled = (all(x.get('enabled') is True for x in matches) if matches else
               'client-secret-rotation' in {normalize(x) for key in ('previewFeatures', 'enabledFeatures', 'experimentalFeatures')
                                           for x in profile_info.get(key, [])}
               and 'client-secret-rotation' not in {normalize(x) for x in profile_info.get('disabledFeatures', [])})
    require(enabled, 'server did not confirm client-secret-rotation enabled')
    profiles = get(base + '/client-policies/profiles').get('profiles', [])
    policies = get(base + '/client-policies/policies').get('policies', [])
    selected = [x for x in profiles if x.get('name') == cfg['profile_name']]
    expected = {'expiration-period': cfg['rotation']['principal_ttl'], 'rotated-expiration-period': cfg['rotation']['rotated_ttl'],
                'remaining-rotation-period': cfg['rotation']['remaining_window']}
    require(len(selected) == 1 and selected[0].get('executors') == [{'executor': 'secret-rotation', 'configuration': expected}],
            'selected profile must contain exactly the configured rotation executor')
    selected_policies = [x for x in policies if x.get('name') == cfg['policy_name']]
    require(len(selected_policies) == 1, 'selected policy is missing or ambiguous')
    policy = selected_policies[0]
    conditions = policy.get('conditions', [])
    require(policy.get('enabled') is True and policy.get('profiles') == [cfg['profile_name']] and len(conditions) == 1
            and conditions[0].get('condition') == 'client-roles'
            and conditions[0].get('configuration', {}).get('roles') == [cfg['policy_role']]
            and conditions[0].get('configuration', {}).get('is-negative-logic', False) is False,
            'policy must select only the dedicated client role')
    rotation_profiles = {x.get('name') for x in profiles if any(e.get('executor') == 'secret-rotation' for e in x.get('executors', []))}
    require(not any(x.get('enabled') and x.get('name') != cfg['policy_name'] and set(x.get('profiles', [])) & rotation_profiles for x in policies),
            'another enabled rotation policy makes this import ambiguous; use a dedicated realm')

    def client(client_id):
        matches = [x for x in get(base + '/clients?clientId=' + quote(client_id, safe='')) if x.get('clientId') == client_id]
        require(len(matches) == 1, 'configured client must already exist uniquely')
        path = base + '/clients/' + quote(matches[0]['id'], safe='')
        return path, get(path), get(path + '/roles')

    _, admin, roles = client(cfg['admin_client_id'])
    require(not any(x.get('name') == cfg['policy_role'] for x in roles)
            and not admin.get('attributes', {}).get(META[1]), 'independent administrator must be outside rotation')
    path, target, roles = client(cfg['client_id'])
    require(target.get('enabled') is True and target.get('publicClient') is False and target.get('serviceAccountsEnabled') is True
            and target.get('clientAuthenticatorType') == 'client-secret'
            and any(x.get('name') == cfg['policy_role'] for x in roles), 'target client does not match the demo contract')
    initial = credential(cfg['initial_secret_file'])
    require(get(path + '/client-secret').get('value') == initial, 'initial credential is not the authoritative principal')
    attributes = target.get('attributes', {})
    metadata = {}
    for key in META:
        if attributes.get(key) is not None:
            try:
                value = float(attributes[key])
                require(math.isfinite(value) and value > 0, 'invalid server expiration metadata')
                metadata[key] = value
            except (ValueError, TypeError):
                raise Blocked('BLOCKED: invalid server expiration metadata') from None
    require(META[0] in metadata and META[1] in metadata, 'principal has no server-issued creation and expiration metadata; prepare it first')
    timing = cfg['rotation']
    require(abs(metadata[META[1]] - metadata[META[0]] - timing['principal_ttl']) <= timing['epsilon'],
            'principal stored lifetime differs from the selected profile')
    require(metadata[META[0]] <= time.time() + timing['epsilon'] and metadata[META[1]] > time.time() + 180 + timing['margin'],
            'principal is expired or lacks initial deployment budget')
    require(not attributes.get('client.secret.rotated') or metadata.get(META[3], float('inf')) + timing['epsilon'] < time.time(),
            'previous rotated credential is still active; wait for its grace period before importing')
    _, result, _ = http.request('POST', token_path, {'grant_type': 'client_credentials',
        'client_id': cfg['client_id'], 'client_secret': initial}, form=True)
    require(bool(result.get('access_token')), 'initial credential did not authenticate')
    return {'client_uuid': target['id'], 'metadata': metadata, 'auth_http': 200,
            'server_version': cfg['expected_server_version'], 'verified_at': time.time()}


def configure(path):
    require(not (PRIVATE / CONFIG).exists(), 'configuration already exists; keep its identity or use another checkout')
    cfg = validate_config(json.loads(Path(path).read_text()))
    source = Path(cfg['kubeconfig']).expanduser()
    read_file(source, sensitive=True)
    credential(cfg['admin_secret_file']); credential(cfg['initial_secret_file'])
    read_file(cfg['ca_file']); ssl.create_default_context(cafile=cfg['ca_file'])
    backend = Backend(cfg)
    cluster_uid = backend.cluster()
    require(backend.get('namespace', cfg['namespace'], None) is None, 'namespace already exists; choose a new isolated namespace')
    nodes = json.loads(backend.oc('get', 'nodes', '-l', 'kubernetes.io/arch=' + cfg['architecture'], '-o', 'json'))
    require(bool(nodes.get('items')), 'no cluster node matches the selected image architecture')
    verification = verify_rhbk(cfg)
    flattened = backend.oc('config', 'view', '--minify', '--flatten', '--raw', '-o', 'json')
    # Copy only the explicitly selected context; credentials never reach stdout.
    private_write(PRIVATE / 'openshift.kubeconfig', flattened)
    for field, filename in [('ca_file', 'openshift-ca.crt'), ('admin_secret_file', 'openshift-admin.secret'),
                            ('initial_secret_file', 'openshift-initial.secret')]:
        private_write(PRIVATE / filename, read_file(cfg[field], sensitive=field != 'ca_file'))
        cfg[field] = str(PRIVATE / filename)
    cfg['kubeconfig'] = str(PRIVATE / 'openshift.kubeconfig')
    identity = {'server': cfg['expected_server'], 'cluster_uid': cluster_uid, 'namespace': cfg['namespace'],
                'namespace_uid': None, 'installation_id': str(uuid.uuid4())}
    private_write(PRIVATE / IDENTITY, json.dumps(identity, indent=2))
    private_write(PRIVATE / CONFIG, json.dumps(cfg, indent=2))
    Backend(cfg).save('evidence/configure.json', verification)
    print('PASS: explicit cluster identity, TLS, RHBK policy and initial credential verified; no remote resources changed')


def render(backend=None):
    backend = backend or Backend()
    cfg, labels = backend.CONFIG, backend.labels()
    objects = [json.loads(p.read_text()) for p in sorted((ROOT / 'deploy/rbac').glob('*.json'))]
    objects += [json.loads((ROOT / 'deploy/workloads' / p).read_text()) for p in
                ('m2m-probe-service.json', 'm2m-probe.json', 'rotation-controller.json')]
    replacements = {'rhbk-rotation-lab': backend.NS, 'catalogo-api-cred-v0001': cfg['secret_prefix'] + '-v0001',
                    'rotation-state': cfg['state_name'], 'rotation-catalogo-api': cfg['lease_name']}
    def replace(value):
        if isinstance(value, str): return replacements.get(value, value)
        if isinstance(value, dict): return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list): return [replace(v) for v in value]
        return value
    objects = replace(objects)
    for obj in objects:
        obj['metadata']['namespace'] = backend.NS
        obj['metadata'].setdefault('labels', {}).update(labels)
        if obj['kind'] == 'Role':
            for rule in obj['rules']:
                if rule.get('resources') == ['cronjobs']: rule['resourceNames'] = [cfg['cronjob_name']]
                if rule.get('resources') == ['deployments']: rule['resourceNames'] = [cfg['deployment_name']]
        if obj['kind'] not in ('Deployment', 'CronJob'): continue
        cron = obj['kind'] == 'CronJob'
        obj['metadata']['name'] = cfg['cronjob_name' if cron else 'deployment_name']
        template = obj['spec']['jobTemplate']['spec']['template'] if cron else obj['spec']['template']
        template['metadata'].setdefault('labels', {}).update(labels)
        spec = template['spec']; spec['nodeSelector'] = {'kubernetes.io/arch': cfg['architecture']}
        if cfg['image_pull_secrets']: spec['imagePullSecrets'] = [{'name': x} for x in cfg['image_pull_secrets']]
        container = spec['containers'][0]
        container['image'] = cfg['images']['controller' if cron else 'probe']
        if cron:
            obj['spec']['suspend'] = True
            obj['spec']['jobTemplate']['metadata'].setdefault('labels', {}).update(labels)
            obj['spec']['jobTemplate']['spec']['activeDeadlineSeconds'] = max(240, cfg['rotation']['rotated_ttl'] + 120)
            env = {'RHBK_URL': cfg['rhbk_url'], 'REALM': cfg['realm'], 'ADMIN_CLIENT_ID': cfg['admin_client_id'],
                   'TARGET_CLIENT_ID': cfg['client_id'], 'NAMESPACE': backend.NS, 'SECRET_PREFIX': cfg['secret_prefix'],
                   'CRONJOB_NAME': cfg['cronjob_name'], 'STATE_NAME': cfg['state_name'], 'LEASE_NAME': cfg['lease_name'],
                   'DEPLOYMENT_NAME': cfg['deployment_name']}
            env.update({key: str(cfg['rotation'][value]) for key, value in {
                'HOLD_SECONDS': 'hold', 'ROLLOUT_SECONDS': 'rollout', 'MARGIN_SECONDS': 'margin',
                'ROTATED_TTL': 'rotated_ttl', 'ROTATE_INTERVAL': 'interval', 'EPSILON_SECONDS': 'epsilon'}.items()})
            present = {item['name']: item for item in container['env']}
            present.update({key: {'name': key, 'value': value} for key, value in env.items()})
            container['env'] = list(present.values())
        else:
            for item in container['env']:
                if item['name'] == 'TOKEN_URL':
                    item['value'] = cfg['rhbk_url'] + '/realms/' + quote(cfg['realm'], safe='') + '/protocol/openid-connect/token'
    return {'apiVersion': 'v1', 'kind': 'List', 'items': objects}


def ensure_namespace(backend):
    namespace = backend.guard(require_namespace=False, allow_pending_namespace=True)
    identity = backend.identity()
    if namespace and identity.get('namespace_uid'):
        return
    if not namespace:
        identity['namespace_create_intent'] = identity['installation_id']
        private_write(PRIVATE / IDENTITY, json.dumps(identity, indent=2))
        # Name uniqueness plus a durable installation marker make readback safe
        # after a lost response. Never adopt a namespace without this intent.
        backend.oc('create', '-f', '-', data={'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': backend.meta(backend.NS, ns=False)})
    namespace = backend.get('namespace', backend.NS, None)
    require(namespace and all(namespace['metadata'].get('labels', {}).get(k) == v for k, v in backend.labels().items()),
            'created namespace ownership could not be verified')
    identity['namespace_uid'] = namespace['metadata']['uid']
    private_write(PRIVATE / IDENTITY, json.dumps(identity, indent=2))


def managed_fields_match(actual, desired):
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(key in actual and managed_fields_match(actual[key], value) for key, value in desired.items())
    if isinstance(desired, list):
        return isinstance(actual, list) and len(actual) == len(desired) and all(managed_fields_match(a, b) for a, b in zip(actual, desired))
    return actual == desired


def create_resource(backend, obj):
    backend.guard()
    require(obj['metadata'].get('namespace') == backend.NS, 'resource namespace differs')
    existing = backend.get(obj['kind'], obj['metadata']['name'])
    if existing:
        require(all(existing['metadata'].get('labels', {}).get(k) == v for k, v in backend.labels().items()),
                'resource belongs to another installation')
        # Reapplying initial manifests after a rollout could restore v0001.
        require(managed_fields_match(existing, obj), 'existing resource differs from reviewed managed fields; refusing to overwrite')
        return existing
    backend.oc('create', '-f', '-', data=obj)
    current = backend.get(obj['kind'], obj['metadata']['name'])
    require(managed_fields_match(current, obj), 'resource readback differs from requested managed fields')
    return current


def secret_object(backend, name, values):
    return {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': backend.meta(name), 'type': 'Opaque', 'immutable': True,
            'data': {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}}


def apply(backend=None):
    backend = backend or Backend()
    cfg = backend.CONFIG
    backend.guard(require_namespace=False, allow_pending_namespace=True)
    state = backend.get('configmap', cfg['state_name']) if backend.get('namespace', backend.NS, None) else None
    if state:
        require(all(state['metadata'].get('labels', {}).get(k) == v for k, v in backend.labels().items()), 'existing state belongs to another installation')
        persisted = json.loads(state['data']['state.json'])
        require(persisted.get('generation') == 1 and persisted.get('phase') == 'IDLE' and persisted.get('suspended') is True,
                'installation already ran; use control/reconcile, never reinitialize its generation')
    verification = verify_rhbk(cfg)
    ensure_namespace(backend)
    initial = credential(cfg['initial_secret_file']); admin = credential(cfg['admin_secret_file'])
    for obj in [secret_object(backend, 'rotation-admin-credential', {'client-secret': admin}),
                secret_object(backend, cfg['secret_prefix'] + '-v0001', {'client-secret': initial, 'client-id': cfg['client_id'], 'generation': 'v0001'})]:
        current = create_resource(backend, obj)
        require(current and current.get('data') == obj['data'] and current.get('immutable') is True,
                'immutable credential differs; refusing replacement')
    ca = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': backend.meta('rhbk-route-ca'),
          'data': {'ca.crt': read_file(cfg['ca_file']).decode()}}
    require(create_resource(backend, ca).get('data') == ca['data'], 'existing trust bundle differs')
    state_value = {'phase': 'IDLE', 'generation': 1, 'current_secret': cfg['secret_prefix'] + '-v0001',
                   'last_rotated_at': verification['metadata'][META[0]], 'suspended': True,
                   'target_client_id': cfg['client_id'], 'secret_prefix': cfg['secret_prefix'], 'realm': cfg['realm'],
                   'namespace': backend.NS, 'initial_metadata': verification['metadata'], 'initial_client_uuid': verification['client_uuid']}
    state_obj = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': backend.meta(cfg['state_name']),
                 'data': {'state.json': json.dumps(state_value, sort_keys=True)}}
    existing_state = create_resource(backend, state_obj)
    require(json.loads(existing_state['data']['state.json']) == state_value, 'initial state differs; inspect instead of resetting')
    for obj in render(backend)['items']:
        create_resource(backend, obj)
    backend.save('evidence/apply.json', {**verification, 'namespace': backend.NS, 'generation': 1,
                 'automation_suspended': True, 'workload_readiness': 'NOT_MEASURED'})
    print('APPLIED: demo resources created; automation suspended. Verify both pods before starting a cycle.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    setup = sub.add_parser('configure'); setup.add_argument('--config', required=True)
    sub.add_parser('check')
    sub.add_parser('render')
    sub.add_parser('apply')
    args = parser.parse_args()
    try:
        if args.command == 'configure': configure(args.config)
        elif args.command == 'check':
            backend = Backend(); backend.guard(); print('PASS: configured cluster and namespace identities match')
        elif args.command == 'render':
            result = render(); Backend().save('evidence/resources.json', result)
            print('RENDERED: evidence/openshift/resources.json; no credentials included, nothing applied')
        else: apply()
        return 0
    except Blocked as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # Neither credential values nor raw remote errors may enter the report.
        print('BLOCKED: configuration, TLS or API validation failed; no raw response was logged', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
