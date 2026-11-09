#!/usr/bin/env python3
"""Create the isolated realm and initialize S1 without blindly repeating a POST."""
import argparse
import base64
import importlib.util
import json
import sys
from lab import ROOT, PRIVATE, HOST, NS, oc, get, guard, apply, meta, create_secret, save

spec = importlib.util.spec_from_file_location('exp', ROOT/'tests/integration/post_expiration.py')
exp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exp)


def owned(obj):
    if obj and obj.get('metadata', {}).get('labels', {}).get('lab.rhbk/owner') != 'client-secret-rotation':
        raise RuntimeError('BLOCKED bootstrap encountered a resource not owned by this laboratory')
    return obj


def secret(name, key):
    return base64.b64decode(owned(get('secret', name))['data'][key]).decode()


def feature_enabled(profile, features=None):
    normalize = lambda value: str(value).lower().replace('_', '-').split(':')[0]
    if features is not None:
        matches = [value for value in features if normalize(value.get('name', '')) == 'client-secret-rotation']
        if matches:
            return all(value.get('enabled') is True for value in matches)
    disabled = {normalize(value) for value in profile.get('disabledFeatures', [])}
    listed = {normalize(value) for key in ('previewFeatures', 'experimentalFeatures', 'enabledFeatures')
              for value in profile.get(key, [])}
    return 'client-secret-rotation' in listed and 'client-secret-rotation' not in disabled


def verify_server_version(version):
    expected = json.loads((ROOT/'versions.lock.yaml').read_text()).get('expected_server_version')
    if not isinstance(expected, str) or not expected.startswith('26.6.'):
        raise RuntimeError('BLOCKED expected RHBK 26.6 build missing from versions.lock.yaml')
    if version != expected:
        raise RuntimeError('BLOCKED effective server build differs from versions.lock.yaml')


def verify_workload_images():
    """Do not start the initial TTL while workload images are still placeholders."""
    try:
        images = json.loads((ROOT/'versions.lock.yaml').read_text()).get('application_images', {})
        for name, container_name in [('m2m-probe', 'probe'), ('rotation-controller', 'controller')]:
            digest = images.get(name, {}).get('digest', '')
            if (not isinstance(digest, str) or not digest.startswith('sha256:') or len(digest) != 71
                    or any(char not in '0123456789abcdef' for char in digest[7:])):
                raise ValueError('missing image digest')
            manifest = json.loads((ROOT/'deploy/workloads'/(name+'.json')).read_text())
            template = (manifest['spec']['template'] if name == 'm2m-probe'
                        else manifest['spec']['jobTemplate']['spec']['template'])
            containers = [item for item in template['spec']['containers'] if item.get('name') == container_name]
            expected = 'image-registry.openshift-image-registry.svc:5000/'+NS+'/'+name+'@'+digest
            if len(containers) != 1 or containers[0].get('image') != expected:
                raise ValueError('workload manifest differs from built image')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError('BLOCKED: run make build and verify both image digests before full bootstrap; no client timer started') from None


