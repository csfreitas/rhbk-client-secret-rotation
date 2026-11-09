"""T15-only bootstrap setup fallback after a measured runtime mapper POST 403.

The bootstrap credential is used only to read ownership and create/read back one
fixed self-audience mapper on one disposable experiment client. No roles,
policies, clients, client secrets, or runtime administrator privileges change.
"""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import re
import ssl
import urllib.parse

ROOT=Path(__file__).resolve().parents[2]
NS='rhbk-rotation-lab'
REALM='lab-secret-rotation'
OWNER='client-secret-rotation'
URL='https://rhbk-rotation.apps-crc.testing'
LABELS={'app.kubernetes.io/part-of':'rhbk-secret-rotation-lab','lab.rhbk/owner':OWNER}
CLIENT_PATTERN=re.compile(r'expiry-token-vs-secret-[a-f0-9]{10}')
UUID_PATTERN=re.compile(r'[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}')


class SetupBlocked(Exception):
    """Only fixed nonsensitive diagnostics may be used here."""


def owned(resource):
    if not isinstance(resource,dict) or any(resource.get('metadata',{}).get('labels',{}).get(key)!=value for key,value in LABELS.items()):
        raise SetupBlocked('T15_bootstrap_resource_ownership_mismatch')
    return resource


def model(client_id):
    return {'name':'lab-self-audience','protocol':'openid-connect','protocolMapper':'oidc-audience-mapper','consentRequired':False,
            'config':{'included.client.audience':client_id,'access.token.claim':'true','id.token.claim':'false'}}


def exact_mapper(existing,expected):
    if not isinstance(existing,dict) or not all(existing.get(key)==expected[key] for key in ('name','protocol','protocolMapper')) or existing.get('consentRequired') not in (None,False):
        return False
    config=existing.get('config',{})
    if not isinstance(config,dict) or any(config.get(key)!=value for key,value in expected['config'].items()):
        return False
    extra=set(config)-set(expected['config'])
    return extra.issubset({'introspection.token.claim','userinfo.token.claim'}) and all(config[key] in {'true','false'} for key in extra)


