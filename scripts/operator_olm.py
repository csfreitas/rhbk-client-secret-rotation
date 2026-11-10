#!/usr/bin/env python3
"""Pinned, manually approved OLM installation in the guarded CRC namespace.

No other Subscription is changed. Existing shared CRD specifications must be
semantically identical after API defaulting. A manual Deployment is stopped,
then updated in place by OLM; this helper never deletes CRDs or workloads.
"""
import argparse
import base64
from collections import Counter
import copy
import datetime
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import lab

PACKAGE = 'rhbk-operator'
CHANNEL = 'stable-v26.6'
CSV = 'rhbk-operator.v26.6.9-opr.1'
VERSION = '26.6.9-opr.1'
SOURCE = 'redhat-operators'
SOURCE_NS = 'openshift-marketplace'
CRDS = {'keycloaks.k8s.keycloak.org', 'keycloakrealmimports.k8s.keycloak.org'}
OG = 'rhbk-rotation-lab'


def require(condition, message):
    if not condition:
        raise RuntimeError('BLOCKED: '+message)


def owned(obj):
    require(obj and all(obj.get('metadata', {}).get('labels', {}).get(k) == v for k, v in lab.LABELS.items()),
            'resource is not owned by this laboratory')


def record(name, value):
    directory = lab.ROOT/'evidence/operator-olm'
    require(not directory.is_symlink(), 'evidence directory must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/(name+'.json')
    require(not path.is_symlink(), 'evidence file must not be a symlink')
    data = json.dumps(value, indent=2, sort_keys=True)+'\n'
    descriptor, temporary = tempfile.mkstemp(prefix='.olm-', dir=directory)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def all_objects(kind):
    return json.loads(lab.oc('get', kind, '--all-namespaces', '-o', 'json')).get('items', [])


def desired_resources():
    obj = json.loads((lab.ROOT/'deploy/operator/resources.json').read_text())
    require(obj.get('kind') == 'List' and len(obj.get('items', [])) == 2, 'expected only OperatorGroup and Subscription manifests')
    group, subscription = obj['items']
    require(group.get('apiVersion') == 'operators.coreos.com/v1' and group.get('kind') == 'OperatorGroup'
            and group['metadata']['name'] == OG and group['metadata'].get('namespace') == lab.NS
            and group.get('spec') == {'targetNamespaces': [lab.NS]}, 'unexpected OperatorGroup manifest')
    require(subscription.get('apiVersion') == 'operators.coreos.com/v1alpha1' and subscription.get('kind') == 'Subscription'
            and subscription['metadata']['name'] == PACKAGE and subscription['metadata'].get('namespace') == lab.NS
            and subscription.get('spec') == {'channel': CHANNEL, 'name': PACKAGE, 'source': SOURCE,
                'sourceNamespace': SOURCE_NS, 'installPlanApproval': 'Manual', 'startingCSV': CSV}, 'unexpected Subscription manifest')
    owned(group); owned(subscription)
    lock = json.loads((lab.ROOT/'versions.lock.yaml').read_text())
    require(lock['operator']['version'] == VERSION, 'OLM version differs from versions.lock.yaml')
    return group, subscription, lock


def verify_scope():
    groups = all_objects('operatorgroups.operators.coreos.com')
    local = [g for g in groups if g['metadata'].get('namespace') == lab.NS]
    require(len(local) <= 1, 'multiple OperatorGroups in laboratory namespace')
    if local:
        group = local[0]; owned(group)
        require(group['metadata']['name'] == OG and group.get('spec', {}).get('targetNamespaces') == [lab.NS]
                and not group['spec'].get('selector'), 'OperatorGroup scope differs from laboratory namespace')
        resolved = group.get('status', {}).get('namespaces')
        require(resolved is None or resolved == [lab.NS], 'OperatorGroup resolved scope differs')
    for group in groups:
        if group['metadata'].get('namespace') == lab.NS:
            continue
        provided = group['metadata'].get('annotations', {}).get('olm.providedAPIs', '').split(',')
        if any(api.startswith(('Keycloak.', 'KeycloakRealmImport.')) and api.endswith('.k8s.keycloak.org') for api in provided):
            targets = group.get('status', {}).get('namespaces', group.get('spec', {}).get('targetNamespaces'))
            require(targets and '' not in targets and '*' not in targets and lab.NS not in targets,
                    'another OperatorGroup reserves Keycloak APIs in this laboratory')
    others = []
    for csv in all_objects('clusterserviceversions.operators.coreos.com'):
        meta = csv['metadata']; ns = meta.get('namespace')
        if csv.get('status', {}).get('reason') == 'Copied' or meta.get('labels', {}).get('olm.copiedFrom'):
            continue
        if ns == lab.NS:
            require(meta['name'] == CSV, 'another CSV exists in laboratory namespace')
        provided = {r.get('name') for r in csv.get('spec', {}).get('customresourcedefinitions', {}).get('owned', [])}
        if not (provided & CRDS):
            continue
        if ns == lab.NS:
            require(meta['name'] == CSV, 'another Keycloak CSV exists in laboratory namespace')
            continue
        candidates = [g for g in groups if g['metadata'].get('namespace') == ns]
        require(len(candidates) == 1, 'other Keycloak CSV has ambiguous OperatorGroup')
        group = candidates[0]
        targets = group.get('status', {}).get('namespaces', group.get('spec', {}).get('targetNamespaces'))
        require(targets and '' not in targets and '*' not in targets and lab.NS not in targets,
                'another OperatorGroup may reconcile this laboratory')
        others.append({'namespace': ns, 'name': meta['name'], 'uid': meta['uid'], 'version': csv['spec'].get('version')})
    subscriptions = []
    for sub in all_objects('subscriptions.operators.coreos.com'):
        meta = sub['metadata']
        if meta.get('namespace') == lab.NS or sub.get('spec', {}).get('name') != PACKAGE:
            continue
        require(sub['spec'].get('installPlanApproval') == 'Manual', 'another RHBK Subscription permits automatic updates')
        subscriptions.append({'namespace': meta['namespace'], 'name': meta['name'], 'uid': meta['uid'],
            'channel': sub['spec'].get('channel'), 'approval': 'Manual',
            'installedCSV': sub.get('status', {}).get('installedCSV'), 'currentCSV': sub.get('status', {}).get('currentCSV')})
    return {'csvs': sorted(others, key=lambda x: (x['namespace'], x['name'])),
            'subscriptions': sorted(subscriptions, key=lambda x: (x['namespace'], x['name']))}


def validate_existing(obj, desired):
    owned(obj)
    require(obj['metadata']['name'] == desired['metadata']['name'] and obj['metadata'].get('namespace') == lab.NS,
            'OLM resource identity differs')
    for key, value in desired['spec'].items():
        require(obj.get('spec', {}).get(key) == value, 'existing OLM resource configuration differs')
    if desired['kind'] == 'Subscription':
        require(not obj.get('spec', {}).get('config'), 'unexpected Subscription runtime overrides')


def prepare():
    lab.guard()
    group, subscription, _ = desired_resources()
    package = lab.get('packagemanifests.packages.operators.coreos.com', PACKAGE, SOURCE_NS)
    status = (package or {}).get('status', {})
    require(status.get('catalogSource') == SOURCE and status.get('catalogSourceNamespace') == SOURCE_NS,
            'Operator package is not from the expected Red Hat catalog')
    channels = [c for c in status.get('channels', []) if c.get('name') == CHANNEL]
    require(len(channels) == 1 and channels[0].get('currentCSV') == CSV,
            'catalog channel does not resolve to the pinned CSV; review versions before proceeding')
    peers = verify_scope()
    for desired in (group, subscription):
        existing = lab.get(desired['kind'], desired['metadata']['name'])
        if not existing:
            # CREATE cannot overwrite a concurrently created object; rerun validates it.
            lab.oc('create', '-f', '-', data=desired)
            existing = lab.get(desired['kind'], desired['metadata']['name'])
        validate_existing(existing, desired)
    record('prepare', {'at': utc(), 'csv': CSV, 'approval': 'Manual', 'other_installations': peers})
    return {'status': 'PREPARED', 'csv': CSV, 'next': 'inspect pending InstallPlan, then approve --plan NAME'}


def validate_plan(plan, subscription, lock):
    require(plan and plan['metadata'].get('namespace') == lab.NS, 'InstallPlan not found in laboratory namespace')
    ref = subscription.get('status', {}).get('installPlanRef', {})
    require(ref.get('name') == plan['metadata']['name'] and ref.get('uid') == plan['metadata']['uid'],
            'InstallPlan is not the exact plan referenced by the Subscription')
    require(any(r.get('kind') == 'Subscription' and r.get('name') == PACKAGE and r.get('uid') == subscription['metadata']['uid']
                for r in plan['metadata'].get('ownerReferences', [])), 'InstallPlan Subscription ownership differs')
    require(plan.get('spec', {}).get('approval') == 'Manual' and plan['spec'].get('clusterServiceVersionNames') == [CSV],
            'InstallPlan approval or CSV set differs')
    manifests = []; bundles = {}
    for step in plan.get('status', {}).get('plan', []):
        resource = step.get('resource', {})
        require(step.get('resolving') == CSV and resource.get('kind') in {'ClusterServiceVersion', 'CustomResourceDefinition',
                'ServiceAccount', 'Role', 'RoleBinding', 'ClusterRole', 'ClusterRoleBinding'},
                'InstallPlan contains unexpected dependencies or resource kinds')
        require(resource.get('sourceName', resource.get('catalogSource', SOURCE)) == SOURCE
                and resource.get('sourceNamespace', resource.get('catalogSourceNamespace', SOURCE_NS)) == SOURCE_NS,
                'InstallPlan resource comes from another catalog')
        value = json.loads(resource.get('manifest', '{}'))
        if value.get('kind') == 'ConfigMap' and 'metadata' not in value:
            name = value.get('name')
            require(value.get('namespace') == SOURCE_NS and value.get('catalogSourceName') == SOURCE
                    and value.get('catalogSourceNamespace') == SOURCE_NS and not value.get('replaces'),
                    'InstallPlan bundle reference differs from expected source or install mode')
            require(isinstance(name, str) and re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', name), 'invalid bundle ConfigMap name')
            if name not in bundles:
                bundles[name] = resolve_bundle(name, lock)
            require(len(bundles) == 1, 'InstallPlan references multiple bundles')
            value = bundles[name].get((resource.get('kind'), resource.get('name')))
            require(value is not None, 'InstallPlan resource is not derived from the verified bundle')
        require(value.get('kind') == resource.get('kind') and value.get('metadata', {}).get('name') == resource.get('name'),
                'InstallPlan manifest identity differs')
        manifests.append(value)
    csvs = [o for o in manifests if o['kind'] == 'ClusterServiceVersion']
    definitions = [o for o in manifests if o['kind'] == 'CustomResourceDefinition']
    require(len(csvs) == 1 and csvs[0]['metadata']['name'] == CSV and csvs[0].get('spec', {}).get('version') == VERSION,
            'InstallPlan lacks the pinned CSV manifest')
    require(len(definitions) == 2 and {o['metadata']['name'] for o in definitions} == CRDS,
            'InstallPlan must contain exactly the two expected Keycloak CRDs')
    csv = csvs[0]
    require(any(m.get('type') == 'OwnNamespace' and m.get('supported') is True for m in csv['spec'].get('installModes', [])),
            'CSV does not support OwnNamespace')
    deployments = csv['spec'].get('install', {}).get('spec', {}).get('deployments', [])
    require(len(deployments) == 1 and deployments[0]['name'] == PACKAGE, 'unexpected CSV Deployment set')
    containers = deployments[0]['spec']['template']['spec']['containers']
    require(len(containers) == 1 and containers[0].get('image') == lock['operator']['image'], 'CSV Operator image differs from verified pin')
    require(deployments[0]['spec']['template']['spec'].get('serviceAccountName') == PACKAGE,
            'CSV Deployment service account differs')
    validate_plan_rbac(manifests, csv)
    lookups = plan.get('status', {}).get('bundleLookups', [])
    require(not bundles or len(lookups) == 1, 'referenced bundle requires one exact InstallPlan bundle lookup')
    for lookup in lookups:
        require(lookup.get('identifier') == CSV and lookup.get('path') == lock['images']['bundle']['original'],
                'InstallPlan bundle differs from verified pin')
        require(lookup.get('catalogSourceRef', {'name': SOURCE, 'namespace': SOURCE_NS}) == {'name': SOURCE, 'namespace': SOURCE_NS},
                'InstallPlan bundle lookup catalog differs')
    return definitions, deployments[0]


def yaml_object(raw):
    """Use the existing oc YAML parser; no optional Python package is required."""
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        with tempfile.TemporaryDirectory(prefix='olm-manifest-') as directory:
            path = Path(directory)/'manifest.yaml'; path.write_bytes(raw)
            return json.loads(lab.oc('create', '--dry-run=client', '--validate=false', '-f', str(path), '-o', 'json'))


def resolve_bundle(name, lock):
    cm = lab.get('configmap', name, SOURCE_NS)
    require(cm and cm.get('kind') == 'ConfigMap' and cm['metadata'].get('name') == name
            and cm['metadata'].get('namespace') == SOURCE_NS, 'bundle ConfigMap identity differs')
    annotations = cm['metadata'].get('annotations', {})
    require(annotations.get('olm.sourceImage') == lock['images']['bundle']['original']
            and annotations.get('olm.contentEncoding') == 'gzip+base64'
            and annotations.get('operators.operatorframework.io.bundle.package.v1') == PACKAGE,
            'bundle ConfigMap image, encoding or package differs')
    source = lab.get('catalogsource', SOURCE, SOURCE_NS)
    require(source and any(ref.get('kind') == 'CatalogSource' and ref.get('name') == SOURCE
                and ref.get('uid') == source['metadata']['uid'] for ref in cm['metadata'].get('ownerReferences', [])),
            'bundle ConfigMap is not owned by the selected CatalogSource')
    expected_files = {'keycloak-operator.clusterserviceversion.yaml',
        'keycloakrealmimports.k8s.keycloak.org-v1.crd.yml', 'keycloaks.k8s.keycloak.org-v1.crd.yml'}
    encoded = cm.get('binaryData', {})
    require(set(encoded) == expected_files and not cm.get('data'), 'bundle file set differs from expected CSV and CRDs')
    objects = {}; hashes = {}
    for filename, value in encoded.items():
        require(isinstance(value, str) and len(value) < 4*1024*1024, 'encoded bundle manifest is too large')
        # binaryData's Kubernetes base64 wraps the OLM gzip+base64 payload.
        compressed = base64.b64decode(base64.b64decode(value, validate=True), validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(16*1024*1024+1)
        require(len(raw) <= 16*1024*1024, 'expanded bundle manifest is too large')
        obj = yaml_object(raw); key = (obj.get('kind'), obj.get('metadata', {}).get('name'))
        require(key not in objects, 'duplicate object in bundle')
        objects[key] = obj; hashes[filename] = hashlib.sha256(raw).hexdigest()
    require(set(objects) == {('ClusterServiceVersion', CSV), *{('CustomResourceDefinition', name) for name in CRDS}},
            'bundle object set differs from the pinned CSV and CRDs')
    csv = objects[('ClusterServiceVersion', CSV)]
    for obj in generated_rbac(csv):
        objects[(obj['kind'], obj['metadata']['name'])] = obj
    record('bundle-review', {'at': utc(), 'configmap': name, 'uid': cm['metadata']['uid'],
            'resourceVersion': cm['metadata']['resourceVersion'], 'source_image': annotations['olm.sourceImage'],
            'manifest_sha256': hashes, 'rbac_validation': 'reconstructed from CSV using OLM deterministic permission-name algorithm'})
    return objects


def permission_name(permission, cluster=False):
    """OLM resolver/rbac.go generateName: base62(sha224(Go JSON + newline))."""
    rules = []
    for rule in permission['rules']:
        # Go PolicyRule struct field order, including its omitempty behavior.
        rules.append({key: rule[key] for key in ('verbs', 'apiGroups', 'resources', 'resourceNames', 'nonResourceURLs')
                      if key == 'verbs' or rule.get(key)})
    value = {'name': CSV}
    if cluster:value['namespace'] = lab.NS
    value['permission'] = {'serviceAccountName': permission['serviceAccountName'], 'rules': rules}
    raw = json.dumps(value, separators=(',', ':'), ensure_ascii=False)
    for char, escaped in [('&', '\\u0026'), ('<', '\\u003c'), ('>', '\\u003e'), ('\u2028', '\\u2028'), ('\u2029', '\\u2029')]:
        raw = raw.replace(char, escaped)
    number = int.from_bytes(hashlib.sha224((raw+'\n').encode()).digest(), 'big')
    alphabet = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'; suffix = ''
    while number:
        number, digit = divmod(number, 62); suffix = alphabet[digit]+suffix
    suffix = suffix or '0'
    base = CSV if cluster else CSV+'-'+permission['serviceAccountName']
    if len(base)+len(suffix) > 63:base = base[:63-len(suffix)-1]
    return base+'-'+suffix


def generated_rbac(csv):
    """Reconstruct OLM-generated resources absent from unpacked bundle files.

    Algorithm: operator-framework/operator-lifecycle-manager, resolver/rbac.go
    and pkg/lib/kubernetes/pkg/util/hash/hash.go. A name mismatch fails closed.
    """
    result = [{'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': PACKAGE, 'namespace': lab.NS}}]
    strategy = csv.get('spec', {}).get('install', {}).get('spec', {})
    for field, kind, binding in [('permissions', 'Role', 'RoleBinding'), ('clusterPermissions', 'ClusterRole', 'ClusterRoleBinding')]:
        for permission in strategy.get(field, []):
            require(permission.get('serviceAccountName') == PACKAGE, 'CSV uses an unexpected service account')
            canonical_rules(permission.get('rules', []))
            name = permission_name(permission, cluster=kind == 'ClusterRole')
            meta = {'name': name, **({'namespace': lab.NS} if kind == 'Role' else {})}
            result.append({'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': kind, 'metadata': meta,
                           'rules': copy.deepcopy(permission['rules'])})
            result.append({'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': binding, 'metadata': copy.deepcopy(meta),
                           'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': kind, 'name': name},
                           'subjects': [{'kind': 'ServiceAccount', 'name': PACKAGE, 'namespace': lab.NS}]})
    return result


def canonical_rules(rules):
    """RBAC arrays are sets; ignore ordering, never add or discard a permission."""
    require(isinstance(rules, list), 'RBAC rules must be a list')
    normalized = []
    allowed = {'apiGroups', 'resources', 'resourceNames', 'verbs', 'nonResourceURLs'}
    for rule in rules:
        require(isinstance(rule, dict) and set(rule).issubset(allowed), 'unexpected RBAC rule fields')
        require(all(isinstance(values, list) and all(isinstance(value, str) for value in values)
                    for values in rule.values()), 'invalid RBAC rule values')
        normalized.append({key: sorted(set(values)) for key, values in rule.items() if values})
    return json.dumps(sorted(normalized, key=lambda value: json.dumps(value, sort_keys=True)), sort_keys=True)


def validate_plan_rbac(manifests, csv):
    """OLM-generated RBAC must exactly realize this CSV's permission blocks."""
    strategy = csv['spec']['install']['spec']
    expected = {}
    for field, kind in [('permissions', 'Role'), ('clusterPermissions', 'ClusterRole')]:
        blocks = strategy.get(field, [])
        require(all(block.get('serviceAccountName') == PACKAGE for block in blocks),
                'CSV grants permissions to an unexpected service account')
        expected[kind] = Counter(canonical_rules(block.get('rules', [])) for block in blocks)
    resources = [obj for obj in manifests if obj['kind'] not in {'ClusterServiceVersion', 'CustomResourceDefinition'}]
    accounts = [obj for obj in resources if obj['kind'] == 'ServiceAccount']
    require(len(accounts) == 1 and accounts[0]['metadata']['name'] == PACKAGE
            and accounts[0].get('apiVersion') == 'v1'
            and accounts[0]['metadata'].get('namespace', lab.NS) in {'', lab.NS}
            and not accounts[0].get('secrets') and not accounts[0].get('imagePullSecrets'),
            'InstallPlan must contain only the expected namespace service account')
    seen = set(); roles = {}; actual = {'Role': Counter(), 'ClusterRole': Counter()}
    for obj in resources:
        kind, meta = obj['kind'], obj['metadata']
        key = (kind, meta['name'])
        require(key not in seen, 'duplicate RBAC resource in InstallPlan'); seen.add(key)
        require(isinstance(meta['name'], str) and len(meta['name']) <= 253
                and re.fullmatch(r'[A-Za-z0-9](?:[-A-Za-z0-9.]*[A-Za-z0-9])?', meta['name']), 'invalid RBAC resource name')
        if kind == 'ServiceAccount':
            continue
        require(obj.get('apiVersion') == 'rbac.authorization.k8s.io/v1', 'unexpected RBAC API version')
        require(meta.get('namespace', '') in ({'', lab.NS} if kind in {'Role', 'RoleBinding', 'ClusterRoleBinding'} else {''}),
                'RBAC resource targets another namespace')
        require(not obj.get('aggregationRule'), 'RBAC aggregation is not declared by CSV permissions')
        if kind in {'Role', 'ClusterRole'}:
            rules = canonical_rules(obj.get('rules', [])); actual[kind][rules] += 1
            roles[key] = obj
    require(actual == expected, 'InstallPlan RBAC rules differ from CSV permissions')
    bound = Counter()
    for obj in resources:
        if obj['kind'] not in {'RoleBinding', 'ClusterRoleBinding'}:
            continue
        expected_kind = 'Role' if obj['kind'] == 'RoleBinding' else 'ClusterRole'
        ref = obj.get('roleRef', {})
        key = (ref.get('kind'), ref.get('name'))
        require(ref.get('apiGroup') == 'rbac.authorization.k8s.io' and ref.get('kind') == expected_kind and key in roles,
                'RBAC binding references a role outside the reviewed InstallPlan')
        subjects = obj.get('subjects', [])
        require(len(subjects) == 1 and subjects[0].get('kind') == 'ServiceAccount'
                and subjects[0].get('name') == PACKAGE and subjects[0].get('namespace') == lab.NS
                and subjects[0].get('apiGroup', '') == '', 'RBAC binding grants access outside the laboratory service account')
        bound[key] += 1
    require(bound == Counter({key: 1 for key in roles}), 'InstallPlan must bind each reviewed role exactly once')


def verify_crds(definitions):
    require(len(definitions) == 2 and {obj.get('metadata', {}).get('name') for obj in definitions} == CRDS,
            'CRD review requires exactly the two Keycloak definitions')
    results = []
    for desired in definitions:
        name = desired['metadata']['name']
        require(desired.get('apiVersion') == 'apiextensions.k8s.io/v1' and desired.get('spec', {}).get('group') == 'k8s.keycloak.org'
                and desired['spec'].get('scope') == 'Namespaced'
                and any(v.get('name') == 'v2beta1' and v.get('served') for v in desired['spec'].get('versions', [])),
                'InstallPlan CRD is incompatible with the laboratory API')
        existing = lab.get('customresourcedefinition', name, None)
        if existing:
            candidate = {'apiVersion': desired['apiVersion'], 'kind': desired['kind'],
                'metadata': {'name': name, 'resourceVersion': existing['metadata']['resourceVersion']}, 'spec': desired['spec']}
            # Server dry-run supplies API defaults; no shared CRD is written here.
            defaulted = json.loads(lab.oc('replace', '--dry-run=server', '-f', '-', '-o', 'json', data=candidate))
            matches = defaulted.get('spec') == existing.get('spec')
            digest = lambda spec: hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            results.append({'name': name, 'uid': existing['metadata']['uid'], 'spec_matches': matches,
                            'existing_spec_sha256': digest(existing['spec']), 'planned_spec_sha256': digest(defaulted['spec'])})
        else:
            results.append({'name': name, 'absent': True, 'spec_matches': True})
    record('crd-review', {'at': utc(), 'crds': results})
    require(all(r['spec_matches'] for r in results), 'shared CRD specification would change; inspect evidence/operator-olm/crd-review.json')
    return results


def quiescent():
    cron = lab.get('cronjob', 'rotation-controller')
    state = lab.get('configmap', 'rotation-state')
    if cron or state:
        owned(cron); owned(state)
        value = json.loads(state['data']['state.json'])
        require(cron['spec'].get('suspend') is True and value.get('suspended') is True
                and value.get('phase') in {'IDLE', 'COMPLETE'}, 'rotation automation must be suspended in a terminal state')
    jobs = lab.get('jobs') or {'items': []}
    require(not any(j.get('status', {}).get('active', 0) or
                (not j.get('status', {}).get('completionTime') and not any(c.get('type') in {'Complete', 'Failed'} and c.get('status') == 'True'
                 for c in j.get('status', {}).get('conditions', []))) for j in jobs.get('items', [])), 'active or pending Jobs prevent Operator handoff')


def snapshot_manual(dep):
    # All known literal variables are non-secret. Refuse unexpected values rather
    # than recording a potentially credential-bearing Deployment in evidence.
    allowed = {'RELATED_IMAGE_KEYCLOAK', 'QUARKUS_OPERATOR_SDK_CONTROLLERS_KEYCLOAKCONTROLLER_NAMESPACES',
        'QUARKUS_OPERATOR_SDK_CONTROLLERS_KEYCLOAKREALMIMPORTCONTROLLER_NAMESPACES', 'OPERATOR_NAME', 'POD_NAME'}
    for container in dep['spec']['template']['spec'].get('containers', []) + dep['spec']['template']['spec'].get('initContainers', []):
        require(not any(container.get(k) for k in ('command', 'args', 'lifecycle')), 'manual Deployment has custom executable configuration; archive it securely before migration')
        require(all('value' not in env or env.get('name') in allowed for env in container.get('env', [])),
                'manual Deployment has unknown literal environment values; archive it securely before migration')
    obj = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': lab.meta(PACKAGE), 'spec': copy.deepcopy(dep['spec'])}
    template = obj['spec']['template']
    template['metadata'] = {'labels': template.get('metadata', {}).get('labels', {}),
        'annotations': {'olm.targetNamespaces': lab.NS}}
    return obj


def stop_manual(expected, peers):
    dep = lab.get('deployment', PACKAGE)
    if not dep:
        return
    owned(dep)
    require(not dep['metadata'].get('ownerReferences') and not dep['metadata'].get('labels', {}).get('olm.owner'),
            'existing Operator Deployment already has a controller owner')
    require(dep['spec']['selector'] == expected['spec']['selector'], 'manual and OLM Deployment selectors differ; explicit migration review required')
    require(dep['spec']['template']['metadata'].get('annotations', {}).get('olm.targetNamespaces') == lab.NS,
            'manual Operator scope differs')
    path = lab.ROOT/'evidence/operator-olm/manual-deployment.json'
    if path.exists():
        previous = json.loads(path.read_text())
        require(previous.get('uid') == dep['metadata']['uid'], 'manual Deployment identity changed since migration snapshot')
    else:
        record('manual-deployment', {'at': utc(), 'uid': dep['metadata']['uid'], 'deployment': snapshot_manual(dep),
                                    'other_installations': peers})
    if dep['spec'].get('replicas', 1) != 0:
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': dep['metadata']['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': dep['metadata']['resourceVersion']},
                 {'op': 'add', 'path': '/spec/replicas', 'value': 0}]
        lab.oc('patch', 'deployment', PACKAGE, '-n', lab.NS, '--type=json', '-p', json.dumps(patch))
    deadline = time.monotonic()+90
    while time.monotonic() < deadline:
        current = lab.get('deployment', PACKAGE)
        require(current and current['metadata']['uid'] == dep['metadata']['uid'] and current['spec'].get('replicas') == 0,
                'manual Operator identity or desired replicas changed during handoff')
        pods = (lab.get('pods') or {}).get('items', [])
        alive = [p for p in pods if p.get('spec', {}).get('serviceAccountName') == 'rhbk-operator'
                 and p.get('status', {}).get('phase') not in {'Succeeded', 'Failed'}]
        if not alive and current.get('status', {}).get('replicas', 0) == 0:
            return
        time.sleep(2)
    raise RuntimeError('BLOCKED: manual Operator did not stop; InstallPlan remains unapproved')


