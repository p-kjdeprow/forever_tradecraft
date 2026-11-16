# Host and operation coordination

Implementation complete. Temporary worker scopes below are historical; see
RESTRUCTURE_EXECUTION.md for final review and verification, and AUTOMATION.md for operation.


Use the existing local operation runner, its private durable ledger, and the shared GUI
lease. Do not add a request spool, socket server, new queue, or second supervisor. Agents
invoke crowley-automation directly; the host composes its public maintenance guard. This
concrete guard replaces the earlier tentative idea of dispatching jobs inside the host tick.
There is one actual GUI owner while a batch runs and no maintenance behind uncertain effects.

Automation runtime exposes maintenance_blocker(ledger_path=None)->str|None. Missing ledger
means no automation work. Existing ledger must validate permissions/schema. recover_if_idle()
returns None when the runner lock is held: block as automation_running. It recovers interrupted
dispatches under the runner lock; any uncertain effect blocks as automation_uncertain. Corrupt
or unreadable state blocks loudly as automation_state_invalid. No host implementation import
in automation, and no reading host internal files from the automation package.

Host calls this guard before policy maintenance and again inside the held GUI lease immediately
before termination, activation or key input. A race where a runner starts after the first check
is covered by the second check; if host already owns the GUI, runner cannot dispatch. Never
hold the host GUI lease while starting a launcher child that acquires that same lease.
Host observation also derives automation_busy from native operation/ownership/pending receipt,
so a native in-flight operation without a Python ledger still blocks maintenance. Missing or
stale facts follow existing fail-closed frame rules. Status reports hold reason without specs,
mail headers or chat text. Add no new durable host state or workflow states.

Native AH ownership remains separate. Keep a known-safe market grant across successive actions
of the same exact batch to avoid a due scanner timer starting a new full sweep between posts.
Bind the held batch ID and expiry; different batches cannot inherit it. Known final cleanup
clears/releases explicitly, expiry releases only before/after conclusively completed effects,
and uncertain native effects never auto-release. A sealed Python movement/interaction stage
can be replaced only by the same batch after bounded inputs finish; no protected call occurs
from begin/stage. Native scanner suspension waits for existing scan work and cancels new timers.
Standalone clients without the host bridge use explicit standalone mode; combined mode refuses
missing bridge. Do not query owned auctions before the grant is quiescent.

Verification must cover runner active before and after host policy decision, lease conflict,
process crash after dispatch, unknown native receipt, invalid ledger, scanner timer already due,
same-batch successive posts, different-batch grant rejection, and known-safe release/resume.
Parent assigns exact file ownership after the host cutover loop reports its initial checks.