def model_matches(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and model_matches(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(model_matches(left, right) for left, right in zip(actual, expected))
    return actual == expected


def verify_model(actual, expected, collection):
    for item in expected[collection]:
        matches = [value for value in actual.get(collection, []) if value.get('name') == item['name']]
        if len(matches) != 1:
            raise RuntimeError('BLOCKED policy/profile readback missing or ambiguous')
        if collection == 'policies' and any(condition.get('configuration', {}).get('is-negative-logic', False) is True for condition in matches[0].get('conditions', [])):
            raise RuntimeError('BLOCKED policy condition unexpectedly inverted')
        keys = ('executors',) if collection == 'profiles' else ('enabled', 'conditions', 'profiles')
        for key in keys:
            if not model_matches(matches[0].get(key), item.get(key)):
                raise RuntimeError('BLOCKED policy/profile readback differs: '+key)


def create_once(obj):
    """Create-only, so a repeated bootstrap cannot reset a later generation."""
    oc('create', '-f', '-', data=obj, check=False)
    result = owned(get(obj['kind'].lower(), obj['metadata']['name']))
    if result is None:
        raise RuntimeError('BLOCKED durable bootstrap record could not be created')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--realm-only', action='store_true')
    args = parser.parse_args()
    if not args.realm_only:
        verify_workload_images()
    guard()
    cron = owned(get('cronjob', 'rotation-controller'))
    if cron and cron.get('spec', {}).get('suspend') is not True:
        raise RuntimeError('BLOCKED suspend automation before bootstrap/policy reconciliation')
    jobs = get('jobs')
    if any(job.get('metadata', {}).get('labels', {}).get('lab.rhbk/owner') == 'client-secret-rotation'
           and job.get('status', {}).get('active', 0) for job in (jobs or {}).get('items', [])):
        raise RuntimeError('BLOCKED an active laboratory Job may be using the policy')
    transport = exp.Transport(str(PRIVATE/'route-ca.crt'))
    url = 'https://'+HOST
    code, body, _ = transport.request('POST', url+'/realms/master/protocol/openid-connect/token', {
        'grant_type': 'password', 'client_id': 'admin-cli', 'username': secret('rhbk-bootstrap', 'username'),
        'password': secret('rhbk-bootstrap', 'password')}, form=True)
    if code != 200:
        raise RuntimeError('BLOCKED bootstrap authentication HTTP '+str(code))
    token = body['access_token']

    def admin(method, path, payload=None, allowed=(200, 201, 204)):
        status, value, info = transport.request(method, url+path, payload, token)
        if status not in allowed:
            suffix = '; mutation outcome unknown: do not retry POST' if status == 0 and method == 'POST' else ''
            raise RuntimeError('Admin operation failed '+method+' '+path+' HTTP '+str(status)+suffix)
        return value

    info = admin('GET', '/admin/serverinfo')
    version = info.get('systemInfo', {}).get('version', '')
    verify_server_version(version)
    profile_info = info.get('profileInfo', {})
    save('evidence/server-version.json', {'version': version, 'profileInfo': profile_info,
                                        'features': [{key: feature.get(key) for key in ('name', 'enabled', 'type')} for feature in info.get('features', [])],
                                        'captured_at': exp.utc()})
    if not feature_enabled(profile_info, info.get('features')):
        raise RuntimeError('BLOCKED serverinfo did not confirm active client-secret-rotation feature')
    realm = 'lab-secret-rotation'
    base = '/admin/realms/'+realm
    existing = admin('GET', base, allowed=(200, 404))
    if 'realm' not in existing:
        admin('POST', '/admin/realms', {'realm': realm, 'enabled': True, 'sslRequired': 'external',
              'accessTokenLifespan': 600, 'attributes': {'lab.rhbk/owner': 'client-secret-rotation'}})
    elif existing.get('attributes', {}).get('lab.rhbk/owner') != 'client-secret-rotation':
        raise RuntimeError('BLOCKED foreign realm')
    # Only this explicitly-owned realm is modified. Readback validates exact keys.
    for suffix, filename in [('profiles', 'profile.json'), ('policies', 'policy.json')]:
        payload = json.loads((ROOT/'realm'/filename).read_text())
        current_model = admin('GET', base+'/client-policies/'+suffix)
        try:
            verify_model(current_model, payload, suffix)
        except RuntimeError:
            admin('PUT', base+'/client-policies/'+suffix, payload)
        verify_model(admin('GET', base+'/client-policies/'+suffix), payload, suffix)

    def resolve(client_id):
        clients = admin('GET', base+'/clients?clientId='+client_id)
        exact = [client for client in clients if client.get('clientId') == client_id]
        if len(exact) > 1:
            raise RuntimeError('BLOCKED client ID not unique')
        return exact[0]['id'] if exact else None

    def create_client(name):
        cid = resolve(name)
        if not cid:
            admin('POST', base+'/clients', {'clientId': name, 'protocol': 'openid-connect',
                  'publicClient': False, 'clientAuthenticatorType': 'client-secret',
                  'serviceAccountsEnabled': True, 'standardFlowEnabled': False,
                  'directAccessGrantsEnabled': False, 'implicitFlowEnabled': False,
                  'fullScopeAllowed': False, 'attributes': {'lab.owner': 'client-secret-rotation'}})
            cid = resolve(name)
        if admin('GET', base+'/clients/'+cid).get('attributes', {}).get('lab.owner') != 'client-secret-rotation':
            raise RuntimeError('BLOCKED client is not owned by this laboratory')
        return cid

    aid = create_client('rotation-admin')
    admin_roles = admin('GET', base+'/clients/'+aid+'/roles')
    admin_meta = exp.metadata(admin('GET', base+'/clients/'+aid))
    if any(role.get('name') == exp.ROLE for role in admin_roles) or admin_meta['expires'] is not None:
        raise RuntimeError('BLOCKED independent administrator is covered by an expiration policy')
    uid = admin('GET', base+'/clients/'+aid+'/service-account-user')['id']
    realm_management = resolve('realm-management')
    roles = [admin('GET', base+'/clients/'+realm_management+'/roles/'+name)
             for name in ['manage-clients', 'view-clients', 'query-clients', 'view-realm']]
    admin('POST', base+'/users/'+uid+'/role-mappings/clients/'+realm_management, roles)
    # fullScopeAllowed=false: explicitly allow exactly these runtime roles in tokens.
    admin('POST', base+'/clients/'+aid+'/scope-mappings/clients/'+realm_management, roles)
    # Current RHBK build enforces the dedicated Admin API scope/audience.
    admin_scopes = [scope for scope in admin('GET', base+'/client-scopes') if scope.get('name') == 'kc-admin-api']
    if len(admin_scopes) != 1:
        raise RuntimeError('BLOCKED dedicated Admin API scope unavailable in selected build')
    admin('PUT', base+'/clients/'+aid+'/default-client-scopes/'+admin_scopes[0]['id'])
    credential = admin('GET', base+'/clients/'+aid+'/client-secret')['value']
    create_secret('rotation-admin-credential', {'client-secret': credential}, True)
    if secret('rotation-admin-credential', 'client-secret') != credential:
        raise RuntimeError('BLOCKED immutable administrator credential differs from server')
    secret_file = PRIVATE/'rotation-admin.secret'
    secret_file.write_text(credential)
    secret_file.chmod(0o600)
    runtime = {'RHBK_URL': url, 'RHBK_CA_FILE': str(PRIVATE/'route-ca.crt'),
               'ADMIN_CLIENT_SECRET_FILE': str(secret_file), 'ADMIN_CLIENT_ID': 'rotation-admin',
               'REALM': realm, 'RHBK_SERVER_VERSION': version, 'KUBECONFIG': str(PRIVATE/'crc.kubeconfig')}
    save('.private/runtime.json', runtime)
    (PRIVATE/'runtime.json').chmod(0o600)
    save('evidence/bootstrap.json', {'version': version, 'admin_client': 'rotation-admin',
         'admin_roles': [role['name'] for role in roles], 'admin_excluded_from_policy': True,
         'feature_active': True, 'profile_and_policy_readback_verified': True, 'timestamp': exp.utc()})
    if args.realm_only:
        print('Realm, policy and independent administrator ready; no main-client timer started')
        return

    state_object = owned(get('configmap', 'rotation-state'))
    if state_object:
        print('Existing main generation preserved; use reconcile to continue.')
    else:
        cid = create_client('catalogo-api')
        client_path = base+'/clients/'+cid
        roles = admin('GET', client_path+'/roles')
        if not any(role.get('name') == exp.ROLE for role in roles):
            admin('POST', client_path+'/roles', {'name': exp.ROLE,
                  'description': 'Client-policy selector for this isolated laboratory'})
        intent = owned(get('configmap', 'bootstrap-initialization'))
        staged = owned(get('secret', 'catalogo-api-cred-v0001'))
        before_secret = owned(get('secret', 'catalogo-api-bootstrap-before'))
        if staged:
            # A previous run may have failed after staging S1 but before writing state.
            current = secret('catalogo-api-cred-v0001', 'client-secret')
            if admin('GET', client_path+'/client-secret')['value'] != current:
                raise RuntimeError('BLOCKED staged initial credential differs from server; no POST repeated')
        elif not intent:
            previous = admin('GET', client_path+'/client-secret')['value']
            create_secret('catalogo-api-bootstrap-before', {'client-secret': previous}, True)
            if secret('catalogo-api-bootstrap-before', 'client-secret') != previous:
                raise RuntimeError('BLOCKED previous bootstrap credential is ambiguous')
            # Atomic create claims the single permitted initialization POST. Any crash
            # before/after it leaves a durable record; later runs only reconcile GET.
            import uuid
            attempt = str(uuid.uuid4())
            record = {'phase': 'ROTATING', 'attempt': attempt, 'client_uuid': cid,
                      'recorded_at': exp.utc(), 'pre_metadata': exp.metadata(admin('GET', client_path))}
            intent = create_once({'apiVersion': 'v1', 'kind': 'ConfigMap',
                'metadata': meta('bootstrap-initialization'), 'data': {'intent.json': json.dumps(record)}})
            if json.loads(intent['data']['intent.json'])['attempt'] != attempt:
                raise RuntimeError('BLOCKED another bootstrap owns initialization; reconcile on a later run')
            current = admin('POST', client_path+'/client-secret')['value']
        else:
            record = json.loads(intent['data']['intent.json'])
            if record.get('client_uuid') != cid or not before_secret:
                raise RuntimeError('BLOCKED bootstrap intent does not identify the current client safely')
            previous = secret('catalogo-api-bootstrap-before', 'client-secret')
            current = admin('GET', client_path+'/client-secret')['value']
            representation = admin('GET', client_path)
            rotated = representation.get('attributes', {}).get('client.secret.rotated')
            if current == previous or rotated != previous:
                raise RuntimeError('BLOCKED initialization outcome cannot be proven; no POST repeated')
            print('Recovered prior initialization through readback; no additional POST sent')
        measured = exp.metadata(admin('GET', client_path))
        if not measured['expires'] or not measured['created']:
            raise RuntimeError('BLOCKED policy did not establish real timestamps')
        status, body, _ = transport.request('POST', url+'/realms/'+realm+'/protocol/openid-connect/token', {
            'grant_type': 'client_credentials', 'client_id': 'catalogo-api', 'client_secret': current}, form=True)
        if status != 200 or not body.get('access_token'):
            raise RuntimeError('BLOCKED initial generation does not authenticate; no extra POST sent')
        create_secret('catalogo-api-cred-v0001', {'client-secret': current, 'client-id': 'catalogo-api',
                                               'generation': 'v0001'}, True)
        if secret('catalogo-api-cred-v0001', 'client-secret') != current:
            raise RuntimeError('BLOCKED immutable initial credential differs from server')
        state = {'phase': 'IDLE', 'generation': 1, 'current_secret': 'catalogo-api-cred-v0001',
                 'last_rotated_at': measured['created'], 'suspended': True}
        create_once({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': meta('rotation-state'),
                     'data': {'state.json': json.dumps(state)}})
        save('evidence/initial-generation.json', {'client_id': 'catalogo-api', 'generation': 'v0001',
             'metadata': measured, 'fresh_auth_http': status, 'timestamp': exp.utc()})
        print('Initial generation staged with verified expiration metadata; deploying application')
    for path in sorted((ROOT/'deploy/rbac').glob('*.json')):
        apply(json.loads(path.read_text()))
    for path in sorted((ROOT/'deploy/workloads').glob('*.json')):
        obj = json.loads(path.read_text())
        # Reapplying an initial PodTemplate would roll the app back to v0001.
        if not owned(get(obj['kind'].lower(), obj['metadata']['name'])):
            apply(obj)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
