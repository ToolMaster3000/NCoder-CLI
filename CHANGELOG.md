# Changelog

## v1.8.0 — inline verification badge

- **Inline confidence/verification badge on the final answer**: a small
  trailing line (e.g. `[complex tier · full verification · revised]` or
  `[simple tier · no tools used]`) showing exactly how much scaffolding
  actually ran on that specific answer — complexity tier, verification
  depth applied, and whether self-critique or whole-answer verification
  actually changed it — instead of needing to scroll back through the
  phase log to piece that together. Built entirely from state already
  tracked (verification depth, tools_ran, whether either check revised
  the answer), not a fabricated confidence score — this project doesn't
  compute a single top-level confidence for a final answer the way
  `pop_subtask` does per subtask, so the badge only reports what's
  actually known. Verified all three distinct cases render the exact
  expected text (no tools used / unchanged / revised).

## v1.7.0 — live todo checklist, startup capability summary

- **Live `todo.md` checklist panel during `/auto`**: replaces the
  previous numeric "N/M remaining" count with an actual rendered
  checklist (`[x]`/`[ ]` per item, checked items shown dimmed/struck)
  after every auto-turn, and again when the task completes with
  everything checked off. Uses plain ASCII brackets rather than Unicode
  ballot-box glyphs — this project has already hit real Android-font
  rendering risk with fancier Unicode elsewhere (the banner's
  block-drawing characters), so a checklist takes on none of that risk
  for a purely cosmetic gain. A new `parse_todo_ordered` helper
  preserves original item sequence with inline checked state, since the
  existing `parse_todo`'s split checked/unchecked lists lose ordering.
