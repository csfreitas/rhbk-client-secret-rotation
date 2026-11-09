#!/usr/bin/env python3
"""Create bounded manual Jobs from the same suspended CronJob implementation."""
import argparse,copy,json,sys,time
from lab import guard,get,oc,apply,meta,NS,save

def run(command,extra_env=None,wait=True):
 guard();cron=get('cronjob','rotation-controller')
 if not cron:raise RuntimeError('BLOCKED controller CronJob not installed')
 name='rotation-'+command+'-'+str(time.time_ns())[-12:]
 spec=copy.deepcopy(cron['spec']['jobTemplate']['spec']);c=spec['template']['spec']['containers'][0];c['args']=[command]
 for k,v in (extra_env or {}).items():c['env'].append({'name':k,'value':str(v)})
 obj={'apiVersion':'batch/v1','kind':'Job','metadata':meta(name),'spec':spec};obj['metadata']['labels']['app.kubernetes.io/name']='rotation-controller';apply(obj)
 if not wait:return name
 deadline=time.monotonic()+260
 while time.monotonic()<deadline:
  job=get('job',name);status=job.get('status',{})
  if status.get('succeeded') or status.get('failed'):
   log=oc('logs','job/'+name,'-n',NS,check=False)
   print(log)
   ( __import__('pathlib').Path(__file__).resolve().parents[1]/'evidence'/(name+'.jsonl')).write_text(log)
   if not status.get('succeeded'):raise RuntimeError('Job failed: '+name)
   return name
  time.sleep(2)
 raise RuntimeError('Job outcome unknown; inspect/reconcile, do not repeat POST')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('command',choices=['evaluate','rotate-once','reconcile','status','suspend','resume']);p.add_argument('--no-wait',action='store_true');a=p.parse_args()
 try:run(a.command,wait=not a.no_wait)
 except Exception as e:print(str(e),file=sys.stderr);sys.exit(1)
