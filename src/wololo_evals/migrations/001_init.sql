-- wololo_evals schema, complete and self-contained (04-wololo-evals SPEC S4).
-- Run as the migration owner role. The service connects as wololo_evals_app
-- (no bypassrls, no ownership); the queue worker connects as
-- wololo_evals_worker (its extra dequeue policies are at the bottom).
-- RLS is ENABLED and FORCED on every table with a default-deny policy keyed
-- on the inlined fail-closed resolver wololo_evals.current_tenant_id().

create schema if not exists wololo_evals;

-- ---------------------------------------------------------------------------
-- Fail-closed tenant resolver (SPEC S4.1). A signed JWT claim is
-- authoritative; a JWT WITHOUT the claim denies (returns null, every policy
-- fails closed). Only a no-JWT trusted worker/test path may fall back to the
-- app.tenant_id GUC.
-- ---------------------------------------------------------------------------
create or replace function wololo_evals.current_tenant_id() returns text
language plpgsql stable as $$
declare
  claims text := nullif(current_setting('request.jwt.claims', true), '');
begin
  if claims is not null then
    return claims::jsonb ->> 'tenant_id';   -- null if absent: DENY, no GUC fallback
  end if;
  return nullif(current_setting('app.tenant_id', true), '');
end;
$$;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------
create table wololo_evals.tenants (
  tenant_id                text primary key,
  name                     text not null,
  pass_bar                 real not null default 0.7,
  min_effect_pct           real not null default 5.0,
  n_trials                 int  not null default 5 check (n_trials >= 5),
  regression_threshold_pct real not null default 10.0,
  max_concurrent_runs      int  not null default 2,
  quota_runs_per_day       int  not null default 50,
  judge_model              text,
  judge_endpoint           text,
  metadata                 jsonb not null default '{}',
  created_at               timestamptz not null default now()
);

create table wololo_evals.candidates (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          text not null references wololo_evals.tenants,
  candidate_type     text not null check (candidate_type in ('skill','prompt','harness-config','model-config')),
  candidate_ref      text not null,
  proposed_by        text not null,   -- server-derived from the verified caller identity, never from the body
  baseline_ref       text not null,
  -- candidate payloads are tenant-proprietary IP: envelope-encrypted at rest
  -- (SPEC S10); there is NO plaintext payload column.
  payload_ciphertext bytea not null,
  payload_key_id     text  not null,
  payload_sha256     text  not null,
  rationale          text,
  target_eval_suite  text,
  submitted_at       timestamptz not null default now()
);

create table wololo_evals.runs (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        text not null references wololo_evals.tenants,
  candidate_id     uuid not null references wololo_evals.candidates,
  eval_suite       text not null check (eval_suite in ('skill-review','task-uplift','repo-eval','regression-replay')),
  status           text not null check (status in ('queued','running','passed','failed','errored')),
  suite_version    text not null,   -- content hash, pinned at creation (SPEC S9)
  rubric_version   text not null,   -- content hash, pinned at creation (SPEC S9)
  n_trials         int  not null,
  composite_score  real,            -- frozen at completion; the ONLY composite consumers read
  verdict_stats    jsonb,           -- {mean, stddev, ci_low, ci_high, wins, losses, n, low_confidence}
  eval_log_prefix  text,            -- 'evals/{tenant_id}/{run_id}/' blob prefix (SPEC S10)
  started_at       timestamptz,
  completed_at     timestamptz,
  created_at       timestamptz not null default now()
);

-- one-way status transition guard: queued->running->{passed,failed,errored}, never backward
create or replace function wololo_evals.check_run_status_transition() returns trigger as $$
declare
  rank_old int;
  rank_new int;
begin
  rank_old := array_position(array['queued','running','passed','failed','errored'], old.status);
  rank_new := array_position(array['queued','running','passed','failed','errored'], new.status);
  if rank_new < rank_old then
    raise exception 'eval run status cannot regress from % to %', old.status, new.status;
  end if;
  return new;
end;
$$ language plpgsql;

create trigger runs_status_forward before update of status on wololo_evals.runs
  for each row execute function wololo_evals.check_run_status_transition();

-- trials are first-class so n=1 verdicts are structurally impossible to hide
create table wololo_evals.trials (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      text not null references wololo_evals.tenants,
  run_id         uuid not null references wololo_evals.runs,
  trial_index    int  not null,
  arm            text not null check (arm in ('baseline','treatment','single')),
  status         text not null check (status in ('running','completed','errored')),
  model          text,
  harness        text,
  composite      real,
  eval_log_path  text,
  started_at     timestamptz,
  completed_at   timestamptz,
  unique (run_id, arm, trial_index)
);

create table wololo_evals.scores (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      text not null references wololo_evals.tenants,
  run_id         uuid not null references wololo_evals.runs,
  trial_index    int,             -- null = aggregate row over all trials
  dimension      text not null,
  value          real not null,
  weight         real not null default 1.0,
  max_value      real not null default 1.0,
  suite_version  text not null,   -- every score row pinned (SPEC S9)
  rubric_version text not null,
  judge          text,
  rationale      text,
  created_at     timestamptz not null default now()
);

