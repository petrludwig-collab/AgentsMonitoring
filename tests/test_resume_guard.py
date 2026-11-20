from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentsmon import config, dashboard, identity, keepalive


class ResumeGuard(unittest.TestCase):
    SID = '11111111-1111-4111-8111-111111111111'

    def test_pin_survives_process_exit_and_preserves_exact_conversation(self):
        agent = {'name':'Writer','kind':'codex','cwd':'/work','identity_required':True,'restart':''}
        live = [{**agent, 'alive':True,'session_id':self.SID,'resume_cmd':'codex resume '+self.SID}]
        with tempfile.TemporaryDirectory() as d, patch.object(config,'state_dir',return_value=Path(d)):
            pins = identity.pin_sessions({'agents':[agent]}, live)
            safe = identity.safe_restart(agent, [], pins)
            self.assertIn('resume '+self.SID, safe['restart'])
            self.assertEqual((Path(d)/'session-pins.json').stat().st_mode & 0o777, 0o600)
            # An unresolved live process must invalidate its old pin.
            live[0].update(session_id=None, resume_cmd=None)
            pins = identity.pin_sessions({'agents':[agent]}, live)
            safe = identity.safe_restart(agent, live, pins)
            self.assertNotIn(self.SID, safe['restart'])
            self.assertTrue(safe['restart_warning'])
            self.assertEqual(pins, {})

    def test_ambiguous_dashboard_restart_starts_fresh_with_warning(self):
        agent = {'name':'Writer','kind':'codex','cwd':'/work','restart':'codex --continue'}
        cfg = {'agents':[agent]}
        with tempfile.TemporaryDirectory() as d, patch.object(config,'state_dir',return_value=Path(d)), \
             patch.object(config,'load',return_value=cfg), \
             patch.object(dashboard.detect,'discover_agents',return_value=[
                 {**agent,'alive':True,'session_id':None,'resume_cmd':None}]), \
             patch.object(dashboard,'_set_enabled') as enabled, \
             patch.object(keepalive,'_start',return_value={'name':'Writer','state':'sent'}) as start, \
             patch.object(keepalive,'_confirm',side_effect=lambda ls:[{**x,'state':'confirmed','pid':1} for x in ls]), \
             patch.object(keepalive,'_log'):
            ok, message = dashboard._agent_action('Writer','restart')
        self.assertTrue(ok)
        self.assertIn('Fresh', message)
        enabled.assert_called_once_with('Writer', True)
        self.assertNotIn('--continue', start.call_args.args[0]['restart'])

    def test_configured_resume_options_are_preserved(self):
        old = '22222222-2222-4222-8222-222222222222'
        agent = {'name':'Writer','restart':f'codex --model example resume {old}','cwd':'/work'}
        row = {'name':'Writer','alive':True,'session_id':self.SID,'resume_cmd':'codex resume '+self.SID}
        safe = identity.safe_restart(agent, [row])
        self.assertEqual(safe['restart'], f'codex --model example resume {self.SID}')
