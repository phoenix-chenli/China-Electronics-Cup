"""Cold initialization and instance matching; not Windows real-machine evidence."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock,patch
import unittest

spec=importlib.util.spec_from_file_location('startup_launcher',Path(__file__).resolve().parents[3]/'01_程序代码/启动与打包/launcher.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)

class StartupWaitTests(unittest.TestCase):
    def test_a_large_pdf_startup_can_finish_after_the_old_45_second_cutoff(self):
        process=Mock();process.poll.return_value=None
        ready={'ok':True,'service':'evidenceflow','instance':{'workspace':launcher.WORKSPACE,'id':'our-instance'}}
        with patch.object(launcher.time,'monotonic',side_effect=[0,0,60]),patch.object(launcher.time,'sleep'),patch.object(launcher,'health',side_effect=[{},ready]):
            self.assertTrue(launcher.wait_ready(process,'our-instance'))
        process.terminate.assert_not_called()

    def test_other_instance_is_not_ready_and_wait_is_bounded(self):
        process=Mock();process.poll.return_value=None
        other={'ok':True,'service':'evidenceflow','instance':{'workspace':launcher.WORKSPACE,'id':'other-instance'}}
        with patch.object(launcher.time,'monotonic',side_effect=[0,0,181]),patch.object(launcher.time,'sleep'),patch.object(launcher,'health',return_value=other):
            self.assertFalse(launcher.wait_ready(process,'our-instance'))

    def test_dead_process_stops_wait_immediately(self):
        process=Mock();process.poll.return_value=1
        with patch.object(launcher.time,'monotonic',return_value=0),patch.object(launcher,'health',return_value={}):
            self.assertFalse(launcher.wait_ready(process,'our-instance'))
