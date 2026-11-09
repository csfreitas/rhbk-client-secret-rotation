#!/usr/bin/env python3
"""Explicit recovery after the suspended laboratory's current secret has expired.

A successful run restores the application but leaves automation suspended. It
records the preexisting authentication outage; it is not a normal overlap test.
"""
import datetime as dt
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests/integration'))
import operations


def main():
    crc=None
    report={'operation':'recover-expired','status':'BLOCKED','started_utc':operations.utc(),
            'explicit_manual_recovery':True,'continuity_claim':False}
    try:
        crc=operations.CRC()
        crc.cron_suspend()
        crc.no_live_jobs()
        before=crc.state()
        if before.get('phase') not in {'IDLE','COMPLETE'}:
            raise operations.Blocked('pending_cycle_requires_reconcile_instead_of_another_recovery')
        if not before.get('suspended'):
            crc.run_job('suspend')
        crc.no_live_jobs()
        job=crc.run_job('recover-expired')
        after=crc.state()
        operations.require(after.get('phase')=='COMPLETE' and after.get('generation')==before['generation']+1,
                           'recovery_did_not_complete_exactly_one_generation')
        operations.require(after.get('recovery_mode')=='expired-current' and after.get('preexisting_outage') is True,
                           'explicit_recovery_mode_missing_from_evidence')
        operations.require(after.get('old_retired_early') is True and after.get('old_probe_rejected') is True,
                           'old_credential_invalidation_not_verified')
        crc.wait_healthy(after['generation'])
        cron=crc.owned(crc.get('cronjob','rotation-controller'))
        operations.require(cron['spec'].get('suspend') and after.get('suspended') is True,
                           'recovery_must_leave_automation_suspended')
        report.update(status='PASS',reason='expired_generation_recovered_and_previous_credential_invalidated',
                      recovery_job=job,initial_generation=before['generation'],final_state=after)
    except operations.Failed as exc:
        report.update(status='FAIL',reason=str(exc))
    except operations.Blocked as exc:
        report.update(status='BLOCKED',reason=str(exc))
    except (KeyError,ValueError,TypeError,OSError):
        report.update(status='BLOCKED',reason='unexpected_recovery_configuration_or_schema')
    finally:
        if crc:
            try:
                crc.cron_suspend()
                report['final_state']=crc.state()
                report['active_jobs_requiring_followup']=[name for name in crc.created_jobs if crc.job_status(name)=='Running']
                if report['active_jobs_requiring_followup'] and report['status']=='PASS':
                    report.update(status='BLOCKED',reason='active_jobs_remain_after_recovery')
            except (operations.Blocked,operations.Failed,KeyError,ValueError,TypeError):
                report['final_suspend_warning']='verify_cronjob_and_pending_cycle_before_retry'
                if report['status']=='PASS':report.update(status='BLOCKED',reason='final_suspension_unverified')
            report['events']=crc.events
            report['created_jobs']=crc.created_jobs
        report['finished_utc']=operations.utc()
        directory=ROOT/'evidence/recovery';directory.mkdir(parents=True,exist_ok=True)
        name=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-expired.json'
        (directory/name).write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in {'events','final_state'}},sort_keys=True))
    return 0 if report['status']=='PASS' else (2 if report['status']=='BLOCKED' else 1)


if __name__=='__main__':raise SystemExit(main())
