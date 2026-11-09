#!/usr/bin/env python3
"""Sanitized, read-only observation of the owned CRC laboratory."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import time
import lab

STATUS_KEYS={'pod','generation','role','attempts','successes','failures','last_http','last_success_at','last_result','latest_authentication_ok','initial_authentication','shutdown'}
LOG_KEYS={'timestamp','timestamp_utc','event','pod','generation','next_generation','role','http_status','http','ok','outcome','oauth_error','error','latency_ms','phase','reason','suspended'}
STATE_KEYS={'phase','generation','next_generation','old_generation','cycle_id','current_secret','old_secret','next_secret','suspended','last_rotated_at','post_started_at','post_finished_at','post_observed_at','hold_until','rollout_started_at','rollout_completed_at','old_probe_rejected','blocked_reason','updated_at','completed_at'}
META_KEYS={'client.secret.creation.time','client.secret.expiration.time','client.secret.rotated.creation.time','client.secret.rotated.expiration.time'}
OAUTH={'invalid_client','unauthorized_client','invalid_request','invalid_grant','invalid_scope','server_error','temporarily_unavailable','unsupported_grant_type','access_denied'}
NAME=re.compile(r'^[A-Za-z0-9_.:/@+-]{1,256}$')


def utc():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def owned(obj):return bool(obj) and all(obj.get('metadata',{}).get('labels',{}).get(k)==v for k,v in lab.LABELS.items())
def scalar(obj,keys):
 return {k:v for k,v in obj.items() if k in keys and (v is None or isinstance(v,(str,int,float,bool))) and (not isinstance(v,str) or len(v)<=256)}
def safe_state():
 obj=lab.get('configmap','rotation-state')
 if not obj:return {'available':False}
 if not owned(obj):raise RuntimeError('state ownership mismatch')
 data=json.loads(obj.get('data',{}).get('state.json','{}'));result=scalar(data,STATE_KEYS)
 for key in ('pre_metadata','post_metadata'):
  if isinstance(data.get(key),dict):result[key]={k:v for k,v in data[key].items() if k in META_KEYS and isinstance(v,(int,float))}
 return result

def pod_info(pod):
 meta,status=pod['metadata'],pod.get('status',{})
 return {'name':meta['name'],'uid':meta['uid'],'generation':meta.get('labels',{}).get('lab.rhbk/generation'),
 'component':meta.get('labels',{}).get('app.kubernetes.io/name',meta.get('labels',{}).get('app')),
 'phase':status.get('phase'),'deleting':bool(meta.get('deletionTimestamp')),
 'ready':any(c.get('type')=='Ready' and c.get('status')=='True' for c in status.get('conditions',[])),
 'restarts':sum(c.get('restartCount',0) for c in status.get('containerStatuses',[])),
 'images':[{'name':c.get('name'),'image':c.get('image'),'image_id':c.get('imageID')} for c in status.get('containerStatuses',[])]}

def probe_status(pod):
 info=pod_info(pod)
 if info['deleting'] or info['phase']!='Running':return {**info,'status_available':False}
 raw=lab.oc('get','--raw',f"/api/v1/namespaces/{lab.NS}/pods/{info['name']}:8080/proxy/status",check=False)
 try:
  value=json.loads(raw)
  if not isinstance(value,dict) or value.get('pod')!=info['name']:raise ValueError()
  result=scalar(value,STATUS_KEYS)
  last=value.get('last',{})
  result['oauth_error']=last.get('oauth_error') if last.get('oauth_error') in OAUTH else None
  result['last_attempt_utc']=last.get('timestamp_utc') if isinstance(last.get('timestamp_utc'),str) and len(last['timestamp_utc'])<40 else None
  return {**info,**result,'status_available':True}
 except (ValueError,TypeError):return {**info,'status_available':False}

def snapshot():
 pods=[p for p in (lab.get('pods') or {}).get('items',[]) if owned(p)]
 main=[probe_status(p) for p in pods if p['metadata'].get('labels',{}).get('app.kubernetes.io/name')=='m2m-probe']
 old=[probe_status(p) for p in pods if p['metadata'].get('labels',{}).get('app.kubernetes.io/name')=='old-probe']
 cron=lab.get('cronjob','rotation-controller');deployment=lab.get('deployment','m2m-probe')
 for resource in (cron,deployment):
  if resource and not owned(resource):raise RuntimeError('workload ownership mismatch')
 dep=None
 if deployment:
  ds=deployment['spec'];st=deployment.get('status',{})
  dep={'generation':ds['template']['metadata'].get('labels',{}).get('lab.rhbk/generation'),'desired':ds.get('replicas',1),
       'updated':st.get('updatedReplicas',0),'available':st.get('availableReplicas',0),'ready':st.get('readyReplicas',0),
       'observed_generation':st.get('observedGeneration'),'object_generation':deployment['metadata'].get('generation')}
 return {'timestamp_utc':utc(),'namespace':lab.NS,'state':safe_state(),'deployment':dep,
         'cronjob':{'exists':bool(cron),'suspended':cron['spec'].get('suspend',False) if cron else None,'schedule':cron['spec']['schedule'] if cron else None},
         'main_application':main,'old_secret_probes':old,'infrastructure':[pod_info(p) for p in pods if p['metadata'].get('labels',{}).get('app.kubernetes.io/name') not in {'m2m-probe','old-probe'}]}

def baseline():
 samples=[]
 for index in range(3):
  if index:time.sleep(2)
  samples.append(snapshot())
 before,after=samples[0],samples[-1];dep=after['deployment'];pods=after['main_application']
 old={p['name']:p for p in before['main_application']}
 passed=bool(dep) and dep['desired']==2 and len(pods)==2 and all(p.get('status_available') and p.get('ready') and p.get('generation')==dep['generation'] and p.get('last_http')==200 and p.get('last_success_at') and time.time()-p['last_success_at']<12 and p['name'] in old and p.get('attempts',0)>old[p['name']].get('attempts',0) and p.get('successes',0)>old[p['name']].get('successes',0) for p in pods)
 report={'status':'PASS' if passed else 'BLOCKED','reason':'two_pods_completed_fresh_token_requests' if passed else 'two_stable_healthy_pods_not_observed','samples':samples}
 lab.save('evidence/baseline.json',report);print(json.dumps(report,indent=2));return 0 if passed else 2

def logs(pod):
 component=pod['metadata'].get('labels',{}).get('app.kubernetes.io/name')
 container='probe' if component in {'m2m-probe','old-probe'} else 'controller'
 raw=lab.oc('logs',pod['metadata']['name'],'-n',lab.NS,'-c',container,'--tail=2000',check=False)
 result=[]
 for line in raw.splitlines():
  try:
   row=json.loads(line)
   if not isinstance(row,dict):continue
   filtered=scalar(row,LOG_KEYS)
   if filtered.get('event'):result.append(filtered)
  except ValueError:pass
 return result

def collect():
 data=snapshot();data['probe_and_controller_logs']={}
 data['active_jobs']=[job['metadata']['name'] for job in (lab.get('jobs') or {}).get('items',[]) if owned(job) and not any(condition.get('type') in {'Complete','Failed'} and condition.get('status')=='True' for condition in job.get('status',{}).get('conditions',[]))]
 for pod in (lab.get('pods') or {}).get('items',[]):
  if owned(pod) and pod['metadata'].get('labels',{}).get('app.kubernetes.io/name') in {'m2m-probe','old-probe','rotation-controller'}:
   data['probe_and_controller_logs'][pod['metadata']['name']]=logs(pod)
 selector=','.join(k+'='+v for k,v in lab.LABELS.items())
 # The oc process projects only metadata; no Secret data reaches Python/evidence.
 raw=lab.oc('get','secrets','-n',lab.NS,'-l',selector,'-o','jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.immutable}{"\\n"}{end}')
 data['credential_object_metadata']=[]
 for line in raw.splitlines():
  parts=line.split('\t')
  if len(parts)==2 and NAME.fullmatch(parts[0]):data['credential_object_metadata'].append({'name':parts[0],'immutable':parts[1]=='true'})
 path='evidence/collections/'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json'
 lab.save(path,data);print(json.dumps({'status':'PASS','evidence':path,'main_pods':len(data['main_application']),'old_probes':len(data['old_secret_probes'])}));return 0

def watch():
 try:
  while True:
   data=snapshot()
   print(json.dumps({k:data[k] for k in ('timestamp_utc','state','deployment','cronjob','main_application','old_secret_probes')},sort_keys=True),flush=True)
   time.sleep(2)
 except KeyboardInterrupt:return 0

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['baseline','watch','collect']);args=parser.parse_args()
 try:
  lab.guard()
  return {'baseline':baseline,'watch':watch,'collect':collect}[args.action]()
 except (RuntimeError,ValueError,KeyError,TypeError,OSError):
  print(json.dumps({'status':'BLOCKED','reason':'local_cluster_guard_or_observation_failed'}));return 2
if __name__=='__main__':raise SystemExit(main())
