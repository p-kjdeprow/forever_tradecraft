# Session runtime performance

`tools/session_performance.py` has two explicit offline modes. PNG replay measures
the existing public `PixelDecoder.decode` and pure executor planners against
caller-supplied local artifacts. Live-receipt evaluation reads caller-supplied
schema-2 JSON and checks it against a separately supplied, parent-reviewed
binding file and a separately supplied expected SHA-256. Neither mode captures
the desktop, sends input, starts the session daemon, invokes a model, contacts a
service, writes an accepted binding, or changes session behavior.

## PNG replay

Use an explicit lossless 256x256 status crop and, optionally, preserved timing
receipts for diagnostic-only inspection:

```bash
uv run python tools/session_performance.py \
  --input /private/preserved/status.png \
  --receipt /private/preserved/timing.json \
  --samples 100 \
  --warmups 5 \
  --output "$XDG_RUNTIME_DIR/crowley/runtime-performance.json"
```

Each input is read through a bounded descriptor, checked for PNG format and
dimensions, and hashed before every warmup and measured sample. Inputs are
capped at 16 files and 1 MiB per PNG; samples and warmups are capped at 1,000.
Warmups are labeled and excluded from summaries. `full_strict_decode` calls the
existing public `PixelDecoder.decode`; PNG loading and decode use separate
monotonic wall and process CPU intervals. Pure pointer, click, key, and camera
stages use bounded synthetic values and the existing planner APIs.

The decoder does not expose public pixel-extraction, cell-unpack, or frame /
JSON / schema substep boundaries. Those values are explicitly `null` with a
`no_public_boundary` explanation, and their work is included once in the full
strict-decode interval. Stage intervals can overlap, so do not add them. The
real public decoder tests retain Lua/CRS1 success and malformed pixel, cell,
CRC, sequence, boot, and schema refusal behavior.

Phase 1 `--receipt` files remain diagnostic-only, capped at 16 files and 1 MiB
each. An `accepted=true` field does not create an accepted binding. The report
retains safe receipt digests and known-field counts, while local decoder source
hashes identify only the code measured by replay.

The existing preserved offline baseline measured strict Python decode at
54.54 ms median and 60.65 ms p95; PNG loading was 0.64 ms median and 0.72 ms
p95. This shows measurable Python computation but does not establish it as the
live end-to-end bottleneck. A separate one-time compiled full-screen capture
was 1.674 s overall, including 357 ms in the GNOME Shell request and 347 ms
encoding and writing its PNG. Those portions are not a complete additive
latency attribution. The ordinary fifth sweep also spent most of its observed
time before dispatch, so its delay does not establish slow Python computation.

## Offline schema-2 receipt evaluation

Live mode is mutually exclusive with PNG replay. A parent must supply the
accepted binding file and its expected digest through a separate channel:

```bash
uv run python tools/session_performance.py \
  --live-receipt /private/run-01.json \
  --live-receipt /private/run-02.json \
  --accepted-binding /private/accepted-binding.json \
  --binding-sha256 PARENT_RECORDED_64_HEX_DIGEST \
  --output "$XDG_RUNTIME_DIR/crowley/runtime-performance-decision.json"
```

The evaluator accepts at most 64 distinct receipt inputs. Each binding and
receipt is read from a stable, regular, single-link descriptor with no symlink
following, before/after file identity checks, and a strict size cap (64 KiB for
the binding and 4 MiB per receipt). JSON rejects duplicate keys, non-finite
numbers, invalid UTF-8, and nesting deeper than 32. The binding's SHA-256 must
match the separately supplied parent digest before any receipt is evaluated.
Unknown fields, missing fields, changed files, duplicate receipts, and
duplicate run IDs are retained as refusals; they cannot be silently dropped to
reach a sample threshold.

The exact accepted-binding schema is:

```json
{
  "schema": "crowley.runtime-performance-accepted-binding",
  "schema_version": 1,
  "accepted_source_hashes": {
    "daemon.py": "<sha256>",
    "driver.py": "<sha256>",
    "cli.py": "<sha256>",
    "decoder.py": "<sha256>",
    "protocol.py": "<sha256>",
    "offline_harness.py": "<sha256>",
    "session_performance.py": "<sha256>"
  },
  "producer_binding": {
    "binary_sha256": "<sha256>",
    "protocol_branch": "ScreenshotArea"
  },
  "config_binding": {
    "raw_file_sha256": "<sha256>",
    "semantic_sha256": "<sha256>"
  }
}
```

