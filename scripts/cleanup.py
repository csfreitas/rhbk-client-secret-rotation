#!/usr/bin/env python3
"""Delete only the owned lab namespace and its dedicated ingress-reader RBAC."""
import argparse
import datetime as dt
import json
import time
import lab

GLOBAL_NAME='rhbk-rotation-lab-ingress-reader'

def owned(obj):
 if not obj or any(obj.get('metadata',{}).get('labels',{}).get(k)!=v for k,v in lab.LABELS.items()):
  raise RuntimeError('cleanup ownership mismatch')
 if not obj['metadata'].get('uid'):raise RuntimeError('cleanup UID unavailable')
 return obj

def targets():
 # Cleanup must not establish a new identity based only on the current context.
 if not (lab.ROOT/'evidence/cluster-identity.json').exists():raise RuntimeError('recorded cluster identity is required')
 lab.guard(False)
 namespace=lab.get('namespace',lab.NS,None)
 binding=lab.get('clusterrolebinding',GLOBAL_NAME,None)
 role=lab.get('clusterrole',GLOBAL_NAME,None)
 result=[]
 if namespace:result.append(('/api/v1/namespaces/'+lab.NS,owned(namespace)))
 if binding:
  owned(binding)
  if binding.get('roleRef')!={'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':GLOBAL_NAME} or binding.get('subjects')!=[{'kind':'ServiceAccount','name':'rhbk-operator','namespace':lab.NS}]:
   raise RuntimeError('dedicated global binding has unexpected users or role')
  result.append(('/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/'+GLOBAL_NAME,binding))
 if role:result.append(('/apis/rbac.authorization.k8s.io/v1/clusterroles/'+GLOBAL_NAME,owned(role)))
 return result

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--dry-run',action='store_true');args=parser.parse_args()
 report={'timestamp_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'status':'BLOCKED','dry_run':args.dry_run,'targets':[]}
 try:
  planned=targets()
  report['targets']=[{'kind':obj['kind'],'name':obj['metadata']['name'],'uid':obj['metadata']['uid']} for _,obj in planned]
  if not args.dry_run:
   for path,obj in planned:
    lab.guard(False)
    # Raw DELETE supports DeleteOptions; UID precondition prevents a same-name replacement from being deleted.
    options={'apiVersion':'v1','kind':'DeleteOptions','propagationPolicy':'Background','preconditions':{'uid':obj['metadata']['uid']}}
    lab.oc('delete','--raw='+path,'-f','-',data=options)
   report['status']='DELETE_REQUESTED' if planned else 'ALREADY_ABSENT'
   deadline=time.monotonic()+45
   while planned and time.monotonic()<deadline:
    if not lab.get('namespace',lab.NS,None):report['status']='PASS';break
    time.sleep(3)
  else:report['status']='DRY_RUN'
  lab.save('evidence/cleanup.json',report)
  print(json.dumps(report,indent=2));return 0
 except (RuntimeError,KeyError,ValueError,TypeError,OSError):
  report['reason']='local_identity_ownership_or_delete_precondition_failed'
  lab.save('evidence/cleanup.json',report);print(json.dumps(report,indent=2));return 2
if __name__=='__main__':raise SystemExit(main())
