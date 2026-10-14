"""Shared bounded, leased execution for local and cloud memory agents."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import json
import time
import uuid


class AgentLeaseLost(ValueError):
    pass


def stamp(moment=None):
    return (moment or datetime.now(UTC)).astimezone(UTC).isoformat(timespec='milliseconds')


def initialize_agent_columns(db, table):
    columns = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
    for name, definition in dict(agent_state='TEXT', pipeline='TEXT', lease_owner='TEXT',
            lease_until='TEXT', first_claimed_at='TEXT', task_deadline='TEXT',
            execution_attempts='INTEGER NOT NULL DEFAULT 0').items():
        if name not in columns:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


class AgentJobStore:
    """Identifiers come from store class constants, never model/tool input."""
    agent_task_seconds = 600
    agent_max_attempts = 3
    agent_retry_delays = (30, 120)
    agent_lease_seconds = 120

    def _claim_jobs(self, *, limit, now=None, kind=None, lease_seconds=None):
        moment = now or datetime.now(UTC)
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment)
        current = stamp(moment)
        lease = stamp(moment+timedelta(seconds=lease_seconds or self.agent_lease_seconds))
        table, key, retry = self.agent_job_table, self.agent_job_key, self.agent_retry_column
        kind_sql, kind_args = ('kind=? AND ', [kind]) if kind else ('', [])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # A dead worker never extends the original task deadline.
            db.execute(f"UPDATE {table} SET status='unresolved',last_error='agent_task_deadline',lease_owner=NULL,lease_until=NULL,{retry}=NULL,updated_at=? "
                       "WHERE status IN ('pending','processing','failed') AND COALESCE(pipeline,'agent')='agent' AND task_deadline<=?", (current, current))
            db.execute(f"UPDATE {table} SET status='unresolved',last_error='agent_attempts_exhausted',lease_owner=NULL,lease_until=NULL,{retry}=NULL,updated_at=? "
                       "WHERE pipeline='agent' AND execution_attempts>=? AND (status='failed' OR (status='processing' AND lease_until<=?))",
                       (current, self.agent_max_attempts, current))
            rows = db.execute(f"SELECT * FROM {table} WHERE {kind_sql}(status='pending' "
                f"OR (status='failed' AND attempts<? AND ({retry} IS NULL OR {retry}<=?)) "
                "OR (status='processing' AND lease_until<=?)) ORDER BY created_at LIMIT ?",
                (*kind_args, self.agent_max_attempts, current, current, max(1, int(limit)))).fetchall()
            claimed = []
            for row in rows:
                owner = str(uuid.uuid4())
                deadline = row['task_deadline'] or stamp(moment+timedelta(seconds=self.agent_task_seconds))
                job_lease = min(lease, deadline) if row['pipeline'] != 'legacy' else lease
                db.execute(f"UPDATE {table} SET status='processing',lease_owner=?,lease_until=?,"
                    f"first_claimed_at=COALESCE(first_claimed_at,?),task_deadline=?,updated_at=? WHERE {key}=?",
                    (owner, job_lease, current, deadline, current, row[key]))
                claimed.append({**dict(row), 'status':'processing', 'lease_owner':owner,
                    'lease_until':job_lease, 'first_claimed_at':row['first_claimed_at'] or current, 'task_deadline':deadline})
        return claimed

    def select_legacy_pipeline(self, ident, owner):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._assert_agent_owner(db, ident, owner)
            if row['pipeline'] not in (None, 'legacy'):
                raise ValueError('agent_pipeline_already_selected')
            db.execute(f"UPDATE {self.agent_job_table} SET pipeline='legacy' WHERE {self.agent_job_key}=?", (ident,))

    def expire_agent_job(self, ident, owner):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return db.execute(f"UPDATE {self.agent_job_table} SET status='unresolved',last_error='agent_task_deadline',lease_owner=NULL,lease_until=NULL,updated_at=? "
                f"WHERE {self.agent_job_key}=? AND lease_owner=? AND status='processing' AND task_deadline<=?", (stamp(), ident, owner, stamp())).rowcount

    def _assert_agent_owner(self, db, ident, owner, *, now=None):
        row = db.execute(f'SELECT * FROM {self.agent_job_table} WHERE {self.agent_job_key}=?', (ident,)).fetchone()
        current = stamp(now)
        if (not row or not owner or row['lease_owner'] != owner or row['status'] != 'processing'
                or not row['lease_until'] or row['lease_until'] <= current):
            raise AgentLeaseLost('agent_lease_lost')
        if row['pipeline'] != 'legacy' and (not row['task_deadline'] or row['task_deadline'] <= current):
            raise AgentLeaseLost('agent_task_deadline')
        return row

    def agent_guard(self, ident, owner):
        return lambda db: self._assert_agent_owner(db, ident, owner)

    def renew_agent_lease(self, ident, owner, *, now=None):
        moment = now or datetime.now(UTC)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = self._assert_agent_owner(db, ident, owner, now=moment)
            except AgentLeaseLost:
                return False
            until = min(stamp(moment+timedelta(seconds=self.agent_lease_seconds)), row['task_deadline'])
            db.execute(f'UPDATE {self.agent_job_table} SET lease_until=?,updated_at=? WHERE {self.agent_job_key}=?',
                       (until, stamp(moment), ident))
        return True

    def begin_agent_execution(self, ident, owner, *, configured=True):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._assert_agent_owner(db, ident, owner)
            if row['pipeline'] not in (None, 'agent'):
                raise ValueError('legacy_pipeline_already_selected')
            if configured and row['execution_attempts'] >= self.agent_max_attempts:
                raise ValueError('agent_attempts_exhausted')
            db.execute(f'UPDATE {self.agent_job_table} SET pipeline=\'agent\',execution_attempts=execution_attempts+?,updated_at=? WHERE {self.agent_job_key}=?',
                       (int(configured), stamp(), ident))

    def save_agent_state(self, ident, owner, state):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._assert_agent_owner(db, ident, owner)
            db.execute(f'UPDATE {self.agent_job_table} SET agent_state=?,updated_at=? WHERE {self.agent_job_key}=?',
                       (json.dumps(state, ensure_ascii=False), stamp(), ident))

    def resume_agent_configuration(self):
        with self.connect() as db:
            return db.execute(f"UPDATE {self.agent_job_table} SET status='pending',updated_at=? WHERE status='pending_review' "
                "AND pipeline='agent' AND last_error='memory_agent_not_configured'", (stamp(),)).rowcount

    def finish_agent_job(self, ident, owner, *, result=None, error=None, retryable=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._assert_agent_owner(db, ident, owner)
            next_at = None
            if error:
                if error == 'memory_agent_not_configured':
                    status = 'pending_review'
                elif retryable and row['execution_attempts'] < self.agent_max_attempts:
                    status = 'failed'
                    delay = self.agent_retry_delays[min(max(0, row['execution_attempts']-1), len(self.agent_retry_delays)-1)]
                    next_at = min(stamp(datetime.now(UTC)+timedelta(seconds=delay)), row['task_deadline'])
                else:
                    status = 'unresolved'
            elif result.failures:
                status, error = 'unresolved', 'protocol_or_commit_failure'
            elif result.pending_review:
                status, error = 'pending_review', 'waiting_clarification'
            elif result.rejected:
                status, error = 'refused', 'items_rejected'
            else:
                status = self.agent_success_status
            db.execute(f'UPDATE {self.agent_job_table} SET status=?,processing_result=?,last_error=?,{self.agent_retry_column}=?,attempts=execution_attempts,'
                f'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE {self.agent_job_key}=?',
                (status, json.dumps(result.as_dict(), ensure_ascii=False) if result else row['processing_result'],
                 error, next_at, stamp(), ident))
        return status


async def run_agent_job(store, job, packet, agent):
    """Lease heartbeat races execution; completed model judgments survive retry."""
    ident, owner = job[store.agent_job_key], job['lease_owner']
    deadline = datetime.fromisoformat(job['task_deadline']).timestamp()
    cached = json.loads(job.get('agent_state') or 'null')
    try:
        store.begin_agent_execution(ident, owner, configured=agent.provider is not None)
        if agent.provider is None:
            await agent.prepare(packet, cached=cached, persist=lambda state: store.save_agent_state(ident, owner, state))
            store.finish_agent_job(ident, owner, error='memory_agent_not_configured')
            return 'pending_review'
        async def execute():
            state = await agent.prepare(packet, cached=cached, task_deadline=deadline,
                persist=lambda state: store.save_agent_state(ident, owner, state))
            if state['status'] != 'prepared':
                error = state.get('error', 'agent_unresolved')
                return store.finish_agent_job(ident, owner, error=error,
                    retryable=error in ('agent_model_timeout', 'agent_budget_exhausted') or error.startswith('agent_model_failed:'))
            guard = store.agent_guard(ident, owner)
            result = await asyncio.to_thread(agent.commit, packet, state, commit_guard=guard)
            store.finish_agent_job(ident, owner, result=result)
            return 'unresolved' if result.failures else ('pending_review' if result.pending_review else ('refused' if result.rejected else 'stored'))
        async def heartbeat():
            while time.time() < deadline:
                await asyncio.sleep(min(30, store.agent_lease_seconds/3, max(.01, deadline-time.time())))
                if not await asyncio.to_thread(store.renew_agent_lease, ident, owner):
                    raise AgentLeaseLost('agent_lease_lost_or_deadline')
            raise AgentLeaseLost('agent_task_deadline')
        execution, renewal = asyncio.create_task(execute()), asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait((execution, renewal), return_when=asyncio.FIRST_COMPLETED,
                timeout=min(agent.budget.execution_seconds, max(0, deadline-time.time())))
            if not done:
                raise TimeoutError('agent_execution_timeout')
            return await (execution if execution in done else renewal)
        finally:
            for task in (execution, renewal):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
    except AgentLeaseLost:
        # Another owner or the final deadline now controls this job. No stale
        # attempt may overwrite its state or receipt.
        store.expire_agent_job(ident, owner)
        return 'unresolved'
    except Exception as exc:
        try:
            store.finish_agent_job(ident, owner, error='agent_execution_failed:'+type(exc).__name__, retryable=True)
        except AgentLeaseLost:
            store.expire_agent_job(ident, owner)
        return 'failed'
