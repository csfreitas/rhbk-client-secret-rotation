#!/usr/bin/env python3
"""Sanitized, read-only observation of the owned CRC laboratory."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo
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
 obj=lab.get('configmap',getattr(lab,'STATE_NAME','rotation-state'))
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
 cron=lab.get('cronjob',getattr(lab,'CRONJOB_NAME','rotation-controller'));deployment=lab.get('deployment',getattr(lab,'DEPLOYMENT_NAME','m2m-probe'))
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

def baseline(format='table',timezone='America/Sao_Paulo'):
 samples=[]
 for index in range(3):
  if index:time.sleep(2)
  samples.append(snapshot())
 before,after=samples[0],samples[-1];dep=after['deployment'];pods=after['main_application']
 old={p['name']:p for p in before['main_application']}
 passed=bool(dep) and dep['desired']==2 and len(pods)==2 and all(p.get('status_available') and p.get('ready') and p.get('generation')==dep['generation'] and p.get('last_http')==200 and p.get('last_success_at') and time.time()-p['last_success_at']<12 and p['name'] in old and p.get('attempts',0)>old[p['name']].get('attempts',0) and p.get('successes',0)>old[p['name']].get('successes',0) for p in pods)
 report={'status':'PASS' if passed else 'BLOCKED','reason':'two_pods_completed_fresh_token_requests' if passed else 'two_stable_healthy_pods_not_observed','samples':samples}
 lab.save('evidence/baseline.json',report)
 if format=='json':print(json.dumps(report,indent=2))
 else:print(report['status']+': '+report['reason']+'\n'+table(after,timezone))
 return 0 if passed else 2

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

def generation(value):
 return f'v{value:04d}' if isinstance(value,int) else (str(value) if value is not None else '-')

def authentication_status(pod,now):
 if not pod.get('status_available'):return 'SEM STATUS'
 try:
  attempted=dt.datetime.fromisoformat(pod['last_attempt_utc'].replace('Z','+00:00')).timestamp()
 except (KeyError,TypeError,ValueError,AttributeError):return 'SEM AMOSTRA'
 if not -5 <= now-attempted <= 12:return 'DESATUALIZADA'
 return 'OK' if pod.get('last_http')==200 and pod.get('latest_authentication_ok') is True else 'FALHOU'

def table(data,timezone='America/Sao_Paulo',now=None):
 """Present main pods only; old-secret rejections never count as app failures."""
 now=time.time() if now is None else now
 state=data.get('state',{});cron=data.get('cronjob',{});deployment=data.get('deployment') or {}
 suspension=[state.get('suspended'),cron.get('suspended')]
 automation='SUSPENSA' if True in suspension else ('ATIVA' if suspension==[False,False] else 'INDEFINIDA')
 current=generation(state.get('generation'));next_value=state.get('next_generation')
 if next_value is not None and next_value!=state.get('generation'):current+=' -> '+generation(next_value)
 rows=[dt.datetime.fromtimestamp(now,ZoneInfo(timezone)).strftime('%d/%m/%Y %H:%M:%S')+' | '+timezone,
       f"FASE: {state.get('phase','-')} | GERACAO: {current} | AUTOMACAO: {automation}",
       f"Pods prontos: {deployment.get('ready','-')}/{deployment.get('desired','-')}",
       f"{'POD':36} {'GERACAO':8} {'HTTP':5} {'SUCESSOS':9} {'FALHAS':7} AUTH"]
 for pod in data.get('main_application',[]):
  values=[pod.get(k) for k in ('name','generation','last_http','successes','failures')]
  name,gen,http,success,failures=[str(v) if v is not None else '-' for v in values]
  rows.append(f'{name:36} {gen:8} {http:5} {success:9} {failures:7} {authentication_status(pod,now)}')
 if not data.get('main_application'):rows.append('Nenhum pod da aplicacao observado.')
 old_generation=generation(state.get('old_generation'))
 old=[pod for pod in data.get('old_secret_probes',[]) if pod.get('generation')==old_generation]
 for pod in old:
  http=pod.get('last_http') if pod.get('status_available') else None
  rows.append(f"Sonda anterior {old_generation}: HTTP {http if http is not None else '-'} (separada da aplicacao)")
 if state.get('blocked_reason'):rows.append('BLOQUEIO: '+str(state['blocked_reason']))
 return '\n'.join(rows)

def history(timezone='America/Sao_Paulo'):
 obj=lab.get('configmap',getattr(lab,'STATE_NAME','rotation-state'))
 if not owned(obj):raise RuntimeError('state ownership mismatch')
 state=json.loads(obj.get('data',{}).get('state.json','{}'))
 rows=state.get('history',[])[-5:]
 print('Horarios: '+timezone+'; intervalo entre o fim do POST anterior e o inicio do seguinte.')
 print(f"{'GERACAO':8} {'POST':8} {'INTERVALO':10} {'TTL(dias)':10} {'CONV(s)':8} {'ANTIGA':10} TIPO")
 previous=None
 for row in rows:
  meta=row.get('post_metadata',{});created=meta.get('client.secret.creation.time')
  expires=meta.get('client.secret.expiration.time');rotated=meta.get('client.secret.rotated.expiration.time')
  start=row.get('post_started_at')
  when=dt.datetime.fromtimestamp(start,ZoneInfo(timezone)).strftime('%H:%M:%S') if start is not None else '-'
  elapsed=f'{start-previous:.1f}s' if start is not None and previous is not None else '-'
  ttl=f'{(expires-created)/86400:g}' if expires is not None and created is not None else '-'
  overlap=f'{rotated-created:g}' if rotated is not None and created is not None else '-'
  result='REJEITADA' if row.get('old_rejected') is True else '-'
  kind='RECUPERACAO' if row.get('recovery_mode') else 'NORMAL'
  print(f"{generation(row.get('generation')):8} {when:8} {elapsed:10} {ttl:10} {overlap:8} {result:10} {kind}")
  previous=row.get('post_finished_at')
 if not rows:print('Nenhuma rotacao concluida registrada.')
 return 0

def watch(format='table',output=None,interval=2,timezone='America/Sao_Paulo'):
 stream=None
 if output:
  if output=='auto':
   mode='openshift' if hasattr(lab,'CONFIG') else 'crc'
   output=lab.ROOT/'evidence'/mode/('watch-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.jsonl')
  path=Path(output);path.parent.mkdir(parents=True,exist_ok=True)
  stream=path.open('a')
  print('Observacoes completas: '+str(path),file=sys.stderr)
 try:
  while True:
   data=snapshot()
   row={k:data[k] for k in ('timestamp_utc','state','deployment','cronjob','main_application','old_secret_probes')}
   serialized=json.dumps(row,sort_keys=True)
   if stream:stream.write(serialized+'\n');stream.flush()
   if format=='json':print(serialized,flush=True)
   else:
    if sys.stdout.isatty():print('\033[2J\033[H',end='')
    print(table(row,timezone),flush=True)
   time.sleep(interval)
 except KeyboardInterrupt:return 0
 finally:
  if stream:stream.close()

def main():
 global lab
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['baseline','watch','collect','status','history'])
 parser.add_argument('--environment',choices=['crc','openshift'],default='crc')
 parser.add_argument('--format',choices=['table','json'],default='table')
 parser.add_argument('--output',help='Append complete JSONL observations; auto creates a dated file')
 parser.add_argument('--interval',type=float,default=2)
 parser.add_argument('--timezone',default='America/Sao_Paulo')
 args=parser.parse_args()
 if not 1 <= args.interval <= 60:parser.error('--interval must be between 1 and 60 seconds')
 original_backend=lab
 try:
  ZoneInfo(args.timezone)
  if args.environment=='openshift':
   from openshift import Backend
   lab=Backend()
  lab.guard()
  if args.action=='watch':return watch(args.format,args.output,args.interval,args.timezone)
  if args.action=='history':return history(args.timezone)
  if args.action=='baseline':return baseline(args.format,args.timezone)
  if args.action=='status':
   data=snapshot()
   print(json.dumps(data,indent=2) if args.format=='json' else table(data,args.timezone))
   return 0
  return collect()
 except (RuntimeError,ValueError,KeyError,TypeError,OSError):
  print(json.dumps({'status':'BLOCKED','reason':'local_cluster_guard_or_observation_failed'}));return 2
 finally:lab=original_backend
if __name__=='__main__':raise SystemExit(main())
