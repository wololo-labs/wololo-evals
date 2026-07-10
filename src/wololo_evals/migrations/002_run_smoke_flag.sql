-- Smoke runs (SPEC S6.2): may bypass the n_trials floor and the all-stages-
-- live harness manifest requirement, but are NEVER verdict-bearing: the
-- leaderboard skips them and the canary gate (wave 2) must reject them at
-- open. The flag is a run-row fact so every consumer can see it.
alter table wololo_evals.runs
  add column if not exists smoke boolean not null default false;