def approve(plan_name, migrate_manual=False):
    require(re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', plan_name) is not None and len(plan_name) <= 63, 'invalid InstallPlan name')
    lab.guard()
    group, desired, lock = desired_resources()
    validate_existing(lab.get('operatorgroup', OG), group)
    sub = lab.get('subscription', PACKAGE); validate_existing(sub, desired)
    peers = verify_scope()
    plan = lab.get('installplan', plan_name)
    definitions, deployment = validate_plan(plan, sub, lock)
    crds = verify_crds(definitions)
    if plan['spec'].get('approved') is True:
        return {'status': 'ALREADY_APPROVED', 'plan': plan_name, 'csv': CSV}
    require(plan.get('status', {}).get('phase') == 'RequiresApproval', 'InstallPlan is not awaiting manual approval')
    quiescent()
    existing = lab.get('deployment', PACKAGE)
    if existing:
        require(migrate_manual, 'manual Deployment exists; use --migrate-manual for the reviewed handoff')
        stop_manual(deployment, peers)
    # Repeat guards after stopping the old reconciler and immediately before CAS.
    lab.guard(); quiescent()
    require(verify_scope() == peers, 'another RHBK installation changed during handoff')
    verify_crds(definitions)
    current = lab.get('installplan', plan_name)
    require(current['metadata']['uid'] == plan['metadata']['uid'] and current['metadata']['resourceVersion'] == plan['metadata']['resourceVersion'],
            'InstallPlan changed; review and rerun approval')
    current_definitions, current_deployment = validate_plan(current, sub, lock)
    require(current_definitions == definitions and current_deployment == deployment,
            'resolved bundle changed during handoff')
    record('approval', {'at': utc(), 'plan': plan_name, 'uid': plan['metadata']['uid'], 'csv': CSV,
                        'other_installations': peers, 'crds': crds, 'manual_handoff': bool(existing)})
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': plan['metadata']['uid']},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': plan['metadata']['resourceVersion']},
             {'op': 'test', 'path': '/spec/approved', 'value': False},
             {'op': 'replace', 'path': '/spec/approved', 'value': True}]
    lab.oc('patch', 'installplan', plan_name, '-n', lab.NS, '--type=json', '-p', json.dumps(patch))
    require(lab.get('installplan', plan_name)['spec'].get('approved') is True, 'approval readback failed; rerun to reconcile, do not restore a second Operator')
    return {'status': 'APPROVED', 'plan': plan_name, 'csv': CSV, 'next': 'wait for OLM, then run check'}


