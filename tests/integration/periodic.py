#!/usr/bin/env python3
"""Observe >=3 real CronJob rotations without manual rotation between cycles.

Only resume/suspend control Jobs are created. Main-probe counters are sampled
approximately every two seconds. Old-secret probes are deliberately excluded.
All output is sanitized; credentials and raw resources/logs are never written.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
from pathlib import Path
import signal
import sys
import time
import operations

ROOT=Path(__file__).resolve().parents[2]
HISTORY_SCALARS={'cycle_id','generation','post_started_at','post_finished_at','post_observed_at','rollout_completed_at','completed_at','old_rejected','recovery_mode','preexisting_outage','old_rejection_basis','old_rejection_oauth','old_retired_early'}
META={'client.secret.creation.time','client.secret.expiration.time','client.secret.rotated.creation.time','client.secret.rotated.expiration.time'}
AUTH_FIELDS={'http','ok','error','timestamp','latency_ms'}


def clean_history(row):
 result={k:v for k,v in row.items() if k in HISTORY_SCALARS and isinstance(v,(int,float,str,bool,type(None)))}
 for name in ('pre_metadata','post_metadata'):
  if isinstance(row.get(name),dict):result[name]={k:v for k,v in row[name].items() if k in META and isinstance(v,(int,float))}
 for name in ('old_result','old_probe_result','new_result','baseline_auth'):
  if isinstance(row.get(name),dict):result[name]={k:v for k,v in row[name].items() if k in AUTH_FIELDS and isinstance(v,(int,float,str,bool,type(None)))}
 return result


def full_state(crc):
 obj=crc.owned(crc.get('configmap','rotation-state'))
 raw=json.loads(obj['data']['state.json'])
 state={k:raw[k] for k in operations.STATE_FIELDS if k in raw}
 history=[clean_history(row) for row in raw.get('history',[]) if isinstance(row,dict)]
 return state,history


def terminal(job):
 return next((c['type'] for c in job.get('status',{}).get('conditions',[]) if c.get('status')=='True' and c.get('type') in {'Complete','Failed'}),'Running')


def controller_job(job):
 return (all(job.get('metadata',{}).get('labels',{}).get(k)==v for k,v in operations.LABELS.items())
         and any(c.get('name')=='controller' for c in job.get('spec',{}).get('template',{}).get('spec',{}).get('containers',[])))


def lab_jobs(crc):
 return [job for job in crc.get('jobs')['items'] if controller_job(job)]


class ScheduledProof:
 """Archive terminal Job evidence before CronJob history garbage collection."""
 def __init__(self,owner_uid,started,resume_job):
  self.owner_uid=owner_uid;self.started=int(started);self.resume_job=resume_job
  self.jobs={};self.proofs={}

 def observe(self,crc,jobs,recorder):
  for job in jobs:
   if not controller_job(job):continue
   name=job['metadata']['name']
   created=operations.event_epoch(job['metadata'].get('creationTimestamp'))
   if created is None:raise operations.Blocked('controller_job_creation_time_missing')
   if created<self.started:continue
   command=next((c.get('args',['unknown'])[0] for c in job['spec']['template']['spec']['containers'] if c.get('name')=='controller'),'unknown')
   scheduled=any(ref.get('kind')=='CronJob' and ref.get('uid')==self.owner_uid for ref in job['metadata'].get('ownerReferences',[]))
   if name==self.resume_job:continue
   if not scheduled:
    if command!='status':raise operations.Failed('manual_controller_intervention_during_periodic_measurement')
    continue
   if command!='evaluate':raise operations.Failed('scheduled_job_command_is_not_normal_evaluate')
   status=terminal(job)
   self.jobs[name]={'name':name,'created_utc':job['metadata']['creationTimestamp'],'status':status,
                    'command':command,'scheduled_owner_uid':self.owner_uid,'terminal_logs_archived':name in self.proofs}
   if status in {'Complete','Failed'} and name not in self.proofs:
    rows=crc.logs(name)
    if rows:
     self.proofs[name]=rows
     self.jobs[name]['terminal_logs_archived']=True
     recorder.record('scheduled_job_terminal_evidence',job=name,status=status,scheduled_owner_uid=self.owner_uid,
                     command=command,controller_events=rows)
   if status=='Failed':raise operations.Failed('scheduled_controller_job_failed')

 def correlate(self,completed):
  result=[]
  for cycle in completed.values():
   if cycle.get('recovery_mode') or cycle.get('preexisting_outage') or cycle.get('old_retired_early'):
    raise operations.Failed('explicit_recovery_cycle_cannot_count_as_periodic_rotation')
   if cycle.get('old_rejection_basis') not in {None,'natural-expiration'}:
    raise operations.Failed('periodic_cycle_did_not_use_natural_old_expiration')
   generation=cycle['generation'];matches=[]
   for name,job in self.jobs.items():
    if job['status']!='Complete' or job['scheduled_owner_uid']!=self.owner_uid:continue
    rows=self.proofs.get(name,[])
    responses=[r for r in rows if r.get('event')=='rotation_response_received']
    completes=[r for r in rows if r.get('event')=='state_transition' and r.get('phase')=='COMPLETE']
    response_ok=len(responses)==1 and responses[0].get('http')==200 and responses[0].get('generation')==f'v{generation:04d}'
    complete_ok=len(completes)==1 and completes[0].get('generation')==generation
    baseline=any(r.get('event')=='authentication' and r.get('role')=='baseline' and r.get('generation')==f'v{generation-1:04d}' and r.get('ok') is True and r.get('http')==200 for r in rows)
    old_rejected=any(r.get('event')=='authentication' and r.get('role')=='old_after_expiry' and r.get('generation')==f'v{generation-1:04d}' and r.get('ok') is False and r.get('http') in {400,401} and r.get('error') in {'invalid_client','unauthorized_client'} for r in rows)
    new_positive=any(r.get('event')=='authentication' and r.get('role')=='main_after_old_expiry' and r.get('generation')==f'v{generation:04d}' and r.get('ok') is True and r.get('http')==200 for r in rows)
    if response_ok and complete_ok and baseline and old_rejected and new_positive:matches.append(name)
   if len(matches)>1:raise operations.Failed('generation_has_multiple_scheduled_job_proofs')
   if matches:
    result.append({'generation':generation,'job':matches[0],'cycle_id':cycle['cycle_id'],
                   'post_response_logged':True,'complete_logged':True,'scheduled_owner_uid':self.owner_uid,
                   'baseline_success_logged':True,'old_expiry_rejection_logged':True,'new_positive_control_logged':True})
  return sorted(result,key=lambda item:item['generation'])


class Counters:
 """Lower bounds based on maxima, with initial counters removed for existing pods."""
 def __init__(self,initial):
  self.pods={}
  self.error_events=[]
  self.counter_resets=[]
  self.samples=0
  self.missing_samples=0
  self.seeded=set()
  for pod in initial:
   name=pod['pod'];self.seeded.add(name)
   self.pods[name]=self.new_record(pod,baseline=True)

 @staticmethod
 def new_record(pod,baseline=False):
  values={key:int(pod.get(key,0)) for key in ('attempts','successes','failures')}
  return {'pod':pod['pod'],'generation':pod.get('generation'),'first_observed_utc':operations.utc(),
          'last_observed_utc':operations.utc(),'baseline':values.copy() if baseline else {key:0 for key in values},
          'maxima':values.copy(),'last':values.copy(),'preexisting':baseline,'observations':0}

 def observe(self,pods,now=None):
  now=time.time() if now is None else now;self.samples+=1
  if not pods:self.missing_samples+=1
  failures=[]
  for pod in pods:
   if pod.get('role') not in {None,'main'}:
    raise operations.Blocked('old_probe_or_unknown_role_in_main_sample')
   name=pod['pod']
   for key in ('attempts','successes','failures'):
    if not isinstance(pod.get(key),int) or pod[key]<0:raise operations.Blocked('probe_counter_schema_invalid')
   if name not in self.pods:self.pods[name]=self.new_record(pod)
   record=self.pods[name]
   if pod.get('generation')!=record['generation']:raise operations.Blocked('generation_changed_inside_existing_pod')
   if any(pod[key]<record['last'][key] for key in ('attempts','successes','failures')):
    self.counter_resets.append({'pod':name,'timestamp_utc':operations.utc(now)})
    raise operations.Blocked('pod_counters_reset_during_measurement')
   new_failures=pod['failures']-record['last']['failures']
   if name not in self.seeded and record['observations']==0:new_failures=pod['failures']
   if new_failures>0:
    failures.append({'pod':name,'generation':pod.get('generation'),'new_failed_requests':new_failures,'last_http':pod.get('last_http'),'oauth_error':pod.get('oauth_error'),'last_result':pod.get('last_result'),'last_attempt_utc':pod.get('last_attempt_utc')})
   attempted=operations.event_epoch(pod.get('last_attempt_utc'))
   if pod['attempts'] and attempted is not None and -5<=now-attempted<=12 and (pod.get('last_http')!=200 or pod.get('latest_authentication_ok') is False):
    if not any(f['pod']==name for f in failures):failures.append({'pod':name,'generation':pod.get('generation'),'new_failed_requests':new_failures,'last_http':pod.get('last_http'),'oauth_error':pod.get('oauth_error'),'last_result':pod.get('last_result'),'last_attempt_utc':pod.get('last_attempt_utc')})
   for key in ('attempts','successes','failures'):record['maxima'][key]=max(record['maxima'][key],pod[key]);record['last'][key]=pod[key]
   record['last_observed_utc']=operations.utc(now);record['observations']+=1
  self.error_events.extend(failures)
  return failures

 def summary(self):
  records=[]
  totals={key:0 for key in ('attempts','successes','failures')}
  for pod in self.pods.values():
   delta={key:pod['maxima'][key]-pod['baseline'][key] for key in totals}
   for key,value in delta.items():totals[key]+=value
   records.append({key:value for key,value in pod.items() if key!='last'}|{'observed_delta':delta})
  return {'main_only':True,'sample_count':self.samples,'samples_with_no_main_data':self.missing_samples,
          'observed_request_lower_bounds':totals,'per_pod':records,'observed_authentication_failures':self.error_events,
          'counter_resets':self.counter_resets,'measurement_limit':'Per-pod counter maxima are lower bounds. Requests after the last sample and before pod deletion may be missed. Initial counters of preexisting pods are subtracted. No claim is made about unobserved requests or production availability.'}


class Recorder:
 def __init__(self,path):
  path.parent.mkdir(parents=True,exist_ok=True)
  self.file=path.open('x');self.path=path
 def record(self,event,**fields):
  value={'timestamp_utc':operations.utc(),'event':event,**fields}
  self.file.write(json.dumps(value,sort_keys=True)+'\n');self.file.flush()
 def close(self):self.file.close()


def wait_control(crc,command,deadline):
 name=crc.launch(command)
 while time.monotonic()<deadline:
  status=crc.job_status(name)
  if status!='Running':
   if status!='Complete':raise operations.Failed('control_job_failed_'+command)
   return name
  time.sleep(min(2,max(0,deadline-time.monotonic())))
 raise operations.Blocked('control_job_timeout_'+command)


def clean_stop(crc,deadline,recorder):
 crc.cron_suspend()
 recorder.record('cronjob_suspended_no_more_schedules')
 while time.monotonic()<deadline:
  active=[job['metadata']['name'] for job in lab_jobs(crc) if terminal(job)=='Running']
  if not active:break
  time.sleep(min(2,max(0,deadline-time.monotonic())))
 else:raise operations.Blocked('active_controller_jobs_remain_after_suspending_schedule')
 if time.monotonic()>=deadline:raise operations.Blocked('no_time_remaining_to_suspend_persisted_state')
 stop_job=wait_control(crc,'suspend',deadline)
 state,_=full_state(crc)
 cron=crc.owned(crc.get('cronjob','rotation-controller'))
 if not cron['spec'].get('suspend') or state.get('suspended') is not True:
  raise operations.Blocked('final_suspension_not_confirmed')
 if any(terminal(job)=='Running' for job in lab_jobs(crc)):
  raise operations.Blocked('controller_job_active_after_stop')
 recorder.record('observer_stopped',suspend_job=stop_job,final_state=state,cronjob_suspended=True)
 return state


def execute(args):
 stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
 directory=ROOT/'evidence/periodic'
 recorder=Recorder(directory/(stamp+'-observations.jsonl'))
 report={'scenario':'T06','status':'BLOCKED','started_utc':operations.utc(),'required_cycles':args.cycles,'timeout_seconds':args.timeout,'sample_interval_seconds':2,'observations_file':str(recorder.path.relative_to(ROOT))}
 crc=None;counter=None;resumed=False;interrupted=False
 completed={};seen_jobs={};intervals=[];correlations=[];proof=None
 started=time.monotonic();deadline=started+args.timeout
 old_handlers={}
 def stop_signal(signum,frame):
  nonlocal interrupted
  interrupted=True
 for signum in (signal.SIGINT,signal.SIGTERM):old_handlers[signum]=signal.signal(signum,stop_signal)
 try:
  crc=operations.CRC()
  baseline=crc.precondition()
  initial,history=full_state(crc)
  if initial.get('phase') not in {'IDLE','COMPLETE'}:raise operations.Blocked('initial_state_is_not_terminal')
  existing={h.get('cycle_id') for h in history}
  if any(terminal(job)=='Running' for job in lab_jobs(crc)):raise operations.Blocked('active_job_before_resume')
  first=crc.samples();counter=Counters(first)
  if not crc.healthy(initial['generation']):raise operations.Blocked('initial_application_not_healthy')
  cron=crc.owned(crc.get('cronjob','rotation-controller'));cron_uid=cron['metadata']['uid']
  report['initial_generation']=initial['generation'];report['initial_state']=initial
  recorder.record('observer_started',initial_state=initial,initial_main_pods=first,old_secret_probes_excluded=True)
  if time.monotonic()>=deadline-90:raise operations.Blocked('preflight_consumed_observation_budget')
  resume_job=wait_control(crc,'resume',min(deadline-60,time.monotonic()+60));resumed=True
  report['resume_job']=resume_job;report['resume_completed_utc']=operations.utc()
  recorder.record('cronjob_resumed_once',resume_job=resume_job)
  proof=ScheduledProof(cron_uid,crc.started,resume_job)
  completed={};seen_jobs=proof.jobs;last_nonempty=time.monotonic();previous_sample=None;intervals=[]
  observation_deadline=deadline-60
  while time.monotonic()<observation_deadline:
   tick=time.monotonic()
   if interrupted:raise operations.Blocked('observation_interrupted_by_signal')
   if abs((time.time()-crc.started)-(time.monotonic()-crc.started_mono))>3:raise operations.Blocked('host_clock_jump_during_periodic_test')
   state,history=full_state(crc);pods=crc.samples()
   failure=counter.observe(pods)
   if pods:last_nonempty=time.monotonic()
   if previous_sample is not None:intervals.append(tick-previous_sample)
   previous_sample=tick
   for item in history:
    if item.get('cycle_id') not in existing and item.get('generation',0)>initial['generation']:
     if not item.get('old_rejected') or not item.get('completed_at'):raise operations.Failed('history_cycle_lacks_old_expiry_or_completion_evidence')
     completed[item['cycle_id']]=item
   proof.observe(crc,lab_jobs(crc),recorder)
   correlations=proof.correlate(completed)
   recorder.record('main_sample',state=state,completed_cycle_count=len(completed),main_pods=pods,scheduled_jobs=list(seen_jobs.values()))
   if failure:raise operations.Failed('main_application_authentication_failure_observed')
   if time.monotonic()-last_nonempty>12:raise operations.Blocked('main_application_observation_gap_over_12_seconds')
   if state.get('phase') in {'BLOCKED','FAILED'}:raise operations.Failed('controller_entered_'+state['phase'])
   if len(completed)>=args.cycles and len(correlations)==len(completed):
    generations=sorted(row['generation'] for row in completed.values())
    expected=list(range(initial['generation']+1,initial['generation']+len(completed)+1))
    if generations!=expected:raise operations.Failed('completed_generations_are_not_consecutive')
    if state.get('phase')!='COMPLETE' or not crc.healthy(state['generation']):raise operations.Failed('final_periodic_generation_not_healthy')
    report.update(status='PASS',reason='three_or_more_real_scheduled_cycles_and_no_observed_main_auth_failures')
    break
   time.sleep(max(0,min(2-(time.monotonic()-tick),observation_deadline-time.monotonic())))
  else:raise operations.Blocked('timeout_before_required_scheduled_cycles_with_correlated_job_evidence')
  report['cycle_job_correlations']=correlations
  report['completed_cycles']=list(sorted(completed.values(),key=lambda row:row['generation']))
  report['scheduled_jobs']=list(seen_jobs.values())
  report['observed_sample_interval_seconds']={'min':min(intervals) if intervals else None,'max':max(intervals) if intervals else None,'mean':sum(intervals)/len(intervals) if intervals else None}
 except operations.Failed as exc:report.update(status='FAIL',reason=str(exc))
 except operations.Blocked as exc:report.update(status='BLOCKED',reason=str(exc))
 except (KeyError,TypeError,ValueError,OSError):report.update(status='BLOCKED',reason='unexpected_runtime_schema_or_local_io_failure')
 finally:
  if crc:
   try:
    # Suspend the schedule first; then wait for any already-created Jobs before state suspension.
    report['final_state']=clean_stop(crc,deadline,recorder)
    report['final_cronjob_suspended']=True
   except (operations.Blocked,operations.Failed,KeyError,TypeError,ValueError,OSError):
    report['final_cronjob_suspended']=None;report['cleanup_warning']='verify_cronjob_and_active_jobs_before_any_further_operation'
    if report['status']=='PASS':report.update(status='BLOCKED',reason='periodic_assertions_passed_but_final_suspension_unverified')
  if counter:
   if crc and report.get('final_cronjob_suspended') is True:
    try:
     final_pods=crc.samples();failures=counter.observe(final_pods)
     if len(final_pods)<2 and report['status']=='PASS':report.update(status='BLOCKED',reason='final_main_replica_observation_incomplete')
     recorder.record('final_main_sample_after_stop',main_pods=final_pods)
     if failures:report.update(status='FAIL',reason='main_application_authentication_failure_observed_during_stop')
    except (operations.Blocked,operations.Failed,KeyError,TypeError,ValueError,OSError):
     if report['status']=='PASS':report.update(status='BLOCKED',reason='final_main_observation_unavailable')
   report['continuity']=counter.summary()
  report['cycle_job_correlations']=correlations
  report['completed_cycles']=list(sorted(completed.values(),key=lambda row:row['generation']))
  report['scheduled_jobs']=list(seen_jobs.values())
  report['observed_sample_interval_seconds']={'min':min(intervals) if intervals else None,'max':max(intervals) if intervals else None,'mean':sum(intervals)/len(intervals) if intervals else None}
  report['finished_utc']=operations.utc();report['elapsed_seconds']=round(time.monotonic()-started,3)
  recorder.record('observer_final_summary',status=report['status'],reason=report.get('reason'),continuity=report.get('continuity'),final_cronjob_suspended=report.get('final_cronjob_suspended'))
  recorder.close()
  for signum,handler in old_handlers.items():signal.signal(signum,handler)
  directory.mkdir(parents=True,exist_ok=True);path=directory/(stamp+'-summary.json');path.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
  print(json.dumps({'scenario':'T06','status':report['status'],'reason':report.get('reason'),'summary':str(path.relative_to(ROOT)),'observations':report['observations_file'],'observed_request_lower_bounds':report.get('continuity',{}).get('observed_request_lower_bounds'),'elapsed_seconds':report['elapsed_seconds']},sort_keys=True))
 return 0 if report['status']=='PASS' else (1 if report['status']=='FAIL' else 2)


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--cycles',type=int,default=3)
 parser.add_argument('--timeout',type=int,default=960,help='Total observation/stop budget, at most 1000 seconds')
 args=parser.parse_args()
 if args.cycles<3:parser.error('--cycles must be at least 3')
 if not 180<=args.timeout<=1000:parser.error('--timeout must be between 180 and 1000 seconds')
 return execute(args)
if __name__=='__main__':raise SystemExit(main())
