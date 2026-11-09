"""External expired-recovery wrapper safety tests, without cluster I/O."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[2]/'scripts/recover_expired.py'
spec=importlib.util.spec_from_file_location('recover_wrapper_tests',SOURCE)
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)


class CRC:
    def __init__(self):
        self.current={'phase':'IDLE','generation':1,'suspended':True}
        self.events=[];self.created_jobs=[];self.commands=[];self.active=False;self.failure=False
    def cron_suspend(self):pass
    def no_live_jobs(self):
        if self.active:raise recovery.operations.Blocked('another_lab_job_is_active')
    def state(self):return self.current.copy()
    def run_job(self,command):
        self.commands.append(command);self.created_jobs.append('job-'+command)
        if self.failure:
            self.current['phase']='ROTATING'
            raise recovery.operations.Failed('injected_failure')
        self.current.update(phase='COMPLETE',generation=2,recovery_mode='expired-current',preexisting_outage=True,
                            old_retired_early=True,old_probe_rejected=True,suspended=True)
        return 'job-'+command
    def wait_healthy(self,generation):
        if generation!=2:raise recovery.operations.Failed('wrong_generation')
    def owned(self,obj):return obj
    def get(self,*args):return {'spec':{'suspend':True}}
    def job_status(self,name):return 'Failed' if self.failure else 'Complete'


class RecoveryWrapperTests(unittest.TestCase):
    def execute(self,crc):
        with tempfile.TemporaryDirectory() as directory,patch.object(recovery,'ROOT',Path(directory)), \
             patch.object(recovery.operations,'CRC',lambda:crc),contextlib.redirect_stdout(io.StringIO()):
            result=recovery.main()
            report=json.loads(next((Path(directory)/'evidence/recovery').glob('*.json')).read_text())
            return result,report
    def test_success_is_explicit_outage_recovery_not_continuity_claim(self):
        crc=CRC();result,report=self.execute(crc)
        self.assertEqual(result,0);self.assertEqual(report['status'],'PASS')
        self.assertFalse(report['continuity_claim'])
        self.assertEqual(crc.commands,['recover-expired'])
    def test_existing_active_job_prevents_recovery_job_creation(self):
        crc=CRC();crc.active=True
        result,report=self.execute(crc)
        self.assertEqual(result,2);self.assertEqual(crc.commands,[])
    def test_pending_cycle_demands_reconcile_not_another_recovery(self):
        crc=CRC();crc.current['phase']='RETIRING_OLD'
        result,report=self.execute(crc)
        self.assertEqual(result,2);self.assertEqual(crc.commands,[])
        self.assertIn('reconcile',report['reason'])
    def test_failed_job_is_never_resubmitted_by_wrapper(self):
        crc=CRC();crc.failure=True
        result,report=self.execute(crc)
        self.assertEqual(result,1);self.assertEqual(crc.commands,['recover-expired'])
        self.assertEqual(report['final_state']['phase'],'ROTATING')


if __name__=='__main__':unittest.main()