def check():
    lab.guard()
    group, desired, lock = desired_resources()
    validate_existing(lab.get('operatorgroup', OG), group)
    sub = lab.get('subscription', PACKAGE); validate_existing(sub, desired)
    require(sub.get('status', {}).get('installedCSV') == CSV and sub.get('status', {}).get('currentCSV') == CSV,
            'Subscription has not installed the pinned CSV')
    peers = verify_scope()
    csv = lab.get('clusterserviceversion', CSV)
    require(csv and csv.get('status', {}).get('phase') == 'Succeeded' and csv.get('spec', {}).get('version') == VERSION,
            'pinned CSV is not Succeeded')
    dep = lab.get('deployment', PACKAGE)
    require(dep and dep['spec'].get('replicas') == 1 and lab.deployment_ready(dep), 'OLM Operator Deployment is not ready')
    require(any(r.get('kind') == 'ClusterServiceVersion' and r.get('uid') == csv['metadata']['uid'] and r.get('name') == CSV
                for r in dep['metadata'].get('ownerReferences', [])), 'Operator Deployment is not owned by the pinned CSV')
    require(dep['spec']['template']['metadata'].get('annotations', {}).get('olm.targetNamespaces') == lab.NS,
            'running Operator targets another namespace')
    containers = dep['spec']['template']['spec']['containers']
    require(len(containers) == 1 and containers[0].get('image') == lock['operator']['image'], 'running Operator image differs from verified pin')
    approval = lab.ROOT/'evidence/operator-olm/approval.json'
    if approval.exists():
        require(json.loads(approval.read_text()).get('other_installations') == peers, 'another RHBK installation changed since approval')
    result = {'status': 'PASS', 'at': utc(), 'csv': CSV, 'namespace': lab.NS, 'approval': 'Manual',
              'operator_ready': True, 'other_installations': peers}
    record('check', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare'); commands.add_parser('check')
    approval = commands.add_parser('approve')
    approval.add_argument('--plan', required=True)
    approval.add_argument('--migrate-manual', action='store_true')
    args = parser.parse_args()
    result = approve(args.plan, args.migrate_manual) if args.command == 'approve' else globals()[args.command]()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'BLOCKED: invalid OLM response or local input; raw output withheld', file=__import__('sys').stderr)
        raise SystemExit(1)
