import secrets
"""Periodic observer counter and timeout guards; no CRC calls."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/integration'))
import operations
spec=importlib.util.spec_from_file_location('periodic',ROOT/'tests/integration/periodic.py')
periodic=importlib.util.module_from_spec(spec);spec.loader.exec_module(periodic)


def sample(name='pod-1',attempts=10,successes=10,failures=0,**fields):
 value={'pod':name,'role':'main','generation':'v0001','attempts':attempts,'successes':successes,'failures':failures,'last_http':200,'last_result':'success','latest_authentication_ok':True,'last_attempt_utc':operations.utc(),'last_success_at':time.time()}
 value.update(fields);return value


class PeriodicSafety(unittest.TestCase):
 def test_preexisting_counters_are_subtracted_and_new_pods_start_at_zero(self):
  counters=periodic.Counters([sample(attempts=20,successes=19,failures=1)])
  self.assertEqual(counters.observe([sample(attempts=23,successes=22,failures=1)]),[])
  self.assertEqual(counters.observe([sample(name='pod-2',attempts=4,successes=4)]),[])
  result=counters.summary()['observed_request_lower_bounds']
  self.assertEqual(result,{'attempts':7,'successes':7,'failures':0})

 def test_failure_counter_catches_error_even_after_latest_attempt_succeeds(self):
  counters=periodic.Counters([sample()])
  failure=counters.observe([sample(attempts=13,successes=12,failures=1)])
  self.assertEqual(failure[0]['new_failed_requests'],1)
  self.assertEqual(counters.summary()['observed_request_lower_bounds']['failures'],1)

 def test_first_observation_of_new_pod_includes_earlier_failure(self):
  counters=periodic.Counters([])
  failure=counters.observe([sample(attempts=4,successes=3,failures=1)])
  self.assertEqual(failure[0]['new_failed_requests'],1)

 def test_counter_reset_cannot_create_false_continuity_success(self):
  counters=periodic.Counters([sample()])
  with self.assertRaisesRegex(operations.Blocked,'counters_reset'):
   counters.observe([sample(attempts=1,successes=1)])

 def test_old_probe_cannot_pollute_main_availability(self):
  counters=periodic.Counters([])
  with self.assertRaisesRegex(operations.Blocked,'old_probe'):
   counters.observe([sample(role='old')])

 def test_history_export_excludes_sensitive_fields(self):
  result=periodic.clean_history({'cycle_id':'unit-cycle','generation':2,'client_secret':secrets.token_urlsafe(20),'post_metadata':{'client.secret.expiration.time':1000,'client.secret.rotated':secrets.token_urlsafe(20)}})
  self.assertEqual(result,{'cycle_id':'unit-cycle','generation':2,'post_metadata':{'client.secret.expiration.time':1000}})

 def test_timeout_limit_is_checked_before_any_execution(self):
  with patch.object(sys,'argv',['periodic.py','--timeout','1001']),patch.object(periodic,'execute') as run,contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
   periodic.main()
  run.assert_not_called()

 def test_expired_cleanup_budget_suspends_schedule_without_launching_job(self):
  class FakeCRC:
   def __init__(self):self.suspended=False
   def cron_suspend(self):self.suspended=True
   def launch(self,*args):raise AssertionError('must not create a job after deadline')
  class Recorder:
   def record(self,*args,**kwargs):pass
  crc=FakeCRC()
  with self.assertRaisesRegex(operations.Blocked,'active_controller_jobs'):
   periodic.clean_stop(crc,time.monotonic()-1,Recorder())
  self.assertTrue(crc.suspended)

def scheduled_job(name='scheduled-1',generation=2,command='evaluate',owner='cron-owned',status='Complete'):
 return {'metadata':{'name':name,'creationTimestamp':operations.utc(1000),
          'labels':operations.LABELS.copy(),'ownerReferences':([{'kind':'CronJob','uid':owner}] if owner else [])},
         'spec':{'template':{'spec':{'containers':[{'name':'controller','args':[command]}]}}},
         'status':{'conditions':[{'type':status,'status':'True'}] if status!='Running' else []}}


def generation_logs(generation):
 return [{'event':'rotation_response_received','generation':f'v{generation:04d}','http':200},
         {'event':'state_transition','phase':'COMPLETE','generation':generation},
         {'event':'authentication','role':'baseline','generation':f'v{generation-1:04d}','http':200,'ok':True},
         {'event':'authentication','role':'old_after_expiry','generation':f'v{generation-1:04d}','http':401,'ok':False,'error':'unauthorized_client'},
         {'event':'authentication','role':'main_after_old_expiry','generation':f'v{generation:04d}','http':200,'ok':True}]


def completed_cycle(generation=2,**extra):
 return {'cycle_id':'cycle-'+str(generation),'generation':generation,'completed_at':1100,'old_rejected':True,
         'recovery_mode':None,'preexisting_outage':False,'old_rejection_basis':'natural-expiration',**extra}


class ScheduledProofTests(unittest.TestCase):
 def setUp(self):
  self.rows={};self.recorded=[]
  class CRC:
   def logs(inner,name):return self.rows.get(name,[])
   def get(inner,*args):return {'items':getattr(self,'jobs',[])}
  class Recorder:
   def record(inner,event,**fields):self.recorded.append({'event':event,**fields})
  self.crc=CRC();self.recorder=Recorder();self.proof=periodic.ScheduledProof('cron-owned',1000.5,'resume-job')
 def observe(self,*jobs):self.proof.observe(self.crc,list(jobs),self.recorder)
 def test_owned_controller_job_is_found_without_optional_app_label(self):
  self.jobs=[scheduled_job()]
  self.assertEqual(len(periodic.lab_jobs(self.crc)),1)
  self.jobs[0]['metadata']['labels']={}
  self.assertEqual(periodic.lab_jobs(self.crc),[])
 def test_history_alone_cannot_prove_any_scheduled_cycle(self):
  cycles={str(g):completed_cycle(g) for g in [2,3,4]}
  self.assertEqual(self.proof.correlate(cycles),[])
 def test_terminal_logs_correlate_all_three_generations_to_real_cron_jobs(self):
  jobs=[];cycles={}
  for generation in [2,3,4]:
   name='scheduled-'+str(generation);jobs.append(scheduled_job(name,generation))
   self.rows[name]=generation_logs(generation);cycles[name]=completed_cycle(generation)
  self.observe(*jobs)
  result=self.proof.correlate(cycles)
  self.assertEqual([r['generation'] for r in result],[2,3,4])
  self.assertTrue(all(r['post_response_logged'] and r['complete_logged'] and r['scheduled_owner_uid']=='cron-owned' for r in result))
  self.assertEqual(len(self.recorded),3)
 def test_job_evidence_survives_cron_history_garbage_collection(self):
  self.rows['scheduled-1']=generation_logs(2);self.observe(scheduled_job())
  self.observe()  # Job disappeared from the next list, but its terminal logs are archived.
  result=self.proof.correlate({'cycle':completed_cycle()})
  self.assertEqual(result[0]['job'],'scheduled-1')
 def test_missing_post_or_authentication_control_never_correlates(self):
  for omitted in ['rotation_response_received','baseline','old_after_expiry','main_after_old_expiry']:
   proof=periodic.ScheduledProof('cron-owned',1000,'resume-job')
   self.rows['scheduled-1']=[r for r in generation_logs(2) if r.get('event')!=omitted and r.get('role')!=omitted]
   proof.observe(self.crc,[scheduled_job()],self.recorder)
   self.assertEqual(proof.correlate({'cycle':completed_cycle()}),[])
 def test_wrong_generation_between_response_and_completion_does_not_correlate(self):
  self.rows['scheduled-1']=generation_logs(2);self.rows['scheduled-1'][0]['generation']='v0003'
  self.observe(scheduled_job());self.assertEqual(self.proof.correlate({'cycle':completed_cycle()}),[])
 def test_recovery_and_manual_jobs_are_rejected(self):
  for owner,command in [(None,'recover-expired'),('cron-owned','recover-expired'),('different-cron','evaluate')]:
   with self.assertRaises(operations.Failed):self.observe(scheduled_job(owner=owner,command=command))
 def test_recovery_history_cannot_count_even_under_a_real_cron_owner(self):
  self.rows['scheduled-1']=generation_logs(2);self.observe(scheduled_job())
  for extra in [{'recovery_mode':'expired-current'},{'preexisting_outage':True},{'old_rejection_basis':'explicit-invalidation'},{'old_retired_early':True}]:
   with self.assertRaises(operations.Failed):self.proof.correlate({'cycle':completed_cycle(**extra)})
 def test_running_job_cannot_confirm_completion_and_logs_retry_when_missing(self):
  self.rows['scheduled-1']=generation_logs(2);self.observe(scheduled_job(status='Running'))
  self.assertEqual(self.proof.correlate({'cycle':completed_cycle()}),[])
  self.rows['scheduled-1']=[];self.observe(scheduled_job())
  self.assertEqual(self.proof.correlate({'cycle':completed_cycle()}),[])
  self.rows['scheduled-1']=generation_logs(2);self.observe(scheduled_job())
  self.assertEqual(len(self.proof.correlate({'cycle':completed_cycle()})),1)
 def test_two_jobs_claiming_same_generation_are_ambiguous(self):
  for name in ['scheduled-1','scheduled-2']:self.rows[name]=generation_logs(2)
  self.observe(scheduled_job('scheduled-1'),scheduled_job('scheduled-2'))
  with self.assertRaisesRegex(operations.Failed,'multiple_scheduled_job'):
   self.proof.correlate({'cycle':completed_cycle()})


if __name__=='__main__':unittest.main()
