from __future__ import annotations

import concurrent.futures
from dataclasses import replace
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_task_graph as graph
from agent_runtime import FakeRuntime, FakeRuntimeOutcome, HermesRuntime, NativeRuntime, RuntimeResult, RuntimeRole
from model_provider import FakeModelProvider, FakeModelProviderOutcome
from reviewer_judge import (ReviewStore, ReviewError, validate_review, parse_review, REVIEW_BEGIN, REVIEW_END, MARKER)
from shared_context import ContextProjector, SharedContextStore, canonical_json, content_sha256
from worker_pool import WorkerPool

ORCH = graph.ORCHESTRATOR


def payload(assessment='pass', **extra):
    return {'schema_version': 1, 'assessment': assessment, 'summary': 'Checked the supplied result',
            'findings': [], 'required_changes': [], **extra}


def output(value):
    return REVIEW_BEGIN + '\n' + canonical_json(value) + '\n' + REVIEW_END + '\n' + MARKER


class ReviewerJudgeTest(unittest.TestCase):
    connect = graph.TaskGraphTest.connect
    _seed = graph.TaskGraphTest._seed
    task = graph.TaskGraphTest.task
    plan = graph.TaskGraphTest.plan
    states = graph.TaskGraphTest.states

    def setUp(self):
        graph.TaskGraphTest.setUp(self)
        with self.connect() as c:
            c.execute('''INSERT INTO roles (
                role_id,profile_name,role_kind,description,reasoning_effort,max_turns,toolsets_json,skills_json,
                workspace_mode,may_commit,may_push,network_enabled,cpu_limit,memory_mb,enabled,
                config_source,config_hash,registered_at,updated_at,runtime_kind,model_id
                ) SELECT 'reviewer','ops-reviewer','reviewer',description,reasoning_effort,max_turns,toolsets_json,skills_json,
                'read_only',0,0,0,cpu_limit,memory_mb,enabled,config_source,config_hash,registered_at,updated_at,
                'native',model_id FROM roles WHERE role_id='orchestrator' ''')
        self.sandbox = replace(graph.TaskGraphTest.runtime_request(self,"review").sandbox,read_only=True)
        self.store = ReviewStore(self.connect)
        self.projector = ContextProjector(self.connect)
        self.executor = graph.ManualExecutor()
        self.pool = WorkerPool(self.connect, lambda _: None, controller_instance_id='graph-controller',
                               max_concurrency=2, executor=self.executor)
        self.addCleanup(self.pool.shutdown)

    def create(self, *, diamond=False, required=True):
        tasks = [self.task(k, deps) for k, deps in
                 ([('a', []), ('b', ['a']), ('c', ['a']), ('d', ['b', 'c'])] if diamond else [('b', []), ('d', ['b'])])]
        for task in tasks:
            if task['key'] in ('b', 'c') and required:
                task['review'] = {'required': True}
        plan_id = ORCH.insert_plan(ORCH.validate_plan(self.plan(tasks), allow_test_actions=False), source='AI', initial_status='READY')
        with self.connect() as c:
            c.execute('''INSERT INTO objective_queue(objective_id,objective,source,status,priority,not_before,
                project_scope_json,max_parallel_tasks,planning_max_attempts,planning_attempt_count,plan_id,created_at,heartbeat_at)
                VALUES (?,'review objective','AI','RUNNING',100,?,?,2,3,1,?,?,?)''',
                ('objective-'+plan_id, graph.NOW, json.dumps(['project-'+t['key'] for t in tasks]), plan_id, graph.NOW, graph.NOW))
        ORCH.refresh_plan_states(plan_id)
        return plan_id

    def start(self, plan_id, key, *, run=False):
        with self.connect() as c:
            task = c.execute('SELECT * FROM orchestration_tasks WHERE plan_id=? AND task_key=?', (plan_id,key)).fetchone()
        assignment = self.pool.submit(task['orchestration_task_id'], task['role_id'], 'native')
        attempt, _, task = ORCH.reserve_attempt(task['orchestration_task_id'], instance_id='graph-controller')
        self.pool.bind_attempt(assignment, attempt)
        snapshot = self.projector.freeze_task(task_id=task['orchestration_task_id'], assignment_id=assignment, attempt_id=attempt)
        if run:
            with self.connect() as c:
                c.execute("INSERT INTO runs(run_id,project_id,status,created_at) VALUES (?,?,'COMPLETED',?)",
                          ('run-'+attempt,task['project_id'],graph.NOW))
                c.execute('UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?', ('run-'+attempt,attempt))
        return task, attempt, assignment, snapshot

    def finish(self, started):
        task, attempt, assignment, snapshot = started
        ORCH.finish_task_success(task, attempt, {'output': 'result '+task['task_key']})
        # Complete the actual pool future to verify slot release.
        self.executor.futures[-1].set_result({'output': task['task_key']})
        ORCH.refresh_plan_states(task['plan_id'])
        rows = self.store.list(plan_id=task['plan_id'])
        return next((r['review_id'] for r in rows if r['attempt_id']==attempt), None)

    def ready_review(self, *, run=False):
        plan_id=self.create()
        started=self.start(plan_id,'b',run=run)
        return plan_id, started, self.finish(started)

    def judge(self, review_id, assessment='pass'):
        fake=FakeRuntime([FakeRuntimeOutcome.success(output=output(payload(assessment)))])
        self.assertTrue(self.store.execute(review_id,fake,owner='controller',sandbox=self.sandbox))
        return fake

    def test_success_is_pending_until_review_and_judge_accept(self):
        plan_id, started, rid=self.ready_review()
        self.assertIsNotNone(rid)
        self.assertEqual(self.states(plan_id),{'b':'BLOCKED','d':'PENDING'})
        self.assertEqual(self.store.get(rid)['status'],'PENDING')
        self.judge(rid)
        row=self.store.get(rid)
        self.assertEqual(row['disposition'],'PASS')
        self.assertEqual(row['review_sha256'],content_sha256(payload()))
        self.assertEqual(self.states(plan_id)['d'],'PENDING')
        self.assertTrue(self.store.accept(rid))
        ORCH.refresh_plan_states(plan_id)
        self.assertEqual(self.states(plan_id)['d'],'READY')
        self.assertFalse(self.store.accept(rid))

    def test_exact_snapshot_survives_new_context_and_excludes_other_branch(self):
        plan_id=self.create(diamond=True)
        self.finish(self.start(plan_id,'a'))
        shared=SharedContextStore(self.connect)
        shared.add(project_id='project-b',scope='PROJECT',kind='FACT',key='before',content='historical context')
        b=self.start(plan_id,'b')
        c=self.start(plan_id,'c')
        # These two workers own both pool slots concurrently.
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM worker_pool_assignments WHERE status='RUNNING'").fetchone()[0],2)
        ORCH.finish_task_success(b[0],b[1],{'output':'only B'})
        ORCH.finish_task_success(c[0],c[1],{'output':'unrelated C runtime history'})
        shared.add(project_id='project-b',scope='PROJECT',kind='FACT',key='later',content='must not recompute')
        reviews={r['task_id']:r for r in self.store.list()}
        evidence=self.store.evidence(reviews[b[0]['orchestration_task_id']]['review_id'])
        self.assertEqual(evidence['worker_context'],b[3]['projection'])
        self.assertEqual(evidence['input']['worker_context_snapshot']['sha256'],b[3]['projection_sha256'])
        self.assertEqual(evidence['worker_result'],{'output':'only B'})
        self.assertNotIn('unrelated C',canonical_json(evidence))
        self.assertNotIn('must not recompute',canonical_json(evidence))
        self.assertIn('historical context',canonical_json(evidence))
        ce=self.store.evidence(reviews[c[0]['orchestration_task_id']]['review_id'])
        self.assertEqual(ce['worker_context'],c[3]['projection'])
        self.assertNotIn('only B',canonical_json(ce))

    def test_needs_fix_is_durable_without_retry(self):
        plan_id, started, rid=self.ready_review()
        self.judge(rid,'needs_fix')
        for _ in range(3):
            self.store.reconcile(); ORCH.reconcile_task_graph()
        row=self.store.get(rid)
        self.assertEqual(row['disposition'],'NEEDS_FIX')
        self.assertEqual(self.states(plan_id),{'b':'BLOCKED','d':'PENDING'})
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM orchestration_plans WHERE plan_id=?',(plan_id,)).fetchone()[0],'BLOCKED')
            self.assertEqual(c.execute('SELECT count(*) FROM orchestration_attempts').fetchone()[0],1)
            self.assertIn('result b',c.execute('SELECT result_json FROM orchestration_attempts').fetchone()[0])

    def test_blocked_is_distinct(self):
        plan_id, _, rid=self.ready_review()
        self.judge(rid,'blocked')
        self.assertEqual(self.store.get(rid)['disposition'],'BLOCKED')
        self.assertIsNone(self.store.get(rid)['approval_id'])
        with self.assertRaises(ReviewError): self.store.accept(rid)

    def test_existing_human_gate_and_bounded_resolution(self):
        plan_id, _, rid=self.ready_review(run=True)
        self.judge(rid,'human_review')
        row=self.store.get(rid)
        self.assertEqual(row['human_status'],'PENDING')
        with self.connect() as c:
            self.assertTrue(ORCH.plan_has_active_human_gate(c,plan_id))
        with self.assertRaises(ReviewError): self.store.accept(rid)
        self.store.resolve_human(row['approval_id'],'APPROVE')
        self.assertTrue(self.store.accept(rid))
        ORCH.refresh_plan_states(plan_id)
        self.assertEqual(self.states(plan_id)['d'],'READY')
        self.assertEqual(self.store.get(rid)['disposition'],'HUMAN_REVIEW')
        with self.assertRaises(ReviewError): self.store.resolve_human(row['approval_id'],'REJECT')

    def test_human_rejection_stays_held(self):
        plan_id, _, rid=self.ready_review(run=True)
        self.judge(rid,'human_review')
        self.store.resolve_human(self.store.get(rid)['approval_id'],'REJECT')
        ORCH.reconcile_task_graph()
        self.assertEqual(self.states(plan_id)['d'],'PENDING')
        with self.assertRaises(ReviewError): self.store.accept(rid)

    def test_malformed_output_fails_closed(self):
        plan_id, _, rid=self.ready_review()
        fake=FakeRuntime([FakeRuntimeOutcome.success(output='Looks good!\n'+MARKER)])
        self.assertFalse(self.store.execute(rid,fake,owner='controller',sandbox=self.sandbox))
        self.assertEqual(self.store.get(rid)['failure_code'],'INVALID_OUTPUT')
        self.assertIsNone(self.store.get(rid)['disposition'])
        self.assertEqual(self.states(plan_id)['d'],'PENDING')

    def test_runtime_failure_fails_closed_and_never_redispatches(self):
        _, _, rid=self.ready_review()
        fake=FakeRuntime([FakeRuntimeOutcome.failure('failure with secret text')])
        self.assertFalse(self.store.execute(rid,fake,owner='controller',sandbox=self.sandbox))
        self.assertFalse(self.store.execute(rid,fake,owner='controller',sandbox=self.sandbox))
        self.assertEqual(len(fake.requests),1)
        self.assertNotIn('secret text',canonical_json(self.store.get(rid)))
        self.assertEqual(self.store.get(rid)['failure_code'],'RUNTIME_FAILED')

    def test_canonical_hash_ignores_key_order(self):
        value=payload()
        self.assertEqual(content_sha256(value),content_sha256(dict(reversed(list(value.items())))))
        self.assertEqual(parse_review(output(value),[]),value)

    def test_immutable_evidence_and_subject(self):
        _, started, rid=self.ready_review()
        self.judge(rid)
        for statement, args in [
            ('UPDATE review_context_snapshots SET projection_json=? WHERE review_id=?',('{}',rid)),
            ('DELETE FROM structured_review_results WHERE review_id=?',(rid,)),
            ('DELETE FROM judge_decisions WHERE review_id=?',(rid,)),
            ('UPDATE task_reviews SET worker_snapshot_id=? WHERE review_id=?',('wrong',rid)),
            ('UPDATE orchestration_attempts SET result_json=? WHERE attempt_id=?',('{}',started[1])),
            ('UPDATE orchestration_tasks SET result_json=? WHERE orchestration_task_id=?',('{}',started[0]['orchestration_task_id'])),
            ('UPDATE context_snapshots SET projection_json=? WHERE context_snapshot_id=?',('{}',started[3]['context_snapshot_id'])),
        ]:
            with self.subTest(statement=statement), self.connect() as c, self.assertRaises(sqlite3.IntegrityError):
                c.execute(statement,args)
        with self.assertRaises(ReviewError): self.store.complete(rid,payload('needs_fix'))

    def test_cross_project_evidence_rejected(self):
        _, _, rid=self.ready_review()
        value=payload(findings=[{'code':'BAD','severity':'error','message':'foreign evidence',
                                'evidence':['context_entry:another-project-id']}])
        fake=FakeRuntime([FakeRuntimeOutcome.success(output=output(value))])
        self.assertFalse(self.store.execute(rid,fake,owner='controller',sandbox=self.sandbox))
        self.assertEqual(self.store.get(rid)['failure_code'],'INVALID_OUTPUT')

    def test_bounds_and_strict_types(self):
        cases=[payload(schema_version=True),payload(assessment='APPROVE'),payload(summary='x'*4001),
               payload(findings=[{}]*33),payload(required_changes=['x']*33),payload(required_changes=['x'*2001]),
               payload(extra='field'),payload(findings=[{'code':'X','severity':'fatal','message':'m','evidence':[]}])]
        for value in cases:
            with self.subTest(value=str(value)[:80]),self.assertRaises(ReviewError): validate_review(value,[])
        duplicate=output(payload()).replace('"schema_version":1','"schema_version":1,"schema_version":1')
        with self.assertRaises(ReviewError): parse_review(duplicate,[])
        with self.assertRaises(ReviewError): parse_review(output(payload())*2,[])

    def test_concurrent_claim_dispatch_once(self):
        _, _, rid=self.ready_review()
        barrier=threading.Barrier(2)
        fake=FakeRuntime([FakeRuntimeOutcome.success(output=output(payload()))])
        def execute(index):
            barrier.wait(timeout=5)
            return ReviewStore(self.connect).execute(rid,fake,owner='controller-'+str(index),sandbox=self.sandbox)
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            results=list(executor.map(execute,[1,2]))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(len(fake.requests),1)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM judge_decisions').fetchone()[0],1)

    def test_restart_completed_review_and_decision_not_recreated(self):
        plan_id, started, rid=self.ready_review()
        self.judge(rid)
        for _ in range(3):
            store=ReviewStore(self.connect)
            self.assertEqual(store.reconcile(),0)
            with store.transaction() as c:
                self.assertEqual(store.request(c,started[0]['orchestration_task_id'],started[1]),rid)
            store.complete(rid,payload())
            store.accept(rid)
            ORCH.reconcile_task_graph()
        with self.connect() as c:
            for table in ('task_reviews','structured_review_results','judge_decisions'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM review_events WHERE event_kind='task_accepted'").fetchone()[0],1)

    def test_restart_running_review_is_terminal_interrupted(self):
        _, _, rid=self.ready_review()
        self.assertTrue(self.store.claim(rid,'old'))
        self.assertEqual(self.store.reconcile(),1)
        self.assertEqual(self.store.reconcile(),0)
        self.assertFalse(self.store.claim(rid,'new'))
        self.assertEqual(self.store.get(rid)['failure_code'],'INTERRUPTED')

    def test_pending_review_survives_restart(self):
        _, _, rid=self.ready_review()
        self.assertEqual(self.store.reconcile(),0)
        self.assertEqual([r['review_id'] for r in self.store.list(pending=True)],[rid])
        self.judge(rid)

    def test_native_fake_provider_review_execution(self):
        _, _, rid=self.ready_review()
        provider=FakeModelProvider([FakeModelProviderOutcome.success(output(payload()))])
        self.assertTrue(self.store.execute(rid,NativeRuntime(provider,'fixed-model'),owner='native',sandbox=self.sandbox))
        self.assertEqual(len(provider.requests),1)
        self.assertIn('worker_context',provider.requests[0].messages[0].content)
        self.assertEqual(self.store.get(rid)['disposition'],'PASS')

    def test_hermes_review_uses_runtime_request_contract(self):
        with self.connect() as c:
            c.execute("UPDATE roles SET runtime_kind='hermes' WHERE role_id='reviewer'")
        _, _, rid=self.ready_review()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'repo').symlink_to(graph.ROOT,target_is_directory=True)
            runtime=HermesRuntime(root,required_role=RuntimeRole.REVIEWER)
            with mock.patch.object(runtime,'execute',return_value=RuntimeResult(output(payload()))) as execute:
                self.assertTrue(self.store.execute(rid,runtime,owner='hermes',sandbox=self.sandbox))
            request=execute.call_args.args[0]
            self.assertEqual(request.role,RuntimeRole.REVIEWER)
            self.assertEqual(request.context,self.store.evidence(rid)["input"])

    def diamond_reviews(self,reject=False):
        plan_id=self.create(diamond=True)
        self.finish(self.start(plan_id,'a'))
        b=self.start(plan_id,'b'); c=self.start(plan_id,'c')
        ORCH.finish_task_success(b[0],b[1],{'output':'B'})
        ORCH.finish_task_success(c[0],c[1],{'output':'C'})
        self.executor.futures[-2].set_result('B');self.executor.futures[-1].set_result('C')
        reviews={r['task_id']:r['review_id'] for r in self.store.list()}
        rb=reviews[b[0]['orchestration_task_id']];rc=reviews[c[0]['orchestration_task_id']]
        self.judge(rb,'needs_fix' if reject else 'pass')
        if not reject: self.store.accept(rb)
        ORCH.refresh_plan_states(plan_id)
        self.assertEqual(self.states(plan_id)['d'],'PENDING')
        self.judge(rc);self.store.accept(rc)
        ORCH.refresh_plan_states(plan_id)
        if reject:
            self.assertEqual(self.states(plan_id),{'a':'COMPLETED','b':'BLOCKED','c':'COMPLETED','d':'PENDING'})
            for _ in range(3): ORCH.reconcile_task_graph()
            with self.connect() as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM orchestration_attempts').fetchone()[0],3)
        else:
            self.assertEqual(self.states(plan_id)['d'],'READY')
            self.finish(self.start(plan_id,'d'))
            ORCH.reconcile_task_graph()
            self.assertEqual(self.states(plan_id)['d'],'COMPLETED')
            with self.connect() as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM orchestration_attempts').fetchone()[0],4)

    def test_diamond_pass_waits_both_then_d_once(self): self.diamond_reviews()
    def test_diamond_rejection_keeps_independent_branch(self): self.diamond_reviews(reject=True)

    def test_no_review_policy_preserves_default(self):
        plan_id=self.create(required=False)
        self.finish(self.start(plan_id,'b'))
        self.assertEqual(self.states(plan_id),{'b':'COMPLETED','d':'READY'})
        self.assertEqual(self.store.list(),[])

    def test_direct_completion_cannot_bypass_review(self):
        _, started, _=self.ready_review()
        with self.connect() as c,self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE orchestration_tasks SET status='COMPLETED' WHERE orchestration_task_id=?",(started[0]['orchestration_task_id'],))

    def test_migration_sequence_and_integrity(self):
        self.ready_review()
        with self.connect() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT version FROM schema_migrations ORDER BY version')],list(range(1,32)))
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def production_subject(self, *, runtime_kind='native'):
        with self.connect() as c:
            c.execute('UPDATE roles SET runtime_kind=? WHERE role_id=\'reviewer\'',(runtime_kind,))
        plan_id=self.create(); started=self.start(plan_id,'b',run=True)
        task,attempt,assignment,snapshot=started
        run_id='run-'+attempt; execution_id='worker-execution-'+attempt
        with self.connect() as c:
            c.execute("UPDATE runs SET status='REVIEWING',branch_name='review-test',base_commit=?,result_commit=?,submitted_at=?,transaction_owner='owner' WHERE run_id=?",
                      ('a'*40,'b'*40,graph.NOW,run_id))
            c.execute('INSERT INTO project_locks VALUES (?,?,?,?,?)',('project-b',run_id,'owner',graph.NOW,graph.NOW))
            c.execute("INSERT INTO tasks(task_id,run_id,role,status,description,created_at) VALUES (?,?,'worker-native','COMPLETED','worker',?)",
                      ('worker-task-'+attempt,run_id,graph.NOW))
            c.execute('''INSERT INTO worker_executions(execution_id,task_id,run_id,role_id,source_profile,runtime_profile,
                outer_container_name,prompt_path,output_path,workspace_mode,network_enabled,cpu_limit,memory_mb,
                exit_code,result_json,created_at,finished_at,context_snapshot_id)
                VALUES (?,?,?,'worker-native','ops-worker-native',?,?,?,?,'write',0,1,512,0,?,?,?,?)''',
                (execution_id,'worker-task-'+attempt,run_id,execution_id,execution_id,execution_id+'.prompt',execution_id+'.output',
                 '{"output":"authoritative worker output"}',graph.NOW,graph.NOW,snapshot['context_snapshot_id']))
            c.execute('UPDATE orchestration_attempts SET worker_execution_id=? WHERE attempt_id=?',(execution_id,attempt))
        rid=self.finish(started)
        self.assertIsNotNone(rid)
        return plan_id,started,rid

    def production_review(self, *, kind='native', assessment='pass'):
        import os
        import subprocess
        from types import SimpleNamespace
        import test_agent_runtime as runtime_tests
        plan_id,started,rid=self.production_subject(runtime_kind=kind)
        installed=self.database.parent/'installed';installed.mkdir();(installed/'repo').symlink_to(graph.ROOT,target_is_directory=True)
        spec=importlib.util.spec_from_file_location('judge_reviewer_command',graph.SCRIPTS/'orchestra-reviewer.py')
        reviewer=importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ,{'ORCHESTRA_ROOT':str(installed)}): spec.loader.exec_module(reviewer)
        reviewer.DATABASE=self.database
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            assignment=reviewer.ASSIGNMENTS.create_assignment(c,run_id='run-'+started[1],
                orchestration_attempt_id=started[1],assignment_number=1,role_id='reviewer',assigned_by='controller')
        self.store.claim(rid,'controller')
        instruction=installed/'instruction.txt';instruction.write_text('Review the supplied evidence')
        repository=installed/'repository';worktree=installed/'worktree';clone=installed/'clone'
        for path in (repository,worktree,clone): path.mkdir()
        result=output(payload(assessment))
        provider=FakeModelProvider([FakeModelProviderOutcome.success(result)])
        runtime=NativeRuntime(provider,'fixed-model') if kind=='native' else HermesRuntime(installed,required_role=RuntimeRole.REVIEWER)
        requests=[]
        def hermes_transport(request):
            requests.append(request)
            # Exercise Hermes command mapping, with process transport deterministic.
            command=runtime.build_command(request)
            self.assertIn('ORCHESTRA_SANDBOX_READ_ONLY=true',command)
            self.assertIn('TERMINAL_DOCKER_NETWORK=false',command)
            return RuntimeResult(result)
        patches=[mock.patch('builtins.print'),mock.patch.object(reviewer,'connect',self.connect),
            mock.patch.object(reviewer.WORKER,'prepare_worker_environment',return_value=runtime_tests.oci_preparation('e')),
            mock.patch.object(reviewer,'verify_transaction',return_value=(repository,worktree,'b'*40)),
            mock.patch.object(reviewer,'git_references',return_value={'refs/heads/review-test':'b'*40}),
            mock.patch.object(reviewer,'git',side_effect=lambda path,*args:'b'*40 if args==('rev-parse','HEAD') else ''),
            mock.patch.object(reviewer,'prepare_review_clone',return_value=clone),
            mock.patch.object(reviewer,'precreate_reviewer_sandbox',return_value=('c'*64,{},subprocess.CompletedProcess([],0,'',''))),
            mock.patch.object(reviewer,'audit_reviewer_sandbox',return_value={'read_only':True}),
            mock.patch.object(reviewer,'nested_docker',return_value=subprocess.CompletedProcess([],0,'','')),
            mock.patch.object(reviewer,'remove_owned_reviewer_sandbox'),mock.patch.object(reviewer,'make_clone_writable')]
        if kind=='hermes':
            patches.extend([mock.patch.object(runtime,'execute',side_effect=hermes_transport),
                            mock.patch.object(runtime,'_locked_hermes_agent_image',return_value='example/hermes@sha256:'+'f'*64)])
        import contextlib
        with contextlib.ExitStack() as stack:
            for patch in patches:stack.enter_context(patch)
            reviewer.command_launch(SimpleNamespace(run='run-'+started[1],role='reviewer',assignment=assignment['assignment_id'],
                instruction_file=str(instruction),marker=MARKER,timeout=30,graph_review=rid),runtime=runtime)
        row=self.store.get(rid)
        self.assertEqual(row['status'],'COMPLETED')
        self.assertEqual(row['disposition'],assessment.upper())
        self.assertIsNotNone(row['legacy_execution_id'])
        with self.connect() as c:
            legacy=c.execute('SELECT * FROM reviewer_executions WHERE execution_id=?',(row['legacy_execution_id'],)).fetchone()
            self.assertEqual(legacy['runtime_kind'],kind)
            self.assertEqual(legacy['outer_container_name'],row['execution_id'])
            self.assertEqual(legacy['repository_unchanged'],1)
        self.assertEqual(self.store.evidence(rid)['worker_result'],{'output':'authoritative worker output'})
        return plan_id,started,rid

    def test_production_native_reviewer_persists_legacy_and_structured_authorities(self):
        self.production_review()

    def test_production_hermes_reviewer_mapping_and_persistence(self):
        self.production_review(kind='hermes')

    def test_production_judge_gates_integrator(self):
        _,started,rid=self.production_review(assessment='needs_fix')
        spec=importlib.util.spec_from_file_location('judge_integrator',graph.SCRIPTS/'orchestra-integrator.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with self.connect() as c,self.assertRaises(module.IntegrationError):
            module.required_judge_authority(c,'run-'+started[1],self.store.get(rid)['legacy_execution_id'])

    def test_reviewer_cannot_bind_foreign_legacy_execution(self):
        _,_,rid=self.ready_review()
        self.store.claim(rid,'controller')
        with self.assertRaises(ReviewError):self.store.complete(rid,payload(),legacy_execution_id='foreign-execution')
        self.assertIsNone(self.store.get(rid)['disposition'])

    def test_worker_execution_snapshot_ownership_enforced(self):
        _,started,rid=self.production_subject()
        self.assertEqual(self.store.get(rid)['worker_execution_id'],'worker-execution-'+started[1])
        with self.connect() as c,self.assertRaises(sqlite3.IntegrityError):
            c.execute('UPDATE worker_executions SET context_snapshot_id=NULL WHERE execution_id=?',('worker-execution-'+started[1],))

    def test_review_eligibility_rejects_running_worker(self):
        plan_id=self.create();started=self.start(plan_id,'b')
        with self.store.transaction() as c,self.assertRaises(Exception):
            self.store.request(c,started[0]['orchestration_task_id'],started[1])
        self.assertEqual(self.store.list(),[])

    def test_integration_claim_is_once_and_failure_does_not_accept(self):
        _,_,rid=self.ready_review()
        self.judge(rid)
        self.assertTrue(self.store.claim_integration(rid))
        self.assertFalse(self.store.claim_integration(rid))
        self.store.integration_failed(rid)
        self.assertFalse(self.store.claim_integration(rid))

    def test_cancellation_prevents_acceptance(self):
        plan_id,_,rid=self.ready_review()
        self.judge(rid)
        with self.connect() as c:c.execute("UPDATE objective_queue SET status='CANCEL_REQUESTED' WHERE plan_id=?",(plan_id,))
        self.assertFalse(self.store.accept(rid))
        self.assertEqual(self.states(plan_id)['d'],'PENDING')

    def test_legacy_recovery_does_not_consume_review_disposition(self):
        _,started,rid=self.production_subject()
        self.judge(rid,'needs_fix')
        spec=importlib.util.spec_from_file_location('judge_recovery',graph.SCRIPTS/'orchestra-recovery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with mock.patch.object(module,'connect',self.connect),mock.patch.object(module,'assess_run') as assess:
            result=module.recover_run(run_id='run-'+started[1],owner='supervisor',stale_seconds=30,force=False)
        self.assertEqual(result['outcome'],'NO_ACTION');assess.assert_not_called()

    def test_foreign_worker_snapshot_fails_closed_without_worker_retry(self):
        plan_id=self.create(diamond=True)
        self.finish(self.start(plan_id,'a'))
        b=self.start(plan_id,'b');c=self.start(plan_id,'c')
        with self.connect() as connection:
            connection.execute('UPDATE orchestration_attempts SET context_snapshot_id=? WHERE attempt_id=?',
                               (c[3]['context_snapshot_id'],b[1]))
        ORCH.finish_task_success(b[0],b[1],{'output':'durable result despite invalid provenance'})
        with self.connect() as connection:
            row=connection.execute('SELECT review_state,status FROM orchestration_tasks WHERE orchestration_task_id=?',
                                   (b[0]['orchestration_task_id'],)).fetchone()
            self.assertEqual(tuple(row),('FAILED','BLOCKED'))
            attempt=connection.execute('SELECT status,result_json FROM orchestration_attempts WHERE attempt_id=?',(b[1],)).fetchone()
            self.assertEqual(attempt[0],'COMPLETED');self.assertIn('durable result',attempt[1])
        self.assertEqual(self.store.list(),[])

    def test_existing_28_history_is_preserved_by_migration(self):
        database=self.database.parent/'upgrade.db'
        with sqlite3.connect(database) as c:
            c.execute('PRAGMA foreign_keys=ON')
            for migration in sorted((graph.ROOT/'migrations').glob('*.sql')):
                if int(migration.name[:3])<=28:c.executescript(migration.read_text())
            self._seed(c)
            c.execute("INSERT INTO runs(run_id,project_id,status,created_at) VALUES ('old-run','project-b','COMPLETED',?)",(graph.NOW,))
            c.execute("INSERT INTO review_results VALUES ('old-review','old-run','PASS','historical review','{}',?)",(graph.NOW,))
            c.execute("INSERT INTO orchestration_plans(plan_id,objective,source,status,max_parallel_tasks,plan_sha256,plan_json,created_at) VALUES ('old-plan','objective','TEST','COMPLETED',1,?,'{}',?)",('a'*64,graph.NOW))
            c.execute("INSERT INTO orchestration_tasks(orchestration_task_id,plan_id,task_key,kind,project_id,status,instruction,created_at) VALUES ('old-task','old-plan','old','NOOP','project-b','COMPLETED','old',?)",(graph.NOW,))
            before=list(c.execute('SELECT * FROM review_results'))
            c.executescript((graph.ROOT/'migrations/029_reviewer_judge.sql').read_text())
            self.assertEqual(list(c.execute('SELECT * FROM review_results')),before)
            self.assertEqual(c.execute('SELECT status,review_required,review_state FROM orchestration_tasks').fetchone(),('COMPLETED',0,'NONE'))
            self.assertEqual(c.execute('SELECT count(*) FROM task_reviews').fetchone()[0],0)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_judge_and_snapshot_replace_cannot_erase_history(self):
        _,_,rid=self.ready_review();self.judge(rid)
        with self.connect() as c:
            for table in ('review_context_snapshots','structured_review_results','judge_decisions'):
                row=dict(c.execute('SELECT * FROM '+table).fetchone())
                columns=','.join(row);placeholders=','.join('?' for _ in row)
                with self.subTest(table=table),self.assertRaises(sqlite3.IntegrityError):
                    c.execute(f'INSERT OR REPLACE INTO {table}({columns}) VALUES ({placeholders})',tuple(row.values()))

    def test_required_pipeline_stops_before_legacy_reviewer_and_integration(self):
        plan_id=self.create();started=self.start(plan_id,'b')
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ORCH,'RUNTIME',Path(directory)),\
                mock.patch.object(ORCH,'set_attempt_links'),mock.patch.object(ORCH,'run_json',side_effect=[
                    {'run_id':'pipeline-run'},{'execution_id':'worker-execution'},{'submitted':True}]) as command,\
                mock.patch.object(ORCH,'launch_reviewer_with_transport_retry') as legacy:
            result=ORCH.execute_pipeline(started[0],started[1],'graph-controller',
                {'command_timeout_seconds':30,'worker_timeout_seconds':30},started[3])
        self.assertEqual(command.call_count,3);legacy.assert_not_called()
        self.assertEqual(result['worker']['execution_id'],'worker-execution')
        self.assertNotIn('integration',result)

    def test_production_pass_integrates_once_then_releases_graph(self):
        plan_id,started,rid=self.production_review()
        count=[]
        def integrate(args,**kwargs):
            count.append(args)
            with self.connect() as c:c.execute("UPDATE runs SET status='COMPLETED' WHERE run_id=?",('run-'+started[1],))
            return {'integrated':True}
        with mock.patch.object(ORCH,'run_json',side_effect=integrate):
            for _ in range(3):ORCH.reconcile_required_reviews('controller',{'command_timeout_seconds':30})
        self.assertEqual(len(count),1)
        self.assertEqual(self.states(plan_id),{'b':'COMPLETED','d':'READY'})

    def test_production_human_approval_authorizes_same_review(self):
        _,started,rid=self.production_review(assessment='human_review')
        spec=importlib.util.spec_from_file_location('human_judge_integrator',graph.SCRIPTS/'orchestra-integrator.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        row=self.store.get(rid)
        with self.connect() as c,self.assertRaises(module.IntegrationError):
            module.required_judge_authority(c,'run-'+started[1],row['legacy_execution_id'])
        self.store.resolve_human(row['approval_id'],'APPROVE')
        with self.connect() as c:
            self.assertTrue(module.required_judge_authority(c,'run-'+started[1],row['legacy_execution_id']))
        self.assertEqual(self.store.get(rid)['disposition'],'HUMAN_REVIEW')

    def test_transport_logs_do_not_change_structured_result_hash(self):
        framed = 'Runtime starting\n' + output(payload()) + '\nRuntime stopped\n'
        self.assertEqual(content_sha256(parse_review(framed, [])),content_sha256(payload()))

    def test_integration_rechecks_judge_under_transaction(self):
        _,started,rid=self.production_review(assessment='needs_fix')
        spec=importlib.util.spec_from_file_location('locked_judge_integrator',graph.SCRIPTS/'orchestra-integrator.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with self.connect() as c:
            run=c.execute('SELECT * FROM runs WHERE run_id=?',('run-'+started[1],)).fetchone()
        original=module.required_judge_authority
        checked=[]
        def check(connection,*args):
            checked.append(connection.in_transaction)
            return original(connection,*args)
        with mock.patch.object(module,'connect',self.connect),mock.patch.object(module,'required_judge_authority',side_effect=check), \
                mock.patch.object(module,'git') as git,self.assertRaises(module.IntegrationError):
            module.integrate_approved(run=run,review={'review_execution_id':self.store.get(rid)['legacy_execution_id']},
                owner='owner',decision='APPROVE',verdict='PASS',evidence={'repository':'/tmp/repo','worktree':'/tmp/worktree'},transaction=mock.Mock())
        self.assertEqual(checked,[True]);git.assert_not_called()
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM integration_executions').fetchone()[0],0)
