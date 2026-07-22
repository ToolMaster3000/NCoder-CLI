# NCoder-CLI

**v1.8.0** · [Changelog](CHANGELOG.md)

A local, on-device chat CLI for Android/Termux running
[Nanbeige4.1-3B-heretic](https://huggingface.co/heretic-org/Nanbeige4.1-3B-heretic)
via `llama.cpp`, with a grammar-constrained agentic tool-calling loop
(web search, arbitrary HTTP requests, sandboxed file access, Python
execution, keyword notes search, and Termux:API integration), built for
4GB+ RAM Android devices (Xiaomi, Samsung, Huawei, and similar).

> **Before filing a "reliability" issue, read this**: this runs a
> 3B-parameter model on a phone CPU. Throughput on a 4GB-RAM mid-range
> device is roughly **3–8 tokens/second**, and it's not comparable to a
> 200B+ class cloud model on the hardest agentic benchmarks (Browse-Comp,
> GAIA). That said, Nanbeige4.1-3B is specifically post-trained for
> code/math/tool-use/agentic reasoning and substantially outperforms much
> larger models (Qwen3-32B, Qwen3-30B-A3B) on most of its benchmarked
> tasks — it is not a generically weak small model, and several of this
> project's defaults (round budgets, call structure) are tuned with that
> in mind rather than assuming minimal capability. See
> [Known Limitations](#known-limitations) before assuming something is
> broken.

---

## Table of contents

- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [How it works](#how-it-works)
- [Setup script reference](#setup-script-reference)
- [In-CLI command reference](#in-cli-command-reference)
- [Tools available to the model](#tools-available-to-the-model)
- [Long-context & autonomous-task reliability](#long-context--autonomous-task-reliability)
- [TUI notes](#tui-notes)
- [Configuration](#configuration)
- [Directory layout](#directory-layout)
- [Security & privacy](#security--privacy)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Uninstalling](#uninstalling)

---

## Requirements

| | |
|---|---|
| Device | Android phone, **4GB+ RAM** recommended (tested conceptually against Snapdragon 6xx/7-series, Helio G-series, Exynos 12xx-class chipsets) |
| Runtime | [Termux](https://termux.dev) — the F-Droid build, **not** the abandoned Play Store version |
| Storage | ~6GB free (model weights + build artifacts) |
| Optional | [Termux:API](https://wiki.termux.com/wiki/Termux:API) app + package, only needed for the clipboard/notification tool |

## Quickstart

```bash
pkg install git                          # if not already present
git clone <this-repo> ~/ncoder-cli-src   # or copy setup.sh + nanbeige_cli.py over manually
cd ~/ncoder-cli-src
bash setup.sh
```

Before your first run, open `setup.sh` and confirm `MODEL_FILE` matches
an actual filename on the model's
[Hugging Face Files tab](https://huggingface.co/heretic-org/Nanbeige4.1-3B-heretic/tree/main) —
GGUF quantization filenames vary by uploader and this must match exactly
or the download step will fail.

The first run will, in order:

1. Install required Termux packages
2. Build `llama.cpp` from a pinned, known-working commit (not a moving target)
3. Download the model and verify its SHA256 checksum against Hugging Face's API
4. Print device-specific battery-whitelist instructions (see [Troubleshooting](#troubleshooting))
5. Install an autostart hook so NCoder launches automatically next time you open Termux
6. Start the local model server and drop you into the chat interface

Every step is idempotent — re-running `bash setup.sh` at any point checks
current state and only does what's missing.

## How it works

```
+-----------------------------------------------------------+
|  Termux (Android)                                          |
|                                                              |
|  +------------+   HTTP (localhost)   +-----------------+    |
|  | nanbeige_  |<--------------------->|  llama-server    |   |
|  | cli.py     |                       |  (llama.cpp)     |   |
|  | (this repo)|                       |  running the     |   |
|  |            |                       |  GGUF model      |   |
|  +-----+------+                       +-----------------+    |
|        |                                                     |
|        v                                                     |
|  ~/ncoder-cli/workspace/   (sandboxed - see Security below)  |
|    +- scratchpad.md      (full tool-output detail)           |
|    +- invariants.md      (pinned facts, compaction-proof)    |
|    +- todo.md            (model-maintained checklist)        |
|    +- task_state.json    (diff-based checkpoint)             |
+-----------------------------------------------------------+
```

Each user turn runs through a reliability pipeline before you see an
answer: the model writes a short **plan**, a **grammar-constrained**
(GBNF) decision call turns that into structurally-valid tool call(s), the
CLI executes those tools directly (not the model), and a **self-critique**
pass checks the final answer against the tool results before it's shown
to you. Full detail on each stage is in the sections below and inline in
`nanbeige_cli.py`.

## Setup script reference

| Flag | What it does |
|---|---|
| `bash setup.sh` | Full install (idempotent, safe to re-run) + launch |
| `--setup-only` | Install/build only, don't launch |
| `--run-only` | Skip all checks, just start the server + CLI (used internally by autostart) |
| `--force-rebuild` | Rebuild llama.cpp even if already built |
| `--selftest` | Start the server and run an end-to-end health check of chat + all tools, then exit |
| `--stop` | Stop the background `llama-server` process |
| `--no-autostart` | Install without adding the Termux autostart hook |
| `--disable-autostart` | Remove a previously-installed autostart hook |
| `--version` | Print the installed NCoder version and exit |

## In-CLI command reference

| Command | What it does |
|---|---|
| `/help` | List commands |
| `/reset` | Clear conversation, keep system prompt |
| `/system <prompt>` | Replace the system prompt |
| `/save [name]` / `/load <name>` | Persist/restore a conversation |
| `/sessions` | List saved conversations |
| `/regenerate` | Re-run the last prompt for a fresh answer |
| `/auto <goal>` | Run autonomously across multiple turns until the task's `todo.md` is fully checked off, the model signals it needs you, or a safety cap of 8 turns is reached |
| `/resume` | Show the last interrupted-task checkpoint, if any, and load it back in |
| `/undo` | Undo the last checkpointed tool-use step |
| `/stats` | Show the last response's speed stats |
| `/quit` | Exit (the server keeps running in the background — see below) |

#### Server lifecycle notes

`llama-server` intentionally outlives `/quit` so the next launch is fast
(no reloading the model into RAM). Run `bash setup.sh --stop` when you
actually want to free that RAM — e.g. before closing Termux for a while,
or before a rebuild.

## Tools available to the model

The model decides on its own, every turn, whether a tool is needed —
there is no user-facing toggle to turn tool use on or off.

| Tool | What it does |
|---|---|
| `web_search` | DuckDuckGo HTML scrape, no API key required |
| `http_request` | Arbitrary GET/POST/PUT/PATCH/DELETE to a URL the model constructs itself |
| `read_file` / `write_file` / `list_directory` | Sandboxed to `~/ncoder-cli/workspace/`, cannot escape it |
| `run_python` | Sandboxed subprocess execution, 15s timeout, output capped |
| `search_notes` | Keyword/TF-IDF-ranked search over workspace text files — not semantic embeddings, since a second model for that would compete for the same scarce RAM |
| `termux_api` | Clipboard read/write and notifications via the Termux:API app |
| `push_subtask` / `pop_subtask` | Internal orchestration tools — narrow the model's context to nested work, then fold back a confidence-tagged, optionally-verified, optionally-structured result. Supports `depends_on` for dependency-ordered scheduling (see below) |
| `request_user_input` | Explicit signal that the model genuinely cannot proceed without you — a missing credential, a choice only you can make, or a destructive action. The system prompt tells the model to prefer stating a reasonable assumption and continuing over calling this for ordinary ambiguity. |
| `record_fact` / `query_facts` | A queryable structured JSON fact ledger, distinct from `invariants.md`'s always-reinjected prose — for discrete facts a later subtask might need to look up by keyword rather than re-derive. |

Tool calls are constrained by a GBNF grammar generated from each tool's
JSON schema, so malformed arguments are structurally impossible for the
decision step — not just discouraged by the prompt. Up to 3 independent
calls can be batched into a single decision, so a multi-fact task doesn't
need a full extra round per lookup. The plan and tool-decision steps are
combined into a single grammar-constrained call (not two separate
calls) — both more efficient and a better match for Nanbeige4.1-3B's own
trained strength at sustained, coherent single-pass reasoning. Sampling
uses the model's own recommended `top_p=0.95` and `repeat_penalty=1.0`
on every call.

## Long-context & autonomous-task reliability

A phone-local setup has two failure modes a server deployment doesn't:
the process can be killed by the OS mid-task, and the context window is
small (6144 tokens by default). Each of the following targets one of
those specifically:

| Mechanism | Problem it solves |
|---|---|
| **Scratchpad** (`workspace/scratchpad.md`) | Full, unabridged tool output is written to disk, not kept in the live message list — only a short preview + a pointer goes into context. Verbose intermediate detail no longer forces early compaction of things that actually matter, and the model can `read_file` or `search_notes` the full version back if needed — confirmed the two are actually connected: scratchpad entries are indexed and retrievable via `search_notes` like any other workspace file. |
| **Pinned invariants** (`workspace/invariants.md`) | The model writes load-bearing facts/constraints here as it discovers them. Re-read from disk and re-injected verbatim every round; explicitly excluded from context compaction, so it can never be silently summarized away. |
| **Diff-based checkpointing** (`workspace/task_state.json`) | Only each round's *new* messages are appended to a diff list, not a full re-dump. `/resume` reconstructs full state from base + diffs after an interruption; `/undo` pops the single most recent diff to recover from one bad tool call without discarding the whole task. Once 5 diffs accumulate, they're automatically collapsed into a fresh base snapshot, so a very long task's checkpoint file doesn't grow into dozens of tiny diffs that need replaying. |
| **Dependency-ordered subtasks** (`push_subtask`/`pop_subtask` with `depends_on`) | Nested work (e.g. "look up A, then use A to do B") runs in a narrowed, clean context; only a short result summary folds back into the parent task when it pops. A subtask can declare it depends on another subtask's result — if that dependency hasn't finished, it's queued instead of started, and the model is told when it becomes unblocked. This is dependency-ordered *scheduling*, not concurrent model execution — there's a single inference stream, so the "graph" determines sequence, not parallelism. Verified directly: a queued subtask correctly waits, and its narrowed context receives the actual completed dependency's result once unblocked. If a subtask is left open when the round budget or stall detection ends the loop, it's automatically folded back to the root task. |
| **Concurrent tool dispatch** | Independent `web_search`/`http_request` calls batched into the same decision are genuinely I/O-bound (waiting on network, not the model), so they run concurrently via a thread pool — measured directly at roughly half the wall-clock time of sequential dispatch for a 2-call batch. Results are still appended to context in original call order for determinism. Other tools (files, Python, Termux:API, subtask control) stay sequential since they touch shared state or control flow. |
| **Mid-task reflection** | A cheap extra call checks whether the task is still converging on the original goal — distinct from the end-of-answer self-critique, this catches drift *while there's still round/time budget left to correct it*. Adaptively scheduled: pulled forward to the next round whenever a bounced verification, escalated tool failure, or detected contradiction fires, rather than only on a fixed every-3-rounds clock. |
| **Persistent todo file** (`workspace/todo.md`) | For anything more than 2-3 steps, the model maintains a checklist here instead of relying only on in-context planning. |
| **Context compaction** | At ~70% context usage, older tool-result turns are summarized into short lines via one cheap extra call, instead of hard-truncating or erroring. System prompt, pinned invariants, and the most recent exchanges are always kept verbatim; the nudge message explicitly tells the model it can `search_notes` the scratchpad for anything just summarized away. |
| **Stall detection** | Two consecutive rounds requesting the identical tool call with identical arguments breaks the loop rather than burning rounds on a stuck pattern. |
| **Wall-clock budget** | Each turn is capped at 180 seconds of tool-use time (not just a round count), since a stuck task at ~5 tok/s could otherwise run a long time before hitting a round limit. |
| **Prompt caching** | Every call in a round shares the same system prompt + few-shot prefix; `cache_prompt` lets llama.cpp reuse that cached prefix instead of reprocessing it on every call. |
| **Complexity-scaled budgets** | One cheap classification call up front tags the task simple/moderate/complex/autonomous and scales the round-count and wall-clock budget accordingly, instead of giving a quick lookup and a genuinely multi-stage task the same fixed budget. Checks the playbook cache first (keyword overlap, no model call) and reuses a closely-matching prior task's observed tier when one exists, skipping the classification call entirely. Falls back to the moderate tier if classification fails or the goal is empty. |
| **Multi-turn autonomy (`/auto`)** | Reduces how often you need to check in on a genuinely long task: `/auto <goal>` keeps invoking the plan→decision→tool→reflect cycle across successive turns automatically — checking `todo.md` after each one — rather than stopping and waiting for your input after every single round-budget's worth of work. Uses a reserved `autonomous` tier (25 rounds / 15 min) via `complexity_override`, never chosen by the classifier on its own. Bounded by an 8-turn safety cap regardless of `todo.md` state, so an unfinishable task can't run forever. |
| **`request_user_input` signal** | Inverts the default: instead of every turn implicitly stopping and returning control to you, the model is instructed to state a reasonable assumption and keep going for ordinary ambiguity, and only explicitly call this tool — a genuine stop, not a soft pause — when it truly cannot proceed (missing credential, a choice only you can make, a destructive action). Verified directly: it halts the round loop immediately, though any tool calls already queued earlier in the same batch still complete first rather than being silently discarded. |
| **Failure escalation** | Distinct from stall detection (which only catches *identical* repeated calls): tracks consecutive failures on the *same tool* even across varying arguments, and after 2 in a row, nudges the model to try a genuinely different tool or approach instead of grinding on one that clearly isn't working. Tracks which distinct tools have already been nudged away from — if a second different tool also fails repeatedly, the model is told plainly that multiple approaches have failed and pointed toward `request_user_input` instead of continuing to retry indefinitely. |
| **Verification-gated subtask completion** | `pop_subtask` bounces once (not blocked forever) if the subtask's goal involves something checkable — code, tests, facts, calculations — and no `verified_by` evidence was given. Forces an actual check (a test run, a cross-reference) before the model's own assertion that it's "done" is accepted. Fails open after one bounce, since the goal is making the model try, not creating an unbreakable loop. |
| **Contradiction detection** | When a subtask completes and at least one other has already finished, a cheap check compares the new result against prior ones and flags direct conflicts — catches independent subtasks silently disagreeing before that disagreement gets combined into a final answer. |
| **Subtask cycle detection** | `push_subtask`'s `depends_on` is checked against circular dependencies (A waits on B, B waits on A) before queueing — an undetected cycle would silently deadlock both subtasks forever, burning the whole round/time budget with no progress and no error. Also extended to fact-ledger name collisions: if a subtask name is reused (an earlier run under that name recorded a fact, and the name is later reused for a new pending subtask), querying that fact surfaces a warning about the ambiguity. |
| **Verification depth, tiered by complexity** | `simple` skips self-critique and whole-answer verification entirely; `moderate` runs self-critique only; `complex`/`autonomous` run both. Self-critique checks the answer against tool results; whole-answer verification separately checks the *combined* answer against the *original goal*, catching the case where every subtask was individually correct but the combined result still misses part of what was asked. `/auto` always runs full-depth verification regardless of any individual round's classification. |
| **`/auto` decomposability pre-check** | One cheap call before the autonomous loop even starts: can this goal actually be broken into concrete steps, or is it too vague? Avoids burning several unattended turns discovering mid-task that the goal needed clarification from the beginning. |
| **Structured fact ledger** | `record_fact`/`query_facts` — a queryable store for discrete facts, distinct from `invariants.md`'s always-reinjected prose constraints, so a later subtask can look something up by keyword rather than re-derive it or hope it survived compaction. |
| **Playbook cache** | A successful `/auto` run's tool-usage sequence is saved to the workspace and retrievable via `search_notes` for structurally similar future tasks — reuses existing search infrastructure rather than adding new tool surface. |

## TUI notes

- Every final answer ends with a small badge showing exactly how much
  verification actually ran on it (e.g. `[complex tier · full
  verification · revised]`) — not a fabricated confidence score, just a
  report of what's already tracked (tier, verification depth, whether a
  check changed the answer).
- `/auto` shows a live checklist panel of `todo.md`'s actual items
  (checked/unchecked, plain ASCII brackets) after every turn, not just a
  numeric remaining-count.
- A startup summary panel (RAM tier, active complexity budgets,
  Termux:API availability, playbook cache size) is shown once at launch.
- Every blocking call in the orchestration pipeline (classifying,
  planning, deciding, running a tool, reflecting, verifying,
  checking consistency, self-critiquing) shows a distinctly colored,
  animated status spinner with a small stats footer (round/budget
  progress, complexity tier) — not just the final answer generation.
  This is a *per-phase* transient indicator, not a permanently pinned
  footer that survives across the whole conversation: a full always-on
  status bar would need a persistent split-screen layout, which carries
  real risk of breaking Termux's scrollback behavior for a comparatively
  small benefit over what's here.
- The final answer streams with buffered rendering (word-boundary or
  ~100ms, whichever comes first) rather than re-rendering on every
  token — noticeably less CPU competition with the model itself on a
  slow device, with no difference in the final displayed content.
- Batched concurrent tool calls show live per-tool ✓/… status as each
  completes.
- `/auto` completion (success, safety cap, or `request_user_input`)
  triggers a Termux notification and vibration if Termux:API is
  installed — silent no-op otherwise.

## Configuration

**Runtime tuning** (`setup.sh`, near the top):

| Variable | What it controls |
|---|---|
| `MODEL_FILE` / `MODEL_URL` | Which GGUF quant to run |
| `LLAMACPP_PIN` | Pinned llama.cpp commit — bump deliberately, not automatically |
| Thread counts | Auto-detected from `/proc/cpuinfo` (performance-core count) |
| Context size, KV cache quantization, batch sizes | RAM-tiered via `pick_context_size()`: 6144 tokens below 5.5GB RAM (the 4GB target — unchanged from earlier versions), 10240 at 5.5GB+, 16384 at 7GB+. KV quantization/batch sizes set in `start_server()` |

**Long-task tuning** (`nanbeige_cli.py`, near the top):

| Constant | Default | What it controls |
|---|---|---|
| `CONTEXT_WARN_PCT` | 70 | When to trigger context compaction |
| `MAX_ROUND_SECONDS` | 180 | Wall-clock budget per turn's tool use |
| `MAX_BATCH_PER_ROUND` | 3 | Max tool calls batched into one decision |
| `REFLECTION_INTERVAL` | 3 | Rounds between mid-task "still on track?" checks |
| `CHECKPOINT_DIFF_COMPACT_THRESHOLD` | 5 | Diffs accumulated before collapsing into a fresh checkpoint base |
| `CONCURRENT_SAFE_TOOLS` | `{web_search, http_request}` | Which tools are safe to dispatch concurrently within a batch |
| `COMPLEXITY_BUDGETS` | see below | Per-tier (max_rounds, time_budget_seconds): simple=(3, 60), moderate=(12, 240), complex=(30, 480), autonomous=(60, 1200). Raised from earlier versions given the model's documented 500+ round tool-use capability and the merged plan+decision call halving per-round overhead — time budget remains the actual constraint on a phone, not round count. |
| `TOOL_FAILURE_ESCALATION_THRESHOLD` | 2 | Consecutive failures on one tool before nudging a different approach |
| `MAX_AUTO_TURNS` | 8 | Hard safety cap on `/auto`'s multi-turn loop, regardless of `todo.md` state |

## Directory layout

```
~/ncoder-cli/
+-- setup.sh                # install/build/launch script
+-- nanbeige_cli.py          # the CLI itself
+-- llama.cpp/               # cloned + built, pinned to LLAMACPP_PIN
+-- models/                  # downloaded GGUF weights
+-- venv/                    # Python virtualenv for the CLI's dependencies
+-- logs/
|   +-- llama-server.log     # rotates once it exceeds 5MB
+-- sessions/                # /save'd conversations (JSON)
+-- server.pid                # tracks the background llama-server process
+-- workspace/                # sandboxed - everything the model's file
    +-- scratchpad.md         #   tools and run_python can read/write
    +-- invariants.md
    +-- todo.md
    +-- task_state.json
```

## Security & privacy

- **File/code sandboxing**: `read_file`, `write_file`, `list_directory`,
  and `run_python` are confined to `~/ncoder-cli/workspace/` — path
  traversal (`../`, absolute paths) is explicitly checked and rejected.
  `run_python` runs as a fresh subprocess with a timeout, not in-process.
- **Network egress is not restricted**: `web_search` and `http_request`
  can reach any public URL the model decides to construct. This is by
  design (it's the point of the tool), but it means the model has open
  internet access, not an allowlisted one. If that's not acceptable for
  your use case, that's a real constraint to consider before relying on
  this for anything sensitive.
- **The model itself is not conventionally safety-tuned.** Nanbeige4.1-3B-heretic
  has had its refusal behavior mechanically removed ("abliterated"). It
  will comply with a much wider range of requests than a typical
  assistant. Treat its output accordingly, especially anything it
  produces via `run_python` or `http_request` before acting on it.
- **Termux:API access** (clipboard, notifications) only works if you've
  explicitly installed that separate app — it's not silently available.
- Nothing in this project phones home to any service controlled by this
  project's authors; all network activity is either the model's own
  tool calls (search/HTTP) or the one-time HF/GitHub downloads during
  setup.

## Troubleshooting

**Generations get killed partway through / server stops responding after a while**
Almost always OEM battery optimization killing Termux in the background.
`setup.sh` prints device-specific steps on first run; the short version:

| Manufacturer | Fix |
|---|---|
| Xiaomi/Redmi (MIUI/HyperOS) | Security app -> Battery -> Termux -> No restrictions; disable "MIUI Optimization" in Developer Options |
| Samsung (One UI) | Device Care -> Battery -> Termux -> add to "Never sleeping apps" |
| Huawei (EMUI/HarmonyOS) | Battery settings -> Protected Apps -> enable for Termux |

**Model download fails or hangs**
Check `MODEL_FILE` in `setup.sh` actually matches a real filename on the
[HF Files tab](https://huggingface.co/heretic-org/Nanbeige4.1-3B-heretic/tree/main).
Downloads resume automatically (`curl -C -`) if interrupted — just re-run
`bash setup.sh`.

**"Checksum mismatch" error**
The download was corrupted or incomplete. The script deletes the bad file
automatically — just re-run `bash setup.sh` to retry.

**Build fails or `llama-server` won't start**
Run `bash setup.sh --force-rebuild`. If it still fails, check
`~/ncoder-cli/logs/llama-server.log` for the actual error — the script
prints the last 30 lines automatically on a failed startup.

**Not sure if everything actually works**
Run `bash setup.sh --selftest` — it exercises the server, a real chat
completion, and every tool directly, then reports pass/fail per
component.

**Banner border looks misaligned**
A small number of Android terminal fonts render the Unicode block-drawing
glyphs at an unexpected width. This is a font substitution issue with
those specific glyphs, not a bug in the layout math (which pads to exact
character counts regardless of font).

## Known limitations

- Grammar-constrained tool calls simplify `http_request`'s `headers`
  field to a plain string rather than an open JSON object — GBNF handles
  fixed schemas far more easily than arbitrary key/value maps.
- `search_notes` is keyword-ranked, not semantic — it won't find a
  paraphrase that shares no words with the query.
- `/resume` is manual, not automatic — it surfaces the last checkpoint
  for you to explicitly continue from; it doesn't silently detect an
  interrupted task and resume it the instant Termux relaunches.
- Batched tool calls are capped at 3 per round (`MAX_BATCH_PER_ROUND`) —
  a task needing more independent lookups than that in one step spreads
  across additional rounds instead.
- Checkpoint diffs record each round's root-level message growth as it
  happened *before* that round's context compaction, if any — so
  `/resume`/`/undo` restore pre-compaction detail, not the compacted
  version actually sent to the model. This is a deliberate tradeoff
  toward more recoverable history, not a bug.
- Subtask context (`push_subtask`/`pop_subtask`) is not itself
  checkpointed — only the parent task's growth is recorded. If Termux is
  killed while a subtask is mid-flight, `/resume` restores you to just
  before that subtask was pushed, not mid-subtask.
- Subtask `depends_on` provides dependency-ordered *scheduling*, not
  concurrent model execution — there's a single inference stream, so
  declaring subtasks as independent changes the order they run in, not
  whether they run simultaneously. Only `web_search`/`http_request`
  network calls actually run concurrently (see `CONCURRENT_SAFE_TOOLS`).
- Tool-calling reliability scales with model size — a 3B model will
  occasionally skip a tool it should have used, misjudge when one is
  needed, or produce a malformed call the grammar has to reject and
  retry. The mitigations in this README reduce failure rates; they don't
  make a 3B model behave like a much larger one.
- `/auto` mode trades oversight for fewer check-ins by design — the
  model runs further unattended before you see its output, which means
  more distance it could cover down a wrong path before anyone notices,
  not just a hypothetical risk. The checkpointing, `/undo`, and
  `todo.md` mechanisms exist specifically to make that recoverable, but
  they don't prevent it — review `/auto` output rather than assuming a
  completed todo list means a correct one.
- The verification gate on `pop_subtask` decides whether a subtask
  "needs verification" via a keyword heuristic on the goal text (test,
  bug, fix, calculate, fact, etc.), not real semantic understanding — it
  can miss a checkable claim phrased without any trigger word, or
  occasionally flag something that didn't need it. It fails open after
  one bounce either way, so a false positive costs one extra round, not
  a stuck task.
- Contradiction detection is a single cheap model call per completed
  subtask, at temperature 0 — it catches direct, stated conflicts well,
  but isn't a rigorous consistency checker and can miss subtler
  disagreements or occasionally flag things that aren't really in
  conflict.
- Whole-answer verification and the `/auto` decomposability pre-check
  are both single cheap model calls, same caveat as above — they catch
  the common/obvious cases well but aren't rigorous, and both fail open
  (assume things are fine) if the check itself errors, so a network
  hiccup during the check never blocks a task that would otherwise
  succeed.
- The fact ledger and playbook cache are plain append-only JSON/JSONL
  files with no deduplication or staleness handling — a fact recorded
  early in a long-running project could become outdated and nothing
  currently prunes or supersedes it automatically.
- The chat-template sanity check (`--selftest`) can only detect the
  visible symptom of a wrong/generic template (raw special tokens
  leaking into responses) — it can't verify the GGUF's embedded
  template is an exact match for Nanbeige's actual expected format,
  since that would require the literal Jinja template string, which
  isn't reliably fetchable at setup time. If your specific quantized
  GGUF embeds an incorrect template, this check may still pass.
- Self-critique and whole-answer verification are skipped entirely for
  simple-tier turns, and whole-answer verification specifically is
  further limited to complex/autonomous tiers only (moderate gets
  self-critique alone) — see Configuration. `/auto` always keeps full
  verification regardless of what any individual round's classification
  would suggest.
- Playbook-informed classification uses simple keyword/Jaccard overlap,
  not semantic matching — it can miss a genuinely similar goal phrased
  with different words, or occasionally match on superficial keyword
  overlap between otherwise-unrelated tasks. A wrong reuse just means a
  suboptimal budget for that turn, not a broken one.
- The fact-ledger cycle-detection extension only catches the specific
  case of a reused subtask name creating provenance ambiguity — it
  isn't a general check that two subtasks' fact usage is otherwise
  circular in some more general sense.

## Uninstalling

```bash
bash setup.sh --stop                 # stop the background server
bash setup.sh --disable-autostart    # remove the Termux launch hook
rm -rf ~/ncoder-cli                   # remove everything (model, build, sessions, workspace)
```