- **Startup capability summary**: a compact panel shown once at launch
  (skipped during `--selftest`) — detected RAM and the context-size tier
  it maps to (mirroring `setup.sh`'s own tiering, purely for display),
  Termux:API availability, how many playbooks are cached from prior
  successful tasks, and the active complexity budgets. Assembled
  entirely from state the harness already collects but never displayed
  together in one place.

## v1.6.0 — adaptive reflection, playbook-informed classification, tiered verification

- **Adaptive reflection scheduling**: previously fired on a flat
  every-3-rounds clock regardless of how the task was going. Now pulled
  forward to the very next round whenever an existing risk signal fires
  (a bounced subtask verification, an escalated tool failure, a detected
  contradiction), then resets to the base cadence after reflecting.
  Verified directly: an escalation at round 1 pulled reflection forward
  to round 2 instead of waiting for the fixed round-3 schedule.
- **Playbook-informed complexity classification**: `classify_task_complexity`
  now checks the playbook cache first (via simple keyword/Jaccard
  overlap, no model call) — a closely-matching prior goal reuses its
  observed tier instead of paying for a fresh classification call every
  time. Verified the classifier call is genuinely skipped on a strong
  match (the test mock asserts it's never invoked).
- **Tiered verification depth**: replaces the binary skip/run-both
  switch from v1.4.0 with three levels — `simple` skips both checks,
  `moderate` runs self-critique only, `complex`/`autonomous` run both
  self-critique and whole-answer verification. Verified all four tiers
  produce the exact expected number of verification calls.
- **Cycle detection extended to fact-ledger name collisions**: if a
  subtask name is reused (an earlier run under that name recorded a
  fact, and the same name is later reused for a new, still-pending
  subtask), querying that fact now surfaces a warning about the
  ambiguity — detect-and-warn, not detect-and-block, since this can't be
  proven to matter for any specific query the way an explicit
  `depends_on` cycle can be proven circular.
- **Bug fix, found while testing the above**: when a subtask was left
  open at the end of the round loop (round budget exhausted, or the
  loop ended for any other reason without an explicit `pop_subtask`),
  the fold-back to the parent task previously discarded everything the
  subtask had accumulated — tool results, warnings, everything except a
  generic placeholder note. Both fold-back sites (mid-loop budget
  cutoff, end-of-function catch-all) now preserve the subtask's actual
  messages instead of silently dropping them. Verified directly: an
  implicit-cycle warning generated inside an intentionally-unpopped
  subtask now correctly survives into the final transcript.

## v1.5.0 — model-informed tuning, following a recalibration

Prompted by actually reading Nanbeige4.1-3B's model card in detail:
it substantially outperforms much larger models (Qwen3-32B,
Qwen3-30B-A3B) on code/math/tool-use/agentic benchmarks, and is
specifically trained to sustain 500+ rounds of coherent tool
invocation and long single-pass reasoning (documented recommended
max_new_tokens: 131072). Earlier framing in this project underweighted
this — it's not a generically weak 3B model, it's specifically
post-trained for exactly this project's domain. It still trails
200B+-class models on the hardest agentic benchmarks (Browse-Comp,
GAIA), so that ceiling framing hasn't changed — but several harness
defaults were more conservative than the model's demonstrated
capability warranted.

- **Chat template sanity check**: `--selftest` now checks for raw
  special-token leakage (`<|im_start|>`, `[INST]`, etc.) in responses —
  a visible symptom of a wrong/generic chat template being applied by
  llama.cpp instead of the model's actual one. Can't verify an exact
  template match without the literal Jinja string, but catches the most
  common failure signature.
- **Merged plan+decision call**: one grammar-constrained call now
  produces both the plan and the tool-call decision, replacing two
  separate calls. Measured directly: a 2-round task dropped from what
  would have been 4 plan/decision calls to 2 combined calls. Also
  better matches the model's trained strength at coherent single-pass
  reasoning rather than fragmenting it across calls.
- **Raised round/time budgets**: simple (2→3 rounds), moderate (6→12),
  complex (10→30), autonomous (25→60), all with proportionally raised
  time budgets. Not scaled to match the model's 500-round benchmark
  directly (that used a different specialized framework), but
  meaningfully higher given demonstrated capability plus the merged
  call halving per-round overhead. Time budget remains the actual
  phone-side constraint (battery/thermal), not round count.
- **Model-recommended sampling parameters**: `top_p=0.95`,
  `repeat_penalty=1.0` now set explicitly on every call (previously left
  at llama.cpp's generic defaults), and the default `--temperature`
  aligned to the model's own recommended 0.6.
- **RAM-tiered context sizing — built to keep the 4GB minimum
  unchanged**: the 4GB target device keeps *exactly* the context size
  already proven safe (6144) — zero risk introduced. 6GB+ devices get
  10240, 8GB+ devices get 16384, reasoned as a proportional scale-up
  from the same validated KV-cache configuration, not an arbitrary
  bump. Verified the tiering picks correct values across the realistic
  4GB–12GB device range.
- **Complexity-gated verification scaffolding**: self-critique and
  whole-answer verification are now skipped entirely for simple-tier
  turns — trusting the model's own strong native tool-use judgment for
  straightforward cases rather than paying scaffolding cost uniformly.
  `/auto` always keeps full verification regardless, since unattended
  runs are exactly where it matters most.

## v1.4.0 — structured facts, cycle safety, whole-answer verification

- **Structured fact ledger** (`record_fact`/`query_facts`): a queryable
  JSON store for discrete facts, distinct from `invariants.md`'s
  always-reinjected prose — a later subtask can look something up by
  keyword instead of re-deriving it or hoping it's still in context.
- **Subtask dependency cycle detection**: `push_subtask`'s `depends_on`
  previously had no check for circular dependencies (A waits on B, B
  waits on A) — this would have silently deadlocked both forever,
  burning the whole round/time budget with no progress and no error.
  Now detected and rejected before queueing, verified against both
  direct and transitive cycles, with legitimate shared dependencies
  correctly still allowed.
- **Whole-answer re-verification**: a new check distinct from
  self-critique — self-critique checks the answer against tool results;
  this checks the *combined final answer* against the *original goal*
  directly, catching the case where every subtask was individually
  correct but the combined answer still misses part of what was asked.
- **Decomposability pre-check**: `/auto` now runs one cheap check before
  its multi-turn loop even starts — is this goal concrete enough to
  break into a todo list, or too vague? Avoids burning several
  autonomous turns discovering mid-task that the goal needed
  clarification from the start.
- **Real `todo.md` checklist parsing**: replaced a fragile `"- [ ]"`
  substring check (which `/auto`'s continue/stop decision depended on
  entirely) with a proper parser tolerant of different bullet
  characters, indentation, and checked-mark case — verified against
  malformed/non-checklist text producing no false positives.
- **Reusable playbook cache**: a successful `/auto` run's tool-usage
  sequence is saved to the workspace and retrievable via the existing
  `search_notes` tool for structurally similar future tasks — no new
  tool surface added, just another indexed workspace file.

## v1.3.0 — phase-aware status, buffered rendering, notifications

- **Buffered streaming render**: the final answer's markdown was being
  re-rendered on every single streamed token (O(n²) total for a long
  reply) — now buffers until a word boundary or ~100ms, whichever comes
  first. Measured directly: 72 characters streamed one-at-a-time dropped
  from 72 render calls to 15, with byte-identical final content.
- **Phase-specific status indicators**: the plan, decision, reflection,
  tool-dispatch, verification-check, contradiction-check, and
  self-critique calls previously showed *no visual indicator at all*
  while blocking on the network — only the final answer had a spinner.
  Every phase now gets a distinctly colored spinner (reusing the
  existing log-color palette) plus a small persistent footer showing
  round/budget progress and complexity tier.
- **Live per-tool status during concurrent dispatch**: batched
  `web_search`/`http_request` calls now show live ✓/… status per tool as
  each completes, instead of one silent line during a blocking wait for
  the whole batch. Verified directly that result ordering stays exactly
  correct regardless of which tool actually finishes first.
- **Completion notifications**: `/auto` finishing (successfully, via
  safety cap, or via `request_user_input`) now fires a Termux
  notification and a best-effort vibration — a phone is more likely than
  a desktop terminal to be put down mid-task. Degrades silently if
  Termux:API isn't installed.

## v1.2.0 — verification-gated subtasks & contradiction detection

- **Verification gate**: `pop_subtask` now bounces once (not blocked
  forever) if the subtask's goal involves something checkable (code,
  tests, facts, calculations) and no `verified_by` was given — forces an
  actual check before completion is accepted, rather than trusting the
  model's own assertion that it's done.
- **Structured intermediate results**: `pop_subtask` accepts an optional
  `structured_result` JSON field, preferred over prose for precision
  when a later subtask consumes the result rather than just reads it.
  Falls back to raw text if the JSON doesn't parse, rather than
  discarding it.
- **Confidence tagging**: every `pop_subtask` now requires a
  low/medium/high confidence level, carried through to dependency
  injection and the parent transcript.
- **Contradiction detection**: when a subtask completes and at least one
  other has already finished, a cheap check compares the new result
  against prior ones and flags direct conflicts before they get baked
  into a combined answer.
- **Strategy-tracked failure escalation**: extends the existing
  failure-escalation nudge to track which tools have already been tried
  and failed. When a second distinct tool also fails repeatedly, the
  model is told plainly that multiple approaches have been exhausted and
  pointed toward `request_user_input` instead of continuing to retry.

## v1.1.0 — color-coded orchestration logs

- Log lines during tool orchestration are now color-coded by category
  instead of uniform dim gray, so scanning a long or `/auto` run is
  easier: purple for plan text, teal/green for tool actions and auto-turn
  progress, amber for reflection/self-check notes, red for stalls/
  failures/safety caps/`request_user_input`, dim for low-priority
  bookkeeping (classification, compaction).
- Added a visible line for plain sequential and concurrent tool dispatch,
  which previously logged nothing to the console (only to the
  scratchpad) — every tool call is now visible as it happens, not just
  the subtask/request_user_input special cases.

## v1.0.0 — first stable release

The complete feature set as of this release, grouped by area.

### Core runtime
- Idempotent `setup.sh`: installs Termux packages, builds `llama.cpp`
  pinned to a verified commit, downloads and SHA256-verifies the model,
  installs a Termux autostart hook
- `--selftest`, `--stop`, `--version`, `--force-rebuild`, `--setup-only`,
  `--run-only`, `--no-autostart`, `--disable-autostart` flags
- Stale-server detection and recovery on relaunch; log rotation at 5MB
- Hardware-aware thread tuning (performance-core detection via
  `/proc/cpuinfo`), flash attention, quantized KV cache, `cache_prompt`

### Interface
- Claude Code-style boxed banner: mascot, gradient wordmark, complexity/
  version readout — sized and color-verified across narrow (34-col) to
  wide terminals
- Streaming markdown responses with a live spinner
- `/help /reset /system /save /load /sessions /stats /regenerate /resume
  /undo /auto` commands

### Tools (model decides autonomously, every turn, when to use them)
- `web_search`, `http_request` (arbitrary method/headers/body)
- `read_file` / `write_file` / `list_directory` — sandboxed to
  `workspace/`, path-traversal-proof
- `run_python` — subprocess-sandboxed, timeout-capped
- `search_notes` — keyword/TF-IDF-ranked search over workspace files
- `termux_api` — clipboard + notifications via Termux:API
- `push_subtask` / `pop_subtask` — narrowed-context nested work, with
  optional `depends_on` for dependency-ordered scheduling
- `request_user_input` — explicit signal for genuine blockers, used
  sparingly by design (prefer stated assumptions over asking)

### Reliability pipeline
- Plan → GBNF-grammar-constrained tool decision (batched, up to 3 calls)
  → execute → self-critique
- Stall detection (identical repeated calls) and failure escalation
  (repeated failures on one tool across varying arguments)
- Complexity-scaled round/time budgets (simple/moderate/complex/
  autonomous), classified per turn
- Scratchpad (full detail on disk, short previews in live context),
  pinned invariants (compaction-proof facts), `todo.md` checklist
- Context compaction at ~70% usage; mid-task reflection every 3 rounds
- Diff-based checkpointing with automatic collapse past 5 diffs;
  `/resume` and `/undo`
- Concurrent dispatch of independent `web_search`/`http_request` calls
  within a batch

### Autonomy
- `/auto <goal>`: multi-turn autonomous execution against a `todo.md`
  checklist, using a reserved 25-round/900s budget tier, bounded by an
  8-turn safety cap, stopping early if `request_user_input` fires

### Known, stated limitations
See the README's [Known Limitations](README.md#known-limitations)
section — notably: `http_request` headers are grammar-simplified to a
string, `search_notes` is keyword- not semantic-based, subtask context
isn't separately checkpointed, and tool-calling reliability is bounded
by the underlying 3B model's capability regardless of orchestration.