The schema uses logical source names only. It contains no source paths, config
values, character or window identity, and no producer path. Hashes are lowercase
64-character SHA-256 hex strings. Source maps must contain exactly the seven
listed names. Extra fields, alternate producer branches, or missing hashes are
refused. Receipt-level `producer_binding` and `config_binding` use the same
exact digest-only shapes before comparison. A malformed object is represented
only by its refusal digest/reason; a valid shape with a different digest may
retain only the validated hashes and `ScreenshotArea` branch as a mismatch.

The schema-2 receipt root has exactly these fields:

```json
{
  "schema": "crowley.paused-region-performance-run",
  "schema_version": 2,
  "run_id": "<private opaque id>",
  "run_state": "passed | failed | incomplete",
  "generation": 0,
  "targets_monotonic": ["<exactly eight finite monotonic targets>"],
  "observer_source_hashes_before": {"<the exact seven source names>": "<sha256>"},
  "observer_source_hashes_after": {"<the exact seven source names>": "<sha256>"},
  "producer_binding": {"binary_sha256": "<sha256>", "protocol_branch": "ScreenshotArea"},
  "config_binding": {"raw_file_sha256": "<sha256>", "semantic_sha256": "<sha256>"},
  "attempts": ["<zero to eight begun attempt objects, in target order>"]
}
```

The eight scheduled targets are exactly one second apart. Actual outer starts
and actual sample completions are checked independently at 0.75–1.25 second
cadence. A declared `passed` run must satisfy both cadence checks and every
sample deadline. A declared `failed` run retains cadence and deadline
violations as timing diagnostics while its structurally valid measurements may
still contribute to the failed-run counterfactual gate. The report keeps
`timing_qualified` separate from measured pairs and records each timing
violation without changing the declared outcome. An attempt is
an exact object containing `index`, `target_monotonic`,
`deadline_monotonic`, `outer_started_monotonic`, `state`,
`status_returned_monotonic`, `owner_ipc_request_started_monotonic`, `owner`,
`owner_ipc_returned_monotonic`, `png_publication_end_monotonic`,
`png_load_started_monotonic`, `png_load_finished_monotonic`,
`strict_decode_started_monotonic`, `strict_decode_finished_monotonic`,
`sample_completed_monotonic`, `application_compute_cpu_seconds`,
`application_compute_cpu_method`, `partial_decode_wall_seconds`,
`partial_decode_cpu_seconds`, `png_sha256`, `generation`, `identity`, and
`failure`. Unreached boundaries and unavailable measurements are `null`, never
zero. A measured pair requires a successful public strict decode, a valid PNG
digest, application CPU measured with `time.process_time_ns`, a valid
same-attempt owner/source/config/producer binding, stable identity/boot with
advancing sequence, the matching generation and a unique owner nonce, all
model-call deltas known and zero, and successful cleanup. The pair retains its
outer-start-to-strict-decode-finish wall interval and application CPU even when the successful
decode missed its one-second deadline. Such a pair remains timing-unqualified.
A thrown decode may keep separate `partial_decode_*` diagnostics but cannot
form a full pair. A claimed strict-decode completion also requires every causal
client timestamp through strict-decode finish and every owner timestamp,
including owner lock/ACK/capture and both IPC/PNG boundaries, plus an owner
generation equal to the run generation. A declared completed attempt must also
have its later sample-completion timestamp. A failed or incomplete attempt may
end after a successful strict decode but before that marker; the decoded pair
and reached failure evidence remain available, with the sample-completion
field null. Other missing causal boundaries or a missing owner generation
refuse the receipt.

