#!/usr/bin/env python3
"""Local CRC installation and operation. Never uses the ambient oc context."""
import base64, copy, datetime, json, os, pathlib, secrets, subprocess, sys, time
ROOT=pathlib.Path(__file__).resolve().parents[1]
NS='rhbk-rotation-lab'
LABELS={'app.kubernetes.io/part-of':'rhbk-secret-rotation-lab','lab.rhbk/owner':'client-secret-rotation'}
PRIVATE=ROOT/'.private'
HOST='rhbk-rotation.apps-crc.testing'
IMAGE='image-registry.openshift-image-registry.svc:5000/rhbk-rotation-lab/rhbk@sha256:cadcb5911714c8ffcd851c0035f04aa399d3677579bcad825b6b296fefe0b288'
PG_IMAGE='image-registry.openshift-image-registry.svc:5000/rhbk-rotation-lab/postgres@sha256:172d248599f2f87e0e78bffb6401bcbfc4c07f189ceec40ab76b86268b216797'
PY_IMAGE='image-registry.openshift-image-registry.svc:5000/rhbk-rotation-lab/python@sha256:1dbe53dbe4194bc25ea213246f7b2625804b2e102e4348d7e8d65507b92fd43b'
def kubeconfig_args():
 path=PRIVATE/'crc.kubeconfig'
 if not path.is_file() or path.is_symlink():raise RuntimeError('BLOCKED: configure an explicit dedicated CRC kubeconfig first')
 return ['--kubeconfig='+str(path)]
def oc(*args,data=None,check=True):
 p=subprocess.run(['oc',*kubeconfig_args(),'--request-timeout='+('0' if '--raw' in args and any('/proxy/' in str(a) for a in args) else '30s'),*args],input=json.dumps(data).encode() if data is not None else None,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=35)
 if check and p.returncode: raise RuntimeError('oc request failed; command='+str(args[0] if args else 'unknown')+' exit='+str(p.returncode)+'; raw API error withheld to protect credentials')
 return p.stdout.decode()
def get(kind,name=None,ns=NS):
 a=['get',kind]+([name] if name else [])+(['-n',ns] if ns else [])+['-o','json','--ignore-not-found']
 s=oc(*a); return json.loads(s) if s.strip() else None
def save(path,obj):
 p=ROOT/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,indent=2)+'\n')
def meta(name,ns=True): return dict(name=name,labels=LABELS,**({'namespace':NS} if ns else {}))
def apply(obj):
 # Do not persist last-applied annotation containing credentials.
 for resource in obj.get('items',[]) if obj.get('kind')=='List' else [obj]:
  m=resource['metadata'];existing=get(resource['kind'],m['name'],m.get('namespace'))
  if existing and any(existing['metadata'].get('labels',{}).get(k)!=v for k,v in LABELS.items()):raise RuntimeError('BLOCKED: refusing to adopt an unowned resource')
 raw=oc('apply','--server-side','--field-manager=rhbk-rotation-lab','-f','-',data=obj)
 print(raw.strip())
def guard(require_namespace=True):
 server=oc('config','view','--minify','-o','jsonpath={.clusters[0].cluster.server}').strip()
 if server!='https://api.crc.testing:6443': raise RuntimeError('BLOCKED: dedicated kubeconfig does not resolve to local CRC')
 node=get('node','crc',None)
 if node['metadata']['name']!='crc' or node['status']['nodeInfo']['architecture']!='arm64': raise RuntimeError('BLOCKED: node identity or architecture changed')
 cluster_uid=get('namespace','kube-system',None)['metadata']['uid']
 identity=ROOT/'evidence/cluster-identity.json'
 if identity.exists() and json.loads(identity.read_text())['uid']!=cluster_uid: raise RuntimeError('BLOCKED: CRC identity changed')
 ns=get('namespace',NS,None)
 if ns and any(ns['metadata'].get('labels',{}).get(k)!=v for k,v in LABELS.items()): raise RuntimeError('BLOCKED: namespace belongs to another project')
 namespace_identity=ROOT/'evidence/namespace-identity.json'
 recorded_namespace=json.loads(namespace_identity.read_text()) if namespace_identity.exists() else {}
 if ns and (recorded_namespace.get('uid')!=ns['metadata']['uid'] or recorded_namespace.get('name')!=NS or recorded_namespace.get('cluster_uid')!=cluster_uid):raise RuntimeError('BLOCKED: existing namespace was not created by this checkout; use a fresh CRC or restore this checkout identity')
 if require_namespace and not ns: raise RuntimeError('BLOCKED: laboratory namespace missing')
 save('evidence/cluster-identity.json',{'server':server,'uid':cluster_uid,'node':'crc','architecture':'arm64'})
 return node
