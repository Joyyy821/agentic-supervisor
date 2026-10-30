"""Budgets and milestones retain project identity and conservative recovery."""
import asyncio
import json
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agentic_supervisor.codex import RpcClient, VALIDATED_VERSIONS
from agentic_supervisor.config import ConfigError, load_config
from agentic_supervisor.runtime import Runtime, _record_goal_limited, config_from_snapshot
from tests.unit.helpers import new_git_repo, test_root
import uuid

class PersistentTests(unittest.TestCase):
    def setUp(self):
        self.root = test_root() / ('stage-' + uuid.uuid4().hex[:8]); self.root.mkdir()
        self.repo = new_git_repo('stages')
        self.path = self.root / 'config.json'
        self.data = dict(version=1, project='generic', repository=str(self.repo),
                         objective='Improve the project across milestones',
                         runtime_root=str(self.root / 'runtime'),
                         milestone=dict(name='analysis', objective='Inspect the source'),
                         token_budget=100, time_budget_seconds=60)
        self.path.write_text(json.dumps(self.data)); self.cfg=load_config(self.path)
        self.rt=Runtime(self.cfg.runtime_root, '/usr/bin/true')
        self.run=self.rt.store.create_run(self.cfg, 'codex-cli 0.166.0','revision',False)
        self.rt.store.update_run(self.run, thread_id='same-thread',status='paused',desired_status='paused')

    def test_stage_and_budget_changes_preserve_usage_thread_and_objective(self):
        self.rt.store.observe_token_usage(self.run, 80)
        self.rt.store.extend_budget(self.run, 150, 120)
        self.rt.store.set_milestone(self.run,'implementation','Implement the agreed fix')
        row=self.rt.store.get_run(self.run)
        self.assertEqual((row['thread_id'], row['tokens_used'],row['token_budget']), ('same-thread',80,250))
        self.assertEqual(row['stage_token_start'],80)
        self.assertEqual(row['objective'],self.cfg.objective)
        self.assertEqual(row['milestone_status'],'prepared')
        self.assertIsNotNone(self.rt.store.latest_event(self.run,'supervisor/budget_extended'))

    def test_uncertainty_blocks_budget_milestone_and_resume(self):
        cid=self.rt.store.queue_control(self.run,'send','unknown delivery')
        self.rt.store.update_control(cid,'outcome_unknown')
        with self.assertRaises(ValueError): self.rt.store.extend_budget(self.run, 20, 0)
        with self.assertRaises(ValueError): self.rt.store.set_milestone(self.run,'next','next task')
        with self.assertRaises(Exception): self.rt.submit(self.run,'resume')
        self.rt.store.resolve_control(self.run,cid,'not_applied','Verified no dispatch')
        self.rt.store.extend_budget(self.run,20,0)

    def test_active_time_excludes_review_pause(self):
        with patch('agentic_supervisor.store.time.time',return_value=100):
            self.rt.store.update_run(self.run,status='active')
        with patch('agentic_supervisor.store.time.time',return_value=110):
            self.rt.store.update_run(self.run,status='paused',desired_status='paused')
        with patch('agentic_supervisor.store.time.time',return_value=1000):
            self.assertEqual(self.rt.store.elapsed(self.rt.store.get_run(self.run)),10)

    def test_budget_exhaustion_requires_explicit_extension(self):
        self.rt.store.observe_token_usage(self.run,100)
        with self.assertRaises(Exception): self.rt.submit(self.run,'resume')
        self.rt.store.extend_budget(self.run,50,0)
        self.rt.submit(self.run,'resume')

    def test_repeated_native_goal_limit_poll_is_idempotent(self):
        for _ in range(3):
            row=self.rt.store.get_run(self.run)
            _record_goal_limited(self.rt.store,self.run,row,'budgetLimited')
        events=[event for event in self.rt.store.events(self.run)
                if event['kind']=='supervisor/goal_limited']
        row=self.rt.store.get_run(self.run)
        self.assertEqual(len(events),1)
        self.assertEqual((row['status'],row['desired_status'],row['last_error']),
                         ('paused','paused','Codex goal status: budgetLimited'))

    def test_external_artifacts_and_snapshot(self):
        artifact=self.root/'reports';artifact.mkdir()
        data=self.data | {'artifact_paths':[str(artifact)], 'approvals_reviewer':'auto_review','reasoning_effort':'high'}
        self.path.write_text(json.dumps(data));config=load_config(self.path)
        self.assertEqual(config_from_snapshot(config.safe_snapshot()),replace(config,source=Path('<snapshot>')))
        self.assertIn(str(artifact),self.rt._sandbox_policy(self.run,config)['writableRoots'])
        self.path.write_text(json.dumps(data | {'artifact_paths':[str(self.root)]}))
        with self.assertRaises(ConfigError):load_config(self.path)

    def test_activate_restores_remaining_native_budget_on_same_thread(self):
        async def scenario():
            self.rt.store.observe_token_usage(self.run,20)
            client=AsyncMock()
            client.call.side_effect=[{'goal':{'objective':self.rt.goal_objective(self.rt.store.get_run(self.run)),
                                            'tokensUsed':20,'status':'paused'}},{'goal':{'tokensUsed':20}}, {'turn':{'id':'next-turn'}},{}]
            turn=await self.rt.activate(client,self.run,self.cfg,'Continue')
            self.assertEqual(turn,'next-turn')
            args=client.call.call_args_list[1].args[1]
            self.assertEqual(args['threadId'],'same-thread');self.assertEqual(args['tokenBudget'],100)
            self.assertEqual(args['origin'],'user')
            self.assertEqual(client.call.call_args_list[2].args[1]['sandboxPolicy']['type'],'workspaceWrite')
        asyncio.run(scenario())

    def test_only_audited_versions_allowed(self):
        self.assertIn('codex-cli 0.166.0',VALIDATED_VERSIONS)
        self.assertIn('codex-cli 0.144.4',VALIDATED_VERSIONS)
        self.assertNotIn('codex-cli 0.167.0',VALIDATED_VERSIONS)

    def test_new_protocol_uses_paginated_resume(self):
        async def scenario():
            client=RpcClient('/unused',Path('/unused.sock'),Path('/unused'));client.release='codex-cli 0.166.0'
            client._call=AsyncMock(side_effect=[{'thread':{'id':'t'}},{'data':[{'id':'new'},{'id':'old'}]}])
            r=await client.call('thread/resume',{'threadId':'t'})
            self.assertEqual([t['id'] for t in r['thread']['turns']],['old','new'])
            self.assertTrue(client._call.call_args_list[0].args[1]['excludeTurns'])
        asyncio.run(scenario())

    def test_native_reset_uses_acknowledged_counter(self):
        async def scenario():
            self.rt.store.observe_token_usage(self.run,20)
            client=AsyncMock()
            client.call.side_effect=[{'goal':{'objective':'old goal','tokensUsed':20,'status':'complete'}},
                                     {'goal':{'tokensUsed':0}}, {}, {'turn':{'id':'new'}}, {}]
            await self.rt.activate(client,self.run,self.cfg,'next stage')
            self.assertEqual(client.call.call_args_list[2].args[1]['tokenBudget'],80)
            self.assertEqual(self.rt.store.get_run(self.run)['native_tokens_seen'],0)
            self.rt.store.observe_token_usage(self.run,5)
            self.assertEqual(self.rt.store.get_run(self.run)['tokens_used'],25)
        asyncio.run(scenario())

    def test_server_loss_records_only_unfinished_native_effects(self):
        store=self.rt.store
        store.add_event(self.run,'item/started',{'type':'commandExecution'},'unfinished')
        store.add_event(self.run,'item/started',{'type':'commandExecution'},'finished')
        store.add_event(self.run,'item/completed',{'type':'commandExecution'},'finished')
        store.add_event(self.run,'item/started',{'type':'agentMessage'},'message')
        self.assertEqual(store.mark_native_operations_unknown(self.run),1)
        self.assertEqual(store.mark_native_operations_unknown(self.run),0)
        self.assertEqual(store.attempts(self.run)[0]['name'],'native:unfinished')
        self.assertTrue(store.has_uncertain_work(self.run))