`owner` is either `null` when entry did not occur, or the exact object with
`owner_entered_monotonic`, `request_started_monotonic`,
`lock_acquired_monotonic`, `ack_sent_monotonic`, `capture_started_monotonic`,
`capture_returned_monotonic`, `response_ready_monotonic`, `receipt_nonce`,
`generation`, `source_binding`, `producer_binding`, `config_binding`,
`model_calls_before`, `model_calls_after`, `model_call_delta`, `cleanup`, and
`failure`. The owner failure is `null` when the owner reached a successful
response; the enclosing attempt can still fail a later client deadline. Each
model counter object has exactly `started`, `succeeded`, and
`failed`; each value may be a nonnegative integer or `null`. A known delta must
equal after minus before. A missing counter remains `null`, is never guessed as
zero, and disqualifies that attempt from a completed pair. Across snapshots,
`succeeded + failed` cannot exceed `started`; cumulative counts cannot decrease.
An increase between one attempt's after-snapshot and the next before-snapshot
is retained as an inter-sample delta and makes the run ineligible for pure
paused-capture evidence, even when both per-attempt deltas are zero. Any missing
or nonzero model delta also makes that run ineligible.

`cleanup` has exactly the boolean fields `capture_process_reaped`,
`attempt_retired`, `lease_held`, and `watchdog_healthy`, plus nonnegative
`active_process_count`, `active_process_group_count`, and
`observer_process_group_count`. A paired sample requires every boolean to be
true and all three counts to be zero. A well-formed cleanup failure is retained
with the attempt's timestamps, CPU diagnostic, cleanup fields, and fixed failure
reason, but the sample is not paired and the run is ineligible. Malformed or
missing cleanup structure is refused. `identity`, when present, has exactly a
private `fingerprint`, private `boot` string, and nonnegative `sequence`.
Identity, boot, generation consistency, advancing sequence, receipt nonce
uniqueness, config and producer digests, PNG digest, source stability, deadline,
and cleanup are validated for every decoded pair, including a decoded attempt
whose run outcome is `failed`; raw private identifiers are never emitted.

Each failure object has exactly `phase` and `reason_code`. `phase` is one of
`status`, `owner_ipc`, `owner_capture`, `png_publication`, `png_load`,
`strict_decode`, `deadline`, `source_binding`, `config_binding`,
`producer_binding`, `model_counter`, `cleanup`, or `process_termination`.
`reason_code` is one of `status_failed`, `owner_ipc_failed`,
`owner_capture_failed`, `png_publication_failed`, `png_load_failed`,
`strict_decode_failed`, `sample_deadline_missed`, `source_binding_drift`,
`config_binding_mismatch`, `producer_binding_mismatch`,
`model_call_delta_nonzero`, `model_call_counters_missing`, `cleanup_failed`,
`process_terminated`, or `unknown_required_stage`. The runner retains the eight
original targets, every begun attempt, reached timestamps, cleanup evidence,
and failure phase/reason;
it stops after the first qualification failure and does not retry. Unknown
fields are treated as an unsupported required stage. Client, owner, and capture
intervals are derived only from their observed endpoints. They overlap and are
never summed. The report explicitly gives external process CPU as `null` with
`external_process_cpu_unavailable`, and private decode substeps as `null` with
`no_public_boundary`.

The public decision has exactly the versioned keys `schema`, `schema_version`,
`accepted_source_hashes`, `producer_binding`, `config_binding`, `runs`,
`statistics`, `decision`, `reason_codes`, and `limitations`. It preserves every
receipt digest, run outcome, eight targets, attempt, decoded pair, failure,
source and input digests, original public timestamps, model-call snapshots and
deltas, cleanup evidence, timing qualification, and interval-availability count.
A structurally refused receipt keeps its digest and refusal reason; counts that
cannot be recovered, including attempts and interval availability, remain
`null`. Any refused/malformed receipt forces `insufficient_evidence` even when
the rest of the collection meets a threshold. The report omits raw paths,
nonces, identities, boot values, run IDs, errors, XIDs, PIDs, config values, and image
bytes. Each run's `eligible` field means its binding and evidence structure are
valid; a timing failure in a declared failed run remains visible and does not by
itself discard the whole run. A declared passed run with a deadline or cadence
violation is ineligible, though its valid decoded measurements remain visible.

