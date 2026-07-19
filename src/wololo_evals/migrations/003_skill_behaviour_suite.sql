-- Register the 5th eval suite, skill-behaviour (wave-2 SPEC M4), in the
-- runs.eval_suite CHECK constraint. 001_init.sql pins the suite set as a
-- hard-coded enum on the column check; a new suite is therefore a schema
-- migration, not just a code change -- without this the initial insert into
-- runs at create_run time fails the CHECK before the worker ever dispatches.
-- (Numbered 003 because 002 is already taken by 002_run_smoke_flag.sql; it
-- sorts and applies after 001, which is all that matters.)
alter table wololo_evals.runs
  drop constraint if exists runs_eval_suite_check;
alter table wololo_evals.runs
  add constraint runs_eval_suite_check
  check (eval_suite in (
    'skill-review','task-uplift','repo-eval','regression-replay','skill-behaviour'
  ));
