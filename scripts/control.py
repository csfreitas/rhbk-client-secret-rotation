#!/usr/bin/env python3
"""Create bounded manual Jobs from the same suspended CronJob implementation."""
import argparse,copy,json,sys,time
import lab

def run(command,extra_env=None,wait=True,backend=None):
 backend=backend or lab
 backend.guard();cron=backend.get('cronjob',getattr(backend,'CRONJOB_NAME','rotation-controller'))
 if not cron:raise RuntimeError('BLOCKED controller CronJob not installed')
 labels=backend.labels() if hasattr(backend,'labels') else backend.LABELS
 if any(cron.get('metadata',{}).get('labels',{}).get(k)!=v for k,v in labels.items()):
  raise RuntimeError('BLOCKED controller CronJob belongs to another installation')
 name='rotation-'+command+'-'+str(time.time_ns())[-12:]
 spec=copy.deepcopy(cron['spec']['jobTemplate']['spec'])
 containers=[c for c in spec['template']['spec']['containers'] if c['name']=='controller']
 if len(containers)!=1:raise RuntimeError('BLOCKED expected exactly one controller container')
 c=containers[0];c['args']=[command]
 for k,v in (extra_env or {}).items():c['env'].append({'name':k,'value':str(v)})
 obj={'apiVersion':'batch/v1','kind':'Job','metadata':backend.meta(name),'spec':spec};obj['metadata']['labels']['app.kubernetes.io/name']='rotation-controller';backend.apply(obj)
 if not wait:return name
 deadline=time.monotonic()+max(260,int(spec.get('activeDeadlineSeconds',240))+30)
 while time.monotonic()<deadline:
  job=backend.get('job',name);status=job.get('status',{})
  if status.get('succeeded') or status.get('failed'):
   log=backend.oc('logs','job/'+name,'-n',backend.NS,'-c','controller',check=False)
   print(log)
   directory=backend.ROOT/'evidence'/('openshift' if hasattr(backend,'CONFIG') else 'crc')
   directory.mkdir(parents=True,exist_ok=True);(directory/(name+'.jsonl')).write_text(log)
   if not status.get('succeeded'):raise RuntimeError('Job failed: '+name)
   return name
  time.sleep(2)
 raise RuntimeError('Job outcome unknown; inspect/reconcile, do not repeat POST')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('command',choices=['evaluate','rotate-once','reconcile','status','suspend','resume']);p.add_argument('--no-wait',action='store_true');p.add_argument('--environment',choices=['crc','openshift'],default='crc');a=p.parse_args()
 try:
  backend=None
  if a.environment=='openshift':
   from openshift import Backend
   backend=Backend()
  run(a.command,wait=not a.no_wait,backend=backend)
 except Exception as e:print(str(e),file=sys.stderr);sys.exit(1)