The evaluator applies the fixed nearest-rank quantile
`sorted_samples[ceil(N*q)-1]`. The baseline requires at least 15 valid
independent fixed-eight-target runs and at least 120 valid decoded pairs across
them. The pooled CPU/ratio route additionally requires at least 15 eligible
runs with all eight decoded pairs; those runs supply the 80% repeatability
denominator. A failed outcome or timing miss does not by itself remove a
decoded pair. If baseline evidence exists but the 15-full-run primary gate is
unavailable and the alternate gate does not trigger, the result is
`insufficient_evidence`. For the pooled eligible set it computes
`Q95(application_compute_cpu) / Q95(end_to_end_wall)`; this is a ratio of
quantiles, not an average per-sample CPU fraction. A prototype plan is triggered
when pooled application CPU Q95 is at least 100 ms or that ratio is at least
0.20, and the relevant condition also holds in at least 80% of the eligible
full eight-pair runs. Alternatively, the counterfactual deadline gate needs
baseline evidence and at least three fully measured failures across three distinct eligible runs,
each with end-to-end wall over one second, application CPU of at least 100 ms,
and wall minus that same CPU interval below one second. Missing capture/I/O
stage evidence cannot trigger it. `plan_prototype` requests a scoped Rust plan;
it does not authorize a rewrite or deployment. Adequate evidence without a
repeatable trigger returns `defer_rewrite`; inadequate evidence or any source,
config, or producer binding mismatch returns `insufficient_evidence`.

The tool checks supplied receipts against the separately supplied binding hash.
That hash does not prove a human reviewed the source, and receipt JSON is not
independently authenticated as live capture. No qualifying real schema-2 inputs
are currently available, so fixture-based threshold decisions are tests only.
This report cannot establish a live bottleneck or authorize a Rust rewrite.

## Bounded Rust template matcher preparation

The retained WoW control benchmark measured the pure Python matcher at about
2.55 seconds for Construct and 2.52 seconds for Restore on a 220x84 region
with 98x27 RGB templates. The independent native preparation lives under
src/crowley/native/resources/native/template_matcher and is not integrated into the calibration helper. It
preserves the existing acceptance rule (SAD <= 5 * RGB byte count), the best
(SAD, x, y) ordering, and ambiguity relative to the best candidate.

The executable accepts one complete binary request on stdin and emits one JSON
object on stdout. Its 48-byte little-endian header is the ASCII magic
CRWTM001, followed by operation (1 for bounded SEARCH, 2 for exact SCORE_AT),
a zero reserved field, frame and template dimensions, and an absolute region
(x, y, width, height). Full row-major RGB8 frame bytes then template bytes
follow, with no trailing bytes. Dimensions, input size, comparison count,
region geometry, and all arithmetic are bounded before allocation or scoring;
malformed requests exit nonzero without a JSON result. SEARCH can return
matched, rejected, or ambiguous; SCORE_AT returns matched or rejected. Results
contain exact integer sad, byte count, and absolute [x, y, width, height] box
values.

`crowley-native build matcher` performs two independent offline release
builds with the pinned x86_64 toolchain, fixed release flags, and remapped
source paths. It requires byte-identical outputs before atomically publishing
the versioned executable and digest manifest into a caller-supplied private
directory. Existing publications are rebuilt and compared independently; an
adjacent manifest cannot authenticate a changed executable. Python remains
responsible for image identity, template and input verification, ownership,
receipts, and any later helper integration.

The public runner's retained-frame reproduction used `samples=3` and
`warmups=1` with binary
`75b18bd04cc3a9248f99ecdca3a1cdebe6e0d89c4d33eaebd600788177bcd80c`. Median
preparation and serialization was 63.30 ms. Construct and Restore SEARCH were
40.55 ms and 40.58 ms. The corresponding own-box SCORE_AT calls were 7.53 ms
and 7.56 ms; the rival-box SCORE_AT checks were 8.33 ms and 7.39 ms. The
exclusive composed shape was 49.39 ms and the dual-positive composed shape was
81.89 ms. SEARCH, SCORE_AT, and composed intervals include process startup,
full stdin/stdout transfer, native scoring, and JSON parsing; preparation is
reported separately. These retained-frame timings are computation evidence
only and do not qualify fresh desktop capture, input ownership, scan cadence,
or helper integration.

The archived `.archive/executor-review/benchmark_template_matcher.py` runner takes explicit
--binary, --frame, --construct, --restore, and --region arguments. It verifies
the private publication's binary digest and current native source pins, retains
only source and input digests in its JSON report, checks exact Python reference
parity for every status/SAD/count/box and composed call, and separates image
preparation from complete native invocation intervals. Its invocation timing
fields explicitly include native scoring alongside process startup, IPC, and
JSON parsing.