def preflight():
 n=guard(False);cv=get('clusterversion','version',None)
 save('evidence/preflight.json',{'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'api':'https://api.crc.testing:6443','node_arch':n['status']['nodeInfo']['architecture'],'openshift':cv['status']['desired']['version'],'allocatable':n['status']['allocatable'],'node_ready':any(c['type']=='Ready' and c['status']=='True' for c in n['status']['conditions'])})
 print('PASS: dedicated kubeconfig, local CRC identity, namespace ownership and ARM64 verified')
def manifests():
 csv=json.loads((PRIVATE/'operator-csv.json').read_text());spec=csv['spec']['install']['spec']
 operator=[{'apiVersion':'v1','kind':'ServiceAccount','metadata':meta('rhbk-operator')}]
 for index,p in enumerate(spec['permissions']):
  name='rhbk-operator-'+str(index)
  operator.extend([{'apiVersion':'rbac.authorization.k8s.io/v1','kind':'Role','metadata':meta(name),'rules':p['rules']},{'apiVersion':'rbac.authorization.k8s.io/v1','kind':'RoleBinding','metadata':meta(name),'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':name},'subjects':[{'kind':'ServiceAccount','name':'rhbk-operator','namespace':NS}]}])
 # Only global access required by official bundle: read cluster ingress configuration.
 name='rhbk-rotation-lab-ingress-reader'
 operator.extend([{'apiVersion':'rbac.authorization.k8s.io/v1','kind':'ClusterRole','metadata':meta(name,False),'rules':[{'apiGroups':['config.openshift.io'],'resources':['ingresses'],'resourceNames':['cluster'],'verbs':['get']}]},{'apiVersion':'rbac.authorization.k8s.io/v1','kind':'ClusterRoleBinding','metadata':meta(name,False),'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':name},'subjects':[{'kind':'ServiceAccount','name':'rhbk-operator','namespace':NS}]}])
 ds=copy.deepcopy(spec['deployments'][0]['spec']);ds['template']['metadata']['labels'].update(LABELS);ds['template']['metadata']['annotations']={'olm.targetNamespaces':NS}
 lock=json.loads((ROOT/'versions.lock.yaml').read_text())
 for container in ds['template']['spec']['containers']:
  if container['image']==lock['operator']['image']:container['image']='image-registry.openshift-image-registry.svc:5000/'+NS+'/operator@'+lock['images']['operator']['digest_arm64']
  else:raise RuntimeError('BLOCKED: unexpected container in pinned official Operator bundle')
 operator.append({'apiVersion':'apps/v1','kind':'Deployment','metadata':meta('rhbk-operator'),'spec':ds})
 save('deploy/operator/resources.json',{'apiVersion':'v1','kind':'List','items':operator})
 pglabels={**LABELS,'app':'postgresql'}
 env=[{'name':'POSTGRESQL_USER','value':'keycloak'},{'name':'POSTGRESQL_DATABASE','value':'keycloak'},{'name':'POSTGRESQL_PASSWORD','valueFrom':{'secretKeyRef':{'name':'postgres-credential','key':'password'}}}]
 db=[{'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':meta('postgres-data'),'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'crc-csi-hostpath-provisioner','resources':{'requests':{'storage':'1Gi'}}}},{'apiVersion':'v1','kind':'Service','metadata':meta('postgresql'),'spec':{'selector':{'app':'postgresql'},'ports':[{'port':5432,'targetPort':5432}]}},{'apiVersion':'apps/v1','kind':'Deployment','metadata':meta('postgresql'),'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'postgresql'}},'template':{'metadata':{'labels':pglabels},'spec':{'automountServiceAccountToken':False,'containers':[{'name':'postgresql','image':PG_IMAGE,'env':env,'ports':[{'containerPort':5432}],'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'1','memory':'512Mi'}},'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']},'runAsNonRoot':True},'readinessProbe':{'exec':{'command':['/usr/libexec/check-container']}},'volumeMounts':[{'name':'data','mountPath':'/var/lib/pgsql/data'}]}],'volumes':[{'name':'data','persistentVolumeClaim':{'claimName':'postgres-data'}}]}}}}]
 save('deploy/database/resources.json',{'apiVersion':'v1','kind':'List','items':db})
 kc={'apiVersion':'k8s.keycloak.org/v2beta1','kind':'Keycloak','metadata':meta('rhbk'),'spec':{'instances':1,'image':IMAGE,'startOptimized':False,'features':{'enabled':['client-secret-rotation']},'db':{'vendor':'postgres','host':'postgresql','database':'keycloak','usernameSecret':{'name':'postgres-credential','key':'username'},'passwordSecret':{'name':'postgres-credential','key':'password'},'poolInitialSize':1,'poolMinSize':1,'poolMaxSize':5},'http':{'httpEnabled':True},'hostname':{'hostname':'https://'+HOST},'proxy':{'headers':'xforwarded'},'ingress':{'enabled':False},'bootstrapAdmin':{'user':{'secret':'rhbk-bootstrap'}},'resources':{'requests':{'cpu':'500m','memory':'768Mi'},'limits':{'cpu':'2','memory':'1536Mi'}},'additionalOptions':[{'name':'metrics-enabled','value':'true'}]}}
 route={'apiVersion':'route.openshift.io/v1','kind':'Route','metadata':meta('rhbk'),'spec':{'host':HOST,'to':{'kind':'Service','name':'rhbk-service'},'port':{'targetPort':'http'},'tls':{'termination':'edge','insecureEdgeTerminationPolicy':'Redirect'}}}
 save('deploy/rhbk/resources.json',{'apiVersion':'v1','kind':'List','items':[kc,route]})
def create_secret(name,values,immutable=False):
 current=get('secret',name)
 if current and any(current['metadata'].get('labels',{}).get(k)!=v for k,v in LABELS.items()):raise RuntimeError('BLOCKED: credential object belongs to another owner')
 if not current: apply({'apiVersion':'v1','kind':'Secret','metadata':meta(name),'immutable':immutable,'type':'Opaque','data':{k:base64.b64encode(v.encode()).decode() for k,v in values.items()}})
def wait_ready(kind,name,predicate,timeout=600):
 deadline=time.monotonic()+timeout
 while time.monotonic()<deadline:
  obj=get(kind,name)
  if obj and predicate(obj):return obj
  time.sleep(3)
 raise RuntimeError('BLOCKED: readiness timeout for '+kind+'/'+name)
def deployment_ready(obj):
 status=obj.get('status',{});replicas=obj['spec'].get('replicas',1)
 return status.get('observedGeneration',0)>=obj['metadata']['generation'] and status.get('availableReplicas',0)>=replicas and status.get('updatedReplicas',0)>=replicas
def deploy():
 guard()
 if not (PRIVATE/'setup.json').exists():raise RuntimeError('BLOCKED: make setup must complete before deploy')
 PRIVATE.mkdir(exist_ok=True,mode=0o700)
 create_secret('postgres-credential',{'username':'keycloak','password':secrets.token_urlsafe(32)})
 create_secret('rhbk-bootstrap',{'username':'lab-bootstrap','password':secrets.token_urlsafe(32)})
 for path in ['deploy/operator/resources.json','deploy/database/resources.json','deploy/rhbk/resources.json']:
  obj=json.loads((ROOT/path).read_text())
  print(oc('apply','--dry-run=server','-f','-',data=obj).strip())
  apply(obj)
  if path.startswith('deploy/operator'):wait_ready('deployment','rhbk-operator',deployment_ready)
  if path.startswith('deploy/database'):wait_ready('deployment','postgresql',deployment_ready)
 # Public router CA only. No private key is read or copied.
 cm=get('configmap','default-ingress-cert','openshift-config-managed')
 if not cm: raise RuntimeError('Router CA ConfigMap unavailable')
 ca=cm['data']['ca-bundle.crt']; (PRIVATE/'route-ca.crt').write_text(ca)
 apply({'apiVersion':'v1','kind':'ConfigMap','metadata':meta('rhbk-route-ca'),'data':{'ca.crt':ca}})
 wait_ready('keycloak','rhbk',lambda x:any(c.get('type')=='Ready' and c.get('status')=='True' and c.get('observedGeneration',x['metadata']['generation'])>=x['metadata']['generation'] for c in x.get('status',{}).get('conditions',[])))
 import ssl,urllib.request,urllib.error
 context=ssl.create_default_context(cafile=str(PRIVATE/'route-ca.crt'))
 deadline=time.monotonic()+120
 while time.monotonic()<deadline:
  try:
   with urllib.request.urlopen('https://'+HOST+'/realms/master/.well-known/openid-configuration',context=context,timeout=10) as response:
    if response.status==200:break
  except (OSError,urllib.error.URLError):pass
  time.sleep(3)
 else:raise RuntimeError('BLOCKED: RHBK Route did not become ready with validated TLS')
 print('PASS: Operator, PostgreSQL, RHBK and TLS Route ready')
def build():
 guard()
 for app in ['m2m-probe','rotation-controller']:
  apply({'apiVersion':'image.openshift.io/v1','kind':'ImageStream','metadata':meta(app)})
  apply({'apiVersion':'build.openshift.io/v1','kind':'BuildConfig','metadata':meta(app),'spec':{'runPolicy':'Serial','source':{'type':'Binary','binary':{}},'strategy':{'type':'Docker','dockerStrategy':{'dockerfilePath':'Containerfile','buildArgs':[{'name':'PYTHON_IMAGE','value':PY_IMAGE}]}},'output':{'to':{'kind':'ImageStreamTag','name':app+':lab'}},'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'1','memory':'768Mi'}}}})
  name=oc('start-build',app,'-n',NS,'--from-dir='+str(ROOT/'apps'/app),'-o','name').strip().split('/')[-1]
  deadline=time.monotonic()+600
  previous=None
  while time.monotonic()<deadline:
   status=get('build',name)['status']['phase']
   if status!=previous: print(name+': '+status,flush=True);previous=status
   if status=='Complete':break
   if status in {'Failed','Error','Cancelled'}:raise RuntimeError('Build failed: '+name)
   time.sleep(3)
  else:raise RuntimeError('Build timeout: '+name)
 pin_application_images()
def pin_application_images():
 guard()
 lockfile=ROOT/'versions.lock.yaml';lock=json.loads(lockfile.read_text())
 lock.setdefault('application_images',{})
 for app in ['m2m-probe','rotation-controller']:
  stream=get('imagestreamtag',app+':lab');digest=stream['image']['metadata']['name']
  path=ROOT/('deploy/workloads/'+app+'.json');manifest=json.loads(path.read_text())
  template=manifest['spec']['template'] if app=='m2m-probe' else manifest['spec']['jobTemplate']['spec']['template']
  template['spec']['containers'][0]['image']='image-registry.openshift-image-registry.svc:5000/'+NS+'/'+app+'@'+digest
  path.write_text(json.dumps(manifest,indent=2)+'\n')
  import hashlib
  source=ROOT/'apps'/app/('probe.py' if app=='m2m-probe' else 'controller.py')
  lock['application_images'][app]={'digest':digest,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
 lockfile.write_text(json.dumps(lock,indent=2)+'\n')
 print('Application image digests pinned to completed Builds')

def main():
 action=sys.argv[1]
 if action=='preflight':preflight()
 elif action=='render-infra':manifests()
 elif action=='deploy':deploy()
 elif action=='build':build()
 else:raise SystemExit('Unknown action')
if __name__=='__main__':
 try:main()
 except Exception as e:print(str(e),file=sys.stderr);sys.exit(1)
