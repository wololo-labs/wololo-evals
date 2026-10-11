-- The audit hash chain only means something if the roles that write to it
-- cannot rewrite it. 001 granted update on every table to the app and worker
-- roles, audit_events included, so a compromised service could edit an event
-- and then recompute a chain that still verified. Events are append-only for
-- those roles; the head row stays updatable because appending moves it.
revoke update, delete, truncate on wololo_evals.audit_events
  from wololo_evals_app, wololo_evals_worker;