def configure(api,client_uuid,client_id,oc_raw,*,trigger_status):
    """Return a sanitized receipt; do not modify api.token or runtime credentials."""
    if trigger_status!=403:
        raise SetupBlocked('T15_bootstrap_fallback_requires_runtime_POST_403')
    if api.url.rstrip('/')!=URL or api.realm!=REALM or getattr(api,'admin_id',None)!='rotation-admin' or not CLIENT_PATTERN.fullmatch(client_id) or not UUID_PATTERN.fullmatch(client_uuid):
        raise SetupBlocked('T15_bootstrap_target_not_disposable_owned_experiment')
    context=getattr(api.http,'context',None)
    if context is None or context.verify_mode!=ssl.CERT_REQUIRED or context.check_hostname is not True:
        raise SetupBlocked('T15_bootstrap_requires_validated_TLS')
    kubeconfig=os.environ.get('KUBECONFIG','')
    if not kubeconfig or os.pathsep in kubeconfig or Path(kubeconfig).resolve()!=(ROOT/'.private/crc.kubeconfig').resolve():
        raise SetupBlocked('T15_bootstrap_requires_dedicated_local_kubeconfig')

    def oc_json(args):
        try:return json.loads(oc_raw(args))
        except (ValueError,TypeError):raise SetupBlocked('T15_bootstrap_invalid_guard_response') from None

    config=oc_json(['config','view','--minify','-o','json'])
    servers=[entry.get('cluster',{}).get('server') for entry in config.get('clusters',[])]
    if servers!=['https://api.crc.testing:6443']:
        raise SetupBlocked('T15_bootstrap_refuses_nonlocal_cluster')
    try:identity=json.loads((ROOT/'evidence/cluster-identity.json').read_text())
    except (OSError,ValueError):raise SetupBlocked('T15_bootstrap_recorded_cluster_identity_missing') from None
    current=oc_json(['get','namespace','kube-system','-o','json'])
    if not identity.get('uid') or current.get('metadata',{}).get('uid')!=identity['uid']:
        raise SetupBlocked('T15_bootstrap_cluster_identity_changed')
    namespace=owned(oc_json(['get','namespace',NS,'-o','json']))
    try:namespace_identity=json.loads((ROOT/'evidence/namespace-identity.json').read_text())
    except (OSError,ValueError):raise SetupBlocked('T15_bootstrap_recorded_namespace_identity_missing') from None
    if (not isinstance(namespace_identity,dict) or namespace_identity.get('name')!=NS
            or namespace_identity.get('cluster_uid')!=identity['uid']
            or namespace_identity.get('uid')!=namespace.get('metadata',{}).get('uid')
            or not namespace_identity.get('uid')):
        raise SetupBlocked('T15_bootstrap_namespace_not_created_by_this_checkout')
    route=owned(oc_json(['-n',NS,'get','route','rhbk','-o','json']))
    if route.get('spec',{}).get('host')!=urllib.parse.urlsplit(URL).hostname:
        raise SetupBlocked('T15_bootstrap_route_identity_mismatch')
    credential=owned(oc_json(['-n',NS,'get','secret','rhbk-bootstrap','-o','json']))
    try:
        username=base64.b64decode(credential['data']['username'],validate=True).decode()
        password=base64.b64decode(credential['data']['password'],validate=True).decode()
        if not username or not password:raise ValueError()
    except (KeyError,ValueError,UnicodeError):raise SetupBlocked('T15_bootstrap_credential_unavailable') from None
    code,body,_=api.http.request('POST',URL+'/realms/master/protocol/openid-connect/token',
        {'grant_type':'password','client_id':'admin-cli','username':username,'password':password},form=True)
    del username,password,credential
    if code!=200 or not isinstance(body,dict) or not isinstance(body.get('access_token'),str) or not body['access_token']:
        raise SetupBlocked('T15_bootstrap_authentication_failed')
    bootstrap_token=body['access_token'];del body
    base='/admin/realms/'+REALM

    def read(path):
        code,value,_=api.http.request('GET',URL+base+path,token=bootstrap_token)
        if code!=200:raise SetupBlocked('T15_bootstrap_readback_failed')
        return value

    realm=read('')
    if not isinstance(realm,dict) or realm.get('realm')!=REALM or realm.get('attributes',{}).get('lab.rhbk/owner')!=OWNER:
        raise SetupBlocked('T15_bootstrap_realm_not_owned')
    client_path='/clients/'+client_uuid
    client=read(client_path)
    if not isinstance(client,dict) or client.get('id')!=client_uuid or client.get('clientId')!=client_id or client.get('protocol')!='openid-connect' or client.get('publicClient') is not False or client.get('attributes',{}).get('lab.owner')!=OWNER or client.get('attributes',{}).get('lab.experiment')!='token-vs-secret':
        raise SetupBlocked('T15_bootstrap_client_not_owned_or_wrong_scenario')
    del realm,client
    path=client_path+'/protocol-mappers/models'
    expected=model(client_id)
    existing=read(path)
    if not isinstance(existing,list):raise SetupBlocked('T15_bootstrap_mapper_list_invalid')
    matching=[item for item in existing if isinstance(item,dict) and item.get('name')==expected['name']]
    if matching:
        if len(matching)!=1 or not exact_mapper(matching[0],expected):raise SetupBlocked('T15_bootstrap_existing_mapper_conflicts')
        action='existing_mapper_verified'
    else:
        code,_,_=api.http.request('POST',URL+base+path,expected,token=bootstrap_token)
        # A timeout/lost response is reconciled only with GET; no second POST.
        if code not in {0,200,201,204,409}:raise SetupBlocked('T15_bootstrap_mapper_create_failed')
        action='mapper_created' if code in {200,201,204} else 'mapper_outcome_reconciled_by_GET'
    configured=read(path)
    matches=[item for item in configured if isinstance(item,dict) and item.get('name')==expected['name']] if isinstance(configured,list) else []
    if len(matches)!=1 or not exact_mapper(matches[0],expected):raise SetupBlocked('T15_bootstrap_mapper_readback_not_exact_no_retry')
    del bootstrap_token
    return {'authority':'bootstrap_setup_only','action':action,'client_id':client_id,'mapper_name':expected['name'],
            'mapper_readback_verified':True,'runtime_permissions_changed':False,'client_secret_mutation_sent':False}