create table wololo_evals.leaderboard (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        text not null references wololo_evals.tenants,
  model            text not null,
  harness          text not null,
  skill_ref        text not null,
  composite_score  real not null,
  n_trials         int  not null default 1,
  score_stddev     real,
  ci_low           real,
  ci_high          real,
  suite_version    text not null,
  rubric_version   text not null,   -- leaderboard rows only rank within one rubric_version
  run_id           uuid not null references wololo_evals.runs,
  computed_at      timestamptz not null default now(),
  unique (tenant_id, model, harness, skill_ref, rubric_version, run_id)
);

create table wololo_evals.canary_gates (
  id                     uuid primary key default gen_random_uuid(),
  tenant_id              text not null references wololo_evals.tenants,
  candidate_id           uuid not null references wololo_evals.candidates,
  run_id                 uuid not null references wololo_evals.runs,
  deploy_ref             text not null,
  gate_status            text not null check (gate_status in ('watching','passed','regressed','rolled-back')),
  baseline_composite     real not null,   -- copied from runs.composite_score at open, never re-derived
  rubric_version         text not null,
  regression_reason      text,
  -- exactly-once rollback across worker restarts (SPEC S11):
  rollback_key           text unique,     -- deploy_ref || ':' || run_id, persisted BEFORE the HTTP call
  rollback_intent_at     timestamptz,
  rollback_confirmed_at  timestamptz,
  opened_at              timestamptz not null default now(),
  closed_at              timestamptz
);

-- raw telemetry windows posted by wololo-deploy; wololo-evals computes the
-- live composite itself (SPEC S5.5), it never trusts a self-reported composite
create table wololo_evals.canary_telemetry (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      text not null references wololo_evals.tenants,
  gate_id        uuid not null references wololo_evals.canary_gates,
  window_id      text not null,
  metrics        jsonb not null,
  sample_count   int  not null,
  live_composite real,
  observed_at    timestamptz not null,
  received_at    timestamptz not null default now(),
  unique (gate_id, window_id)
);

-- durable job queue (SPEC S11)
create table wololo_evals.jobs (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        text not null references wololo_evals.tenants,
  kind             text not null check (kind in ('eval-run','canary-poll','retention-sweep')),
  payload          jsonb not null,
  idempotency_key  text not null unique,
  status           text not null default 'queued' check (status in ('queued','leased','done','failed','dead')),
  attempts         int  not null default 0,
  max_attempts     int  not null default 5,
  run_after        timestamptz not null default now(),
  lease_expires_at timestamptz,
  leased_by        text,
  created_at       timestamptz not null default now()
);
create index jobs_dequeue on wololo_evals.jobs (status, run_after) where status = 'queued';

create table wololo_evals.audit_chain_heads (
  tenant_id  text primary key references wololo_evals.tenants,
  last_seq   bigint not null,
  last_hash  text not null
);

create table wololo_evals.audit_events (
  id          bigserial primary key,
  tenant_id   text not null references wololo_evals.tenants,
  seq         bigint not null,   -- per-tenant monotonic sequence (per-tenant chains, not one global chain)
  actor       text not null,     -- from the verified token, never from the request body
  action      text not null,
  resource    text not null,     -- candidate id, run id, or deploy_ref, never the payload content
  decision    text not null check (decision in ('allowed','denied')),
  ts          timestamptz not null default now(),
  prev_hash   text not null,
  this_hash   text not null,     -- sha256(prev_hash || canonical-json(tenant_id,seq,actor,action,resource,decision,ts))
  unique (tenant_id, seq)
);

-- ---------------------------------------------------------------------------
-- Row-level security: enabled AND forced on every table, default-deny.
-- ---------------------------------------------------------------------------
do $$
declare
  t text;
begin
  foreach t in array array[
    'tenants','candidates','runs','trials','scores','leaderboard',
    'canary_gates','canary_telemetry','jobs','audit_chain_heads','audit_events'
  ] loop
    execute format('alter table wololo_evals.%I enable row level security', t);
    execute format('alter table wololo_evals.%I force row level security', t);
    execute format(
      'create policy tenant_isolation on wololo_evals.%I '
      'using (tenant_id = wololo_evals.current_tenant_id()) '
      'with check (tenant_id = wololo_evals.current_tenant_id())', t);
  end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- Roles. wololo_evals_app: the API service, no bypassrls, tenant-scoped only.
-- wololo_evals_worker: the queue worker; identical tenant scoping for all
-- work, PLUS a narrow global view of jobs + tenants so cross-tenant dequeue
-- fairness (SPEC S11 round-robin, per-tenant caps) is possible at all.
-- ponytail: two extra permissive policies instead of bypassrls; the worker
-- still cannot read any other tenant's runs/scores/candidates/blobs.
-- ---------------------------------------------------------------------------
do $$
begin
  if not exists (select from pg_roles where rolname = 'wololo_evals_app') then
    create role wololo_evals_app login password 'wololo_evals_app';
  end if;
  if not exists (select from pg_roles where rolname = 'wololo_evals_worker') then
    create role wololo_evals_worker login password 'wololo_evals_worker';
  end if;
end;
$$;

grant usage on schema wololo_evals to wololo_evals_app, wololo_evals_worker;
grant select, insert, update on all tables in schema wololo_evals
  to wololo_evals_app, wololo_evals_worker;
grant usage, select on all sequences in schema wololo_evals
  to wololo_evals_app, wololo_evals_worker;

create policy worker_jobs_global on wololo_evals.jobs
  to wololo_evals_worker using (true) with check (true);
create policy worker_tenants_read on wololo_evals.tenants
  for select to wololo_evals_worker using (true);
