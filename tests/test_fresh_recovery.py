import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from agentsmon import config, dashboard, identity, keepalive


class FreshRecovery(unittest.TestCase):
    SID = '11111111-1111-4111-8111-111111111111'

    def test_dead_agent_without_pin_starts_fresh_and_warns_in_dashboard(self):
        agent = {'name':'Writer','kind':'claude-code','cwd':'/work',
                 'match':'claude','restart':'claude --continue --model example'}
        cfg = {'agents':[agent]}
        with tempfile.TemporaryDirectory() as d, patch.object(config,'state_dir',return_value=Path(d)), \
             patch.object(keepalive.detect,'discover_agents',return_value=[]), \
             patch.object(keepalive.detect,'tmux_sessions',return_value=[]), \
             patch.object(keepalive.detect,'_proc_table',return_value=({},{})), \
             patch.object(keepalive,'_log') as log, \
             patch.object(keepalive,'_start',return_value={'name':'Writer','state':'sent'}) as start, \
             patch.object(keepalive,'_confirm',side_effect=lambda ls:[{**x,'state':'confirmed','pid':1} for x in ls]):
            self.assertEqual(keepalive.tick(cfg),1)
            launch = shlex.split(start.call_args.args[0]['restart'])
            self.assertNotIn('--continue',launch)
            self.assertIn('--model',launch)
            self.assertTrue(any('fresh' in c.args[0].lower() for c in log.call_args_list))
            row = {**agent,'alive':True,'session_id':None,'label':'Claude','vendor':'anthropic'}
            with patch.object(dashboard.detect,'discover_agents',return_value=[row]):
                rows = dashboard._agents_state(cfg)
            self.assertIn('fresh',rows[0]['restart_warning'].lower())

    def test_new_agent_crashing_before_first_turn_keeps_launch_options(self):
        agent = {'name':'Writer','kind':'codex','cwd':'/work','identity_required':True,'restart':'',
                 'fresh_start':"'/tmp/cli path/codex' --model example -c effort=low"}
        safe = identity.safe_restart(agent,[],{})
        self.assertEqual(safe['restart'],agent['fresh_start'])
        self.assertTrue(safe['restart_warning'])

    def test_conflict_never_resumes_another_live_conversation_or_stale_pin(self):
        agent = {'name':'Writer','kind':'codex','cwd':'/work',
                 'restart':f'codex --model example resume {self.SID}'}
        conflict = {**agent,'alive':True,'session_id':self.SID,'resume_cmd':None,'identity_conflict':True}
        safe = identity.safe_restart(agent,[conflict],{'Writer':{'id':self.SID,'cwd':'/work'}})
        self.assertNotIn(self.SID,safe['restart'])
        self.assertNotIn('resume',shlex.split(safe['restart']))

    def test_continue_and_resume_selectors_are_removed_but_codex_config_is_preserved(self):
        cases = [('claude --continue --model example','--model example'),
                 (f'claude -r {self.SID} --model example','--model example'),
                 ('codex -c effort=low resume --last --all','-c effort=low'),
                 (f'agy --conversation={self.SID} --model example','--model example')]
        for command, retained in cases:
            safe = identity.safe_restart({'name':'Writer','restart':command,'cwd':'/work'},[],{})
            self.assertNotIn(self.SID,safe['restart'])
            self.assertIn(retained,safe['restart'])
            self.assertFalse(set(shlex.split(safe['restart'])) & {'resume','--last','--all','-r','--continue'})

    def test_known_pin_resumes_and_another_agents_warning_is_not_reused(self):
        agent = {'name':'Writer','kind':'codex','cwd':'/work','identity_required':True,'restart':''}
        pin = {'id':self.SID,'cwd':'/work','config':str(config.DEFAULT_PATH.absolute()),
               'restart':'codex resume '+self.SID}
        safe = identity.safe_restart(agent,[],{'Writer':pin})
        self.assertIn(self.SID,safe['restart'])
        self.assertFalse(safe.get('restart_warning'))
        with tempfile.TemporaryDirectory() as d, patch.object(config,'state_dir',return_value=Path(d)):
            (Path(d)/'restart-notices.json').write_text(json.dumps({'Writer':{
                'config':'/unrelated/config','cwd':'/work','warning':'not ours'}}))
            self.assertEqual(identity.restart_warnings({'agents':[agent]}),{})

    def test_pin_claimed_by_another_live_agent_starts_fresh(self):
        agent = {'name':'Writer','kind':'codex','cwd':'/work','restart':'codex resume '+self.SID}
        pin = {'id':self.SID,'cwd':'/work','config':str(config.DEFAULT_PATH.absolute()),
               'restart':agent['restart']}
        safe = identity.safe_restart(agent,[{'name':'Other','alive':True,'session_id':self.SID}],{'Writer':pin})
        self.assertEqual(safe['restart'],'codex')
        self.assertTrue(safe['restart_warning'])

    def test_known_pin_keeps_fresh_launch_options_and_clears_notice(self):
        agent = {'name':'Writer','kind':'codex','cwd':'/work','restart':'',
                 'fresh_start':'/tmp/codex --model example -c effort=low'}
        pin = {'id':self.SID,'cwd':'/work','config':str(config.DEFAULT_PATH.absolute()),
               'restart':'codex resume '+self.SID}
        safe = identity.safe_restart(agent,[],{'Writer':pin})
        self.assertEqual(safe['restart'],agent['fresh_start']+' resume '+self.SID)
        with tempfile.TemporaryDirectory() as d, patch.object(config,'state_dir',return_value=Path(d)):
            identity.record_restart(identity.safe_restart(agent))
            self.assertTrue(identity.restart_warnings({'agents':[agent]}))
            identity.record_restart(safe)
            self.assertFalse(identity.restart_warnings({'agents':[agent]}))

    def test_shell_wrapper_without_fresh_command_requires_explicit_configuration(self):
        agent = {'name':'Writer','identity_required':True,'restart':'env WRAPPER=1 codex resume '+self.SID}
        self.assertIsNone(identity.safe_restart(agent))
        agent['fresh_start'] = 'codex --model example'
        self.assertEqual(identity.safe_restart(agent)['restart'],agent['fresh_start'])
