# Harness 行为参考

此文保留页面、状态、动作、证据和总结的详细契约。运行云沙箱见 [cloud-sandbox.md](cloud-sandbox.md)，当前模块决策见 [architecture.md](architecture.md)。

### Multiple observed states per page

Page identity and the appearance after an action are recorded separately. When
embedding retrieval and VLM verification reuse a page node, the screenshot and
semantic values are still retained in that node's `states` dictionary. For example,
an Expense Entry page can contain Food-selected and Income-selected states while
remaining a single page node.

Each state has a stable `state_id`, its own screenshot and screenshot hash,
observed semantic values, Activity, first arrival action sequence, timestamp and
linked `observation_ids`. `representative_state_id` identifies the original page
appearance; the existing page-level screenshot and `semantic_state` stay available
for compatibility. `semantic_state_history` also accumulates values from matched
observations, not only checkpoint merges.

`HASH_PROMPT` returns separate nullable `same_template` and `same_ui_state` decisions,
flat `existing_state` / `new_state` maps, and structured `state_changes`. A home page
with different local subtabs or foreground menus can share a template while retaining
different states and arrival paths. Different purposes, main destinations, workflow
stages or caller roles remain different templates. The compatibility `same_page`
boolean means a confirmed template match; legacy responses do not establish UI-state
equality. Unknown evidence never means equal. Descriptions/embeddings exclude local
selected subtabs, menu visibility and entity-specific data.

In state mode, visual-state reuse compares normalized behavioral conditions, context,
Activity and the actionable signature. Local tab conditions such as `topic_tab` are
recorded separately from main navigation `active_tab`. Exact data values remain in
observations and recovery guards. In page compatibility mode, semantic-value reuse
retains the existing behavior. There is no additional exact-pixel identity requirement.
When semantic values are unavailable, the existing tolerant visual comparison
with the original thresholds is used as fallback. Every action still retains its
own before/after screenshots and observation, including actions that reuse a state.
With screenshot saving disabled, metadata and edges remain available with empty
screenshot paths.

`transitions` and action `observations` include
`from_state_id` / `to_state_id`. Thus a same-page action can connect two states, and
`state_changed` records a known behavioral change rather than metadata dictionary churn.
Failed or unresolved destinations
have an empty `to_state_id`. Page-level `edge_evidence` includes the corresponding
`state_transitions` and observation IDs. Checkpoint merges move all states into the
surviving page without renaming state IDs or deleting their screenshots/evidence.
Planning memory includes a bounded state summary from both live and saved graphs.
The original screenshot/action/screenshot `state_transition_triplets` export keeps
its existing shape for compatibility.

Visual states retain observation evidence. By default, a separate state work queue
now drives exploration (see below). State mode writes graph schema version 4;
`--page-exploration` retains the original page queue and schema version 3. Older
graphs remain readable as contextual memory, but their missing states are not
reconstructed and their queues/coverage are not resumed.

### State work queue and functional deduplication

The default scheduling unit is a `StateWorkItem`, containing a page ID, normalized
behavioral state, entity/caller context, observed state IDs, its own current action
plan and progress, and an observed arrival path. An empty task form, a form with a
title and a form with both title and date have independent work/progress. A Save
attempt on an empty form cannot consume the filled form's Save. Page fields remain
compatibility/summary views; they no longer control state-mode scheduling.

Every uncached screenshot is analyzed for current controls, including screenshots
that reuse an existing page node after clicks or input. New controls create a new
actionable signature/work item. Exact analyses are cached (128 entries); normalized
state/work keys exclude coordinates and screenshot hashes. The existing analysis
request also returns `exploration.module`, `state`, `context` and exact `values`,
plus action-family, relevant-condition and replay hints. It does not require a
separate metadata request. Action selection and current-target localization still
share one request.

On revisiting a matched page in state mode, analysis includes a historical
screenshot, the current screenshot, and bounded current-run UI evidence: stable
identities, functional scopes, covered operations and unsuccessful attempts.
The latest retained action-source image is preferred; the original page image
is the fallback. Images remain in memory with screenshot saving disabled. First
visits use one image; custom analyzers without `analyze_screen_with_history`
retain their existing interface.

The model refreshes the complete current inventory and prioritizes new/uncovered
controls. Equivalent dynamic rows/cards and fixed buttons with changing badges
reuse a functional `control_key`, action family and nonempty stable `target_value`.
Current exact text/boxes remain necessary for grounding. Different entry behaviors,
tab/filter/menu choices and relevant states remain distinct; entity-specific and
persistent operations retain their context. The existing registry decides actual
coverage from observed effects. Historical coordinates or missing controls cannot
become current executable actions, and model claims never mark a page complete.

Analysis cache keys include the history revision, so progress changes can refresh
identical screenshots. History is limited to 32 attempted scopes, 32 known controls
and 24,000 characters; missing records never establish coverage. Revisit evidence
is logged in `memory_planning.jsonl` without image data and works with graph memory
disabled. Action selection also receives `node_exploration_summary` with applicable
common/state-specific coverage, failed or uncertain attempts and current operations,
even when external graph memory is disabled. Its readable description derives from
structured current-run evidence. Missing/truncated records never imply coverage.

Work keys combine the canonical page, normalized conditions, actionable surface,
Activity and entity/workflow context. Free text/dates can be grouped by meaningful
conditions such as empty/filled or unset/set; exact values remain in visual evidence
and recovery guards. Unknown values are never treated as empty/unset. Conflicting
observations, or changed observations with otherwise identical unknown conditions,
receive one bounded reanalysis; unresolved work is `state_unresolved` rather than
silently considered covered. Missing model metadata falls back conservatively to
observed semantic values and controls.

Functional coverage is separate from work/queue deduplication. Its key includes the
canonical page/module, stable control identity/type, selected variant, declared relevant preconditions
and context when required. Mode, tab, overlay and permission gates always apply.
Commit/mutation/unknown operations depend on all normalized conditions. Missing
dependency hints use the whole state/surface; unknown conditions prevent sharing
coverage across work items. Entity-independent `coverage_scope=module` operations
can reuse coverage after unrelated fields change. Context-specific operations retain
their entity/workflow scope. Page labels alone never establish cross-page reuse:
page deduplication/verified merges still apply first.

Only grounded, executed actions with an identified, observed effect enter
`functional_coverage`. This records an observed functional transition, not proof of
business success. No-ops and failed/uncertain outcomes do not become covered. Local
retry limits and a branch-repeat cap prevent endless loops. An uncertain persistent
commit/mutation is blocked after its first attempt to avoid duplicate side effects.

Covered controls are removed from exploration candidates. Controls explicitly marked
`workflow_role=advance|return` and replay-safe may still be used to complete an active
draft, such as entering a required title or confirming an already explored date
picker. Those actions are recorded with `purpose=navigation`, separately from new
functional exploration, and are bounded to one such traversal per workflow/work/
action plus the state action budget. Merely entering a covered module does not
restart a traversal of all its controls.

The live successor is explored before automatic recovery: input, date selection,
confirmation and Save can form one uninterrupted trace. After `continuous_steps`
actions, pending work rotates in FIFO order. An active draft may borrow at most two
additional commit/return steps; only those completion actions are eligible. Generic
Back/Search controls do not extend a branch. Every executed action is appended
to the actual trace. A successful Back appends an explicit guarded Back step;
restart replay starts a fresh verified trace. The App capability catalog refreshes at these round
boundaries and at completion.

### State recovery and budgets

`restore_to_work_item` restarts the App and replays the recorded path (or up to three
observed alternatives). It does not attempt recovery through repeated Back presses.
Every route step must still be explicitly replay-safe and have source/target
observations; save/create/persistent mutation paths are not replayed.

The execution VLM verifies the restart baseline and the result of **every** replayed
action, including identical screenshots. It receives the current screenshot, the
historical state image when available (otherwise a labeled page template), recorded
observations, the just-executed operation, and the next target. Historical state,
context, values and extraction conflicts are evidence for the model, not dictionary
equality gates. Search-history reordering, refreshed rows/counts, promotional content,
system chrome and equivalent categorical labels can be accepted when the workflow
remains usable. The VLM must still verify relevant input values, workflow modes,
overlays and object identity; a page-template match or successful device command
alone is insufficient.

A VLM rejection, invalid response or exhausted bounded request retries stops that
attempt. `replay_attempts` bounds restarted route attempts. Failed attempts preserve
the original route, retain the last observed position, and log the failed step,
model decision/reason and screenshot. Work is marked `recovery_failed` when no route
is verified. A VLM-approved arrival refreshes its controls and reconciles dynamic
metadata while protecting known local-tab, mode, overlay and entity/caller distinctions.
A template/usable-arrival acceptance cannot merge different behavioral states.
Replay checks arrival usability independently of novel exploration effects: repeating
a read-only query may legitimately yield the same results/reference image.
After a rejected target, retain the actual observed branch and rebuild its final guard;
only a separately verified arrival authorizes replay of that branch. `arrival_verified`
is independent of the operation's `replay_safe` safety classification. Unavailable or
rejected verification stops the route, and persistent writes remain non-replayable.
Force-stop does not reset persistent
app data, and recovery neither clears data nor fabricates a checkpoint snapshot.

Coordinate reuse remains independent of VLM state acceptance: only an exact full
source screenshot and screen-size match permits old coordinates; changed or unknown
sources require current-screenshot target localization before execution.

The `template_state_v1` identity contract and `action_dependencies_v1` coverage
contract are recorded alongside the compatible graph schema version. `state_graph.json`
exports work nodes grouped by template, independent paths and guards, verified work
edges, and shared coverage records. `route_evidence` includes state-mode replay attempts.
Pages without analyzed work are blocked, never reported complete.

Coverage conditions retain mode, blocking overlay, permissions and main navigation.
Local subtabs are included only when the action declares that dependency, or when
dependencies are unknown. Tab-switch controls depend on their source selection.
Common Scan/Social security entries may share coverage across `topic_tab`; topic-specific
Children care/Employment cards keep distinct variants and tab conditions. Unknown
dependencies and persistent/entity-specific operations remain conservative. Metadata
additions, omissions, case changes or list-window refreshes do not establish effects.

Defaults and configurable limits:

| Option | Default | Scope |
| --- | ---: | --- |
| `--max-states-per-page` | 16 | Expanded work items per page; extra visual evidence is retained |
| `--max-actions-per-state` | 20 | Exploration attempts plus live workflow navigation |
| `--continuous-steps` | 4 | Live continuation before reconsidering pending work |
| `--max-replay-steps` | 100 | Maximum actions in one restoration route |
| `--max-transition-repeats` | 2 | Exploration attempts of a conditional state/function branch |
| `--verification-retries` | 1 | Additional unresolved-action attempts, within all other limits |

Existing `--max-pages`, `--max-depth`, `--max-actions-per-page` and `--max-steps`
remain upper bounds. In state mode, depth counts page nesting with navigation loops
removed: typing on the same page does not add depth, and returning reduces it.
The full execution trace is retained separately. State-mode live navigation consumes the action/step budgets;
recovery Back/replay is separately bounded by route length and `--replay-attempts`.
Budget exhaustion is `budget_limited` with a reason, never successful completion.

Input actions default to `input_mode=replace`: focus the control, relocate its editable
field after keyboard/layout changes and read its current text from the screenshot.
For populated fields, click the located field, move the caret to the end, and send
backward Delete for its currently observed full text length plus eight extra keys.
The observed value is bounded to 512 characters (at most 520 Delete keys).
The model locates the field once; a separate emptiness check reuses that box without
requesting new coordinates. Only type after that check confirms the field is empty.
Missing, clipped or unreadable field text stops input; it is never treated as empty. Empty fields
need no extra clearing check. `append` and `clear`
are explicit alternatives. The low-level `input_text` helper retains its legacy append
default; direct `replace` / `clear` calls require a known `current_value`.
Screenshot checks apply to the explorer's observed-input execution path.
Failed or unverified clearing does not fall through to typing. The observed value must equal the requested value before
input receives effect credit; failures share a retry budget across equivalent states.
Search analysis keeps `draft_query`, `submitted_query` and `active_filter` separate.
App boundaries follow the package of the top resumed Activity from the Android
activity list, with window focus as a fallback. A target app remaining in a task's
back stack does not make a foreground Chrome Activity part of that app. External
screens are not analyzed or queued; the clicked action is terminal `external` for
that UI branch and is not retried or credited as an internal feature transition.
When internal branches remain, recovery tries at most two Back presses, then one restart of the resolved launcher
Activity in a fresh task. If the target app is still not foreground, scheduling
stops with the other branches pending instead of marking them all recovery failures.
The harness does not filter returned elements by overlay, visibility, enabled status or
interaction-layer metadata, and makes no extra layer-verification request. Element-ID and
coordinate checks still apply. `control_key` and option variants distinguish icon
buttons and filter choices independently of generic model action-family names.

Every run keeps `runs/<run_id>/app_graph.json`, redacted `config.json`, a source-content
hash, analyses, and decision logs. The latest top-level files remain compatibility views.
`termination.reason` distinguishes `frontier_exhausted`, `blocked`, `budget_limited`,
`interrupted`, and `failed`, with work/action status counts. `timings` separates exploration,
finalization, model summary requests and other measured phases; phase timings are inclusive
and must not be summed. `exploration_time` / `total_seconds` includes finalization.

```bash
python explore_android_app.py --package org.tasks \
  --max-states-per-page 16 --max-actions-per-state 20 \
  --continuous-steps 4 --max-transition-repeats 2 --max-steps 200
```

Schema 4 exports `work_items`, `work_queue`, `work_order`, `work_aliases`,
`functional_coverage`, `baseline`, budgets and per-page state summaries. Transitions
and action observations carry `from_work_id`, `to_work_id` and `purpose` as well as
their visual state IDs. Restore/navigation evidence is appended independently and
does not invent exploration edges. Verified page merges rebase work references,
coalesce equivalent work and retain attempts/coverage evidence. The existing triplet
export remains compatible. Coverage is authoritative only within this run; older
runs remain advisory memory, not trusted proof that the current app is already covered.

Configure embeddings (the default model is `BAAI/bge-m3`):

```bash
python explore_android_app.py --package com.example.app \
  --embedding-model BAAI/bge-m3 --embedding-timeout 60 \
  --semantic-similarity-threshold 0.85
```

`EMBEDDING_MODEL`, `EMBEDDING_BASE_URL`, and `EMBEDDING_API_KEY` are also supported.
The endpoint and key default to the VLM endpoint/key; use `--embedding-base-url`
and `--embedding-api-key` to override them. Choose a text embedding model served
by that endpoint. `--no-semantic-dedup` restores the original fast visual + VLM
flow. Injected legacy analyzers without `describe_screen` / `embed_description`
also retain that flow.

`SEMANTIC_SIMILARITY_THRESHOLD` sets the same threshold through the environment.

`app_graph.json` stores each page's `app_package`, `page_description`, `semantic_state`,
`description_embedding`, `embedding_model`, and `semantic_error` alongside the
existing fields. Every VLM decision is appended to
`<output>/vlm_comparisons.jsonl`, including retry attempts, the model reason,
the final acceptance/rejection reason, actionable-surface checks, and semantic
retrieval rank/score/descriptions when identifying a candidate page.

VLM comparison requests default to at most three attempts. Configure the bound
and retry delay with:

```bash
python explore_android_app.py --package com.example.app \
  --vlm-compare-max-attempts 3 --vlm-compare-retry-delay 1
```

If all attempts fail during normal page identification, exploration stops after saving the current graph and the
failed comparison record; an indeterminate comparison is never treated as a
new page.

## Periodic topology checkpoints

In default state mode, scheduling candidates are pending work IDs with independent
state/context/progress. The score favors remaining actions and shorter observed
routes, penalizing restore failures. Checkpoints still audit/verify page merges,
then rebase the work queue and choose a state work item. The page queue/scoring
details below describe `--page-exploration` compatibility mode.

Inspired by UI-KOBE's runtime coverage checkpoints, the explorer synchronously
audits its graph every **50 completed exploration action attempts** by default.
Use `--coverage-checkpoint-steps 0` to disable this feature. Failed executions
count once after their transition is recorded; invalid-coordinate skips, Back,
and path replay do not count. The original unlimited step budget is retained
(`--max-steps 0`). For UI-KOBE's 100-step / 50-step-checkpoint setup:

```bash
python explore_android_app.py --package com.example.app \
  --max-steps 100 --coverage-checkpoint-steps 50 --coverage-checkpoint-top-k 15
```

This runs a checkpoint at step 50, but not at the final step 100. A run that
exhausts its frontier earlier stops normally. Each checkpoint:

1. Sends a **text graph summary** (page descriptions, state values, activities,
   visit counts, degrees, processed controls, and edge instructions) to the model
   to propose duplicate node pairs. It does not rerun all-pairs embedding search.
2. Verifies each valid, same-App pair using the two reference screenshots through
   the existing bounded VLM comparison. Missing images, failed comparisons, or
   different actionable surfaces leave the pair separate. This extra surface
   check preserves the explorer's distinct scroll/menu state semantics.
3. Keeps the smaller numeric page ID, rewires edges, combines controls, action
   progress, activity lists, observed state values and visits, updates aliases,
   image/hash caches and queues, and saves the graph immediately after each merge.
   Original transition observations and screenshot files remain available.
   Pending actions on merged pages are relocated against the current screenshot
   before execution; historical coordinates are not blindly reused.
4. Ranks target-App nodes reachable from the root through successful recorded
   transitions with verified observed destinations using `3 * unexplored_elements + max(0, 3 - out_degree) +
   max(0, 3 - visit_count) + 2 * unresolved_pending_actions`. Out-degree counts unique destinations, including
   self-loops. Visits count page registrations, matched action-result observations,
   and successful scheduler replays (not each recovery Back).
5. Takes the top K nodes, excludes the current node, and asks the model to choose
   one. Invalid selections or model errors fall back to the top candidate. The
   scheduler saves unfinished work, prioritizes the chosen node, then restarts,
   replays and verifies arrival with the existing lifecycle. A navigation failure
   marks that target `recovery_failed`; remaining queued work still continues.

Unlike UI-KOBE's open-ended action generation, this harness uses finite per-page
plans, with bounded verification attempts for unresolved actions. Candidates therefore must have **remaining planned actions within the
configured action/depth budget** and must not have failed recovery. Already
exhausted pages are not repeatedly selected. With no candidates, ordinary
exploration continues. These scores are scheduling heuristics, not estimates of
the fraction of all App features covered. There is no periodic replay of every
edge, final completeness verdict, or edge-instruction normalization.

`coverage_checkpoints.jsonl` records each checkpoint's step, pair decisions,
candidate scores, selected target and fallback reason. It starts fresh each run.
Screenshot comparison details remain in `vlm_comparisons.jsonl` with context
`coverage_checkpoint_merge`. Audit/selection service failures are logged and
do not abort normal exploration. Legacy injected analyzers without the optional
audit/selection methods skip the text audit and use deterministic target ranking.

`app_graph.json` adds `completed_steps`, `node_aliases`, checkpoint log/count,
and per-page `visit_count`, `activities`, `semantic_state_history`, and
`requires_action_relocation`. `semantic_state` remains the representative
observation; the history field preserves merged values without changing its type.
`max_pages` bounds live graph nodes; merged IDs are never reused. These topology
checkpoints do **not** add disk-based resume support.

Run the offline regression suite from the repository root:

```bash
python -m pytest auto-explore/tests -q
```

## Graph memory and App-relevant inputs

Graph memory is enabled by default. It combines the **live graph** with previous
graphs of the **same App package** to inform both new-page analysis and the next
action choice. This is read-only contextual memory, not loading an old exploration
queue or resuming old progress.

By default, the explorer reads an existing `<output>/app_graph.json` and JSON
snapshots under `<output>/memory_graphs/`. Before the live graph overwrites the old
output, the old JSON is archived there under its content hash. Identical snapshots
are loaded only once; images are not archived or required for text memory. Keep
separate output directories for different Apps. You can instead explicitly supply
one or more prior graphs (paths are relative to the working directory):

```bash
python explore_android_app.py --package com.example.books \
  --memory-graph ./previous_books/app_graph.json \
  --output ./output/books_run2
```

Repeat `--memory-graph` to combine multiple reference runs. Explicit sources replace
automatic discovery. Invalid or different-package explicit files fail at startup;
invalid automatically discovered files are reported and ignored. External source
files are not modified. If a source is also the live output file, its original JSON
is archived first.

For each planning request the memory builder retrieves at most **8 pages**, using
the current page, neighboring transitions, current pending controls and content,
description/path word overlap and
Activity information. The serialized memory is bounded to **12,000 characters**.
It includes page descriptions, public content hints, controls, recent action
outcomes and the last six steps of each remembered page's arrival path. The path
includes original inputs and has length/truncation metadata; it is advisory context,
not an instruction to replay a partial route. Retrieval matches semantic content
rather than JSON field names or node IDs, and distinguishes field purposes such as
book-title search versus author search. It excludes embeddings, image bytes and
historical coordinates. Configure
these bounds with `--memory-top-k` and `--memory-max-chars` (minimum 2,000).

Before each exploration action, an additional screenshot-based model call selects
among the remaining planned actions. It can reorder the unprocessed part of the
plan, choose input text and locate the selected target on the current screenshot
in a single model request, but cannot invent a target, change the action type,
reuse completed actions outside the bounded verification plan, or bypass the per-page action budget. Historical knowledge
is advisory: the current screenshot is authoritative, and device execution success
is not treated as proof of business success. Recovery replays keep their original
recorded inputs rather than generating a different query.

For search fields the prompts favor visible or remembered public content such as
book/product titles, artists, locations or topics relevant to that App and field.
New screenshot analyses can return `content_hints` to retain such names for later
planning. The model is prompted to cite the supporting memory ID or current visual
evidence in `input_reason`; this is logged for review, not a guarantee that its
semantic judgment is correct. Generic filler (`example`, `test`, `test123`,
`example text`, `test query`, etc.) is rejected by the
step planner. The existing ADB text transport still requires **printable ASCII,
no percent sign, at most 128 characters**; the model may choose a related English
term and explain the adaptation. This change does not add Chinese/Unicode keyboard
input support. If the model cannot provide suitable input, the planner uses another
usable pending action, or leaves the page `planning_blocked` with its action progress
intact. Invalid model selections and request failures may fall back to a usable
pending action only after current-screen localization succeeds. Explicit “no
suitable action” decisions or invalid returned geometry block the page.

`memory_planning.jsonl` records retrieved context, candidate actions, model choices,
input rationale and fallback/block reasons. `app_graph.json` adds `memory_sources`
(the ordered paths corresponding to `history_0`, `history_1`, etc.),
`memory_planning_log`, and per-page `content_hints`. Each run starts a new planning
log. Queries actually used are saved in planned actions, transitions and child
replay sequences.

### Current-screen action grounding

Actions retain their semantic target (`target_description`, `element_text`,
`element_type`) separately from the coordinates used to execute it. Every normal
step selects an action and returns its current box/swipe endpoints in the same
model response. Coordinates are mapped from the model image to the device exactly
once. Merged-page actions and verification retries use this same response, without
an additional localization request. The model cannot rewrite the target, operation
or duration; verification retries retain their original input.

`source_layout_id` identifies the exact screenshot content and dimensions used to
locate those coordinates; `source_screen_size` and `grounding_method` are saved in
planned actions and transition details. Route steps carry the layout ID and method
alongside their existing `screen_size`. Planning logs include the old candidate
source, current layout ID and resulting coordinates. The layout ID is a SHA-256
fingerprint of the image payload and size, independent of page identity and tolerant
perceptual hashes. It is deliberately conservative: even a clock/animation change
invalidates cached coordinates. A matching page node alone never validates them.

Reordered lists, restored pages, scrolling, keyboards and expanded/collapsed panels
therefore use freshly located controls. Missing, hidden or ambiguous targets must
be declined by the model; missing, degenerate or out-of-screen geometry is rejected
locally. A blocked selection leaves the action pending. This validates coordinate
provenance and geometry, not the model's semantic accuracy or changes occurring
after the screenshot was captured.

### Layout-checked recovery replay

Recovery and scheduler navigation first restart the App and replay the recorded
operations with a source-layout check before each step. Recorded coordinates can
be reused without a localization call only when the exact source screenshot and
screen size match. A changed or unknown source is relocated before any tap/input/
swipe, including legacy paths with coordinates but no provenance. Route steps retain
click/input bounds, swipe start/end points, input text,
duration and the screen size at execution time. Default swipe endpoints are also
recorded. Every executed step is checked by the execution VLM before the next step,
even when its source coordinates were reused. This also applies to page-mode
compatibility routes; a successful device command alone does not prove arrival.

If coordinates are invalid, localization/execution/capture fails, or
the VLM rejects an intermediate/final arrival, recovery restarts from the root.
Every attempt applies the same source-layout rule: identical sources reuse their
recorded coordinates; changed or unknown sources use model localization. Original
action types, inputs and durations are retained. `--replay-attempts` bounds restarted
attempts (default: 2); page-mode compatibility retains one initial coordinate pass
in addition to that retry budget. Legacy paths without coordinate provenance require
model localization before execution.
Only a verified replay updates relocated route coordinates and source layouts for the
next recovery; unsuccessful attempts leave the recorded route intact.

Injected analyzers used for recovery must implement `verify_replay_state`, returning
`status`, a boolean `matched`, and `reason`. The production analyzer obtains that
decision from the execution VLM with bounded comparison-request retries.

Use `--no-graph-memory` to disable historical loading and automatic verification
retries. Current-screen action selection/grounding and evidence recording remain active.
Injected legacy analyzers remain compatible: without `analyze_screen_with_memory`
the analysis phase retains its previous behavior. Without `choose_next_action`,
pending actions are localized individually when their source changes (or when
verification/merged-page relocation is required). A legacy selector returning only
an index also requires individual localization; unavailable localization blocks
execution instead of reusing stale coordinates.

## Evidence-backed topology knowledge

The memory pipeline separates observations, exploration questions and the App
capability catalog. Deterministic evidence statistics remain available. The
configured `--summary-model` maintains the catalog using the existing API service
(see below).

### 1. Observations and edge evidence

Each action observation has a unique `run_id:sequence` identifier, timestamp,
App package/environment scope, source/target descriptors, observed source state,
arrival inputs, semantic action target, actual input, before/after fingerprints,
screenshot references, execution/observation status and recovery result.
`business_success` is explicitly unknown (`null`). Outcomes are:

| Outcome | Meaning |
| --- | --- |
| `destination_observed` | The resulting screen was identified as a different graph node. |
| `same_page_changed` | The identified node is unchanged but the visible image changed. |
| `no_visible_change` | The identified node and visible image are unchanged; the cause is unknown. |
| `execution_failed` | Device execution returned failure or raised an exception. |
| `observation_failed` | Execution succeeded but its resulting screenshot was unavailable. |
| `identification_failed` | A screenshot exists but page identification raised an error. |
| `destination_unresolved` | No target node was identified, for example at the node budget. |
| `outside_app` | The action left the target App. |
| `invalid_action` | Execution was skipped because coordinates or relocation were unavailable. |

`Transition.success` now means device execution success even when the screenshot
fails. Use `destination_verified` to decide whether a transition can contribute to
graph reachability. Unknown destinations are empty strings, never invented
self-loops. Invalid-action observations do not add transitions or consume an
executed-step budget. Identification/recovery exceptions leave action evidence
before propagating; this does not change the existing stop-on-identification-error policy.

`app_graph.json` includes `schema_version: 4` (3 in page compatibility mode), `run_id`, `observations`,
`edge_evidence`, `route_evidence`, `app_capabilities`, and `inherited_observations`.
The legacy `experiences` array remains empty in the default explorer.
Edge evidence aggregates current-run observations by canonical source/target,
action/input and observed conditions. It reports observation and attempt counts,
unresolved/execution-failure counts, alternative destinations and the empirical
destination share **among identified results**. This share is not a probability
of correctness or business success.

Replay attempts separately record the complete semantic route, preserved inputs,
whether final arrival was verified, and the zero-based failing step when known.
The `mode` field distinguishes the layout-checked `coordinates` pass from forced
`model` attempts; `relocated_step_indices` lists steps localized in either pass.
`route_evidence` aggregates these attempts. A successful final arrival does not
invent verified intermediate edges. Recovery Back operations remain represented
by their recovery result, not as independently explored transitions.

Append-only evidence lives at `<output>/runs/<run_id>/observations.jsonl`.
Page and transition images also live under that run directory, so later runs do
not overwrite their evidence. Existing pre-upgrade images are not migrated.
Node merging only changes graph projections/aliases; raw observations remain intact.
The graph snapshots and evidence logs are not disk-resume support.

### 2. Exploration checklists and decision-specific retrieval

Each serialized page has an `exploration_checklist`. It distinguishes untried,
observed, attempted-but-unresolved, blocked, and conflicting-result actions, plus
visible controls that have no plan. Entries retain evidence references, attempts,
observed destinations, pending-plan status and questions to resolve. A page with
`exploration_status: complete` has exhausted its plan; this does not assert that
all App functionality or all checklist questions are resolved.

Memory requests have four purposes:

- `analyze`: related page surfaces and content, without action-progress checklists.
- `action`: local checklists, action outcomes, conditions and relevant App capabilities.
- `replay`: target-page context, previous route successes/failures and the actually executed route prefix.
- `schedule`: pending questions and relevant App capabilities for reachable, budget-eligible candidates.

All contexts share `--memory-top-k` and `--memory-max-chars` bounds. The planner
logs its retrieved context and choices. If selection fails, the deterministic
fallback prefers usable pending actions with fewer prior attempts. History can
guide decisions but cannot supply new live nodes, coordinates or reachable edges.

With a memory-capable action planner, unresolved executed actions can receive
up to **one additional verification attempt** by default. They are appended to
the finite plan, identified by `verification_of`, and relocated on the current
screenshot before execution. Input verification keeps the original input. The
ordinary per-page action, total-step and depth budgets still apply. Outside-App
results and invalid-coordinate skips are not automatically retried. Configure with:

```bash
python explore_android_app.py --package com.example.app \
  --verification-retries 1 --memory-scope "app-v2-test-account"
```

Use `--verification-retries 0` to disable these extra attempts. Unresolved questions
remain in the checklist after retry/budget exhaustion. The implementation does not
invent prerequisite workflows or arbitrarily generate additional controls.

## Incremental App core-capability catalog

The default explorer now produces **one model-generated summary**: an App-level
catalog of what users can accomplish. Both state-work and legacy page exploration
use it. Per-action experience summaries, per-node summaries and the separate App
content profile are no longer generated by the runtime. Per-node summary and replica export remain explicitly available in harness/summary.py and harness/replica.py; the duplicate experience-summary service was removed. Existing output files are preserved.

Each capability has a stable semantic key, a goal-oriented description,
preconditions, configurable inputs, functional variants, evidence references and
open verification questions. For example, entering a title and opening a date
picker can inform one task-creation capability with title/date variants.
Visiting the same control repeatedly does not create a separate capability.
The model is not restricted to a fixed App-category or feature vocabulary.

### Collection and evidence boundaries

The catalog consumes state-local controls, normalized functional conditions and
actual recorded actions. Action evidence includes before/after conditions,
outcome, targeting quality, workflow context and a bounded prefix of the actual
execution trace. Source references retain run/work/state/observation IDs and
screenshot paths. Untried controls contribute entry evidence; they do not prove
what happens when used. Screens explicitly belonging to another package do not
become capabilities of the target App.

The program assigns these evidence levels; the model cannot set them:

| Level | Meaning |
| --- | --- |
| `entry_observed` | A control/entry was observed; the claimed effect remains unverified |
| `effect_observed` | At least one cited action has a recognized UI change |
| `needs_review` | Cited evidence has an explicit targeting or recognition conflict |

These levels apply to a capability and its variants. A recognized destination or
a grounded tap does not prove business completion or persistence.
`business_success` remains `unknown`, `coverage_complete` remains false, and
model interpretations are explicitly advisory. Failed execution, no visible
change, external screens and known misclicks cannot establish a confirmed
business workflow. Independent trials must not be concatenated into a workflow.

Exact input values and coordinates are omitted from summary action data. Named
input-state fields are reduced to functional categories while boolean validity
and unknown/empty/filled conditions remain distinct. Raw UI labels/descriptions
and screenshots can still contain personal data; this catalog is not an
anonymized export.

### Summary model and incremental updates

The catalog uses the existing client and configuration in `harness/summary.py`:
`--summary-model` / `SUMMARY_MODEL`, `--summary-api-key` / `SUMMARY_API_KEY`,
`--summary-timeout`, `--summary-budget-seconds` and `--summary-max-chars`. It shares the configured service
endpoint; the optional summary key falls back to the existing API key.
It never falls back to the execution model. Existing model-role validation still
requires separate execution and summary models and graph memory.

Run from the auto-explore repository root with --device or ANDROID_SERIAL set:

```bash
python explore_android_app.py --package org.tasks --output output/tasks
```

From the `auto-explore` directory, use `python explore_android_app.py` instead.
A nonempty SUMMARY_MODEL enables the catalog; the default is empty (evidence only). To override it:

```bash
python explore_android_app.py --package org.tasks --output output/tasks \
  --summary-model "YOUR_SUMMARY_MODEL" --summary-max-chars 12000
```

The dedicated prompt is
[`harness/prompts/app_capabilities.md`](../harness/prompts/app_capabilities.md).
Each request contains new evidence and a bounded index of existing capabilities,
including the most relevant descriptions/variants. The model returns up to six
capability updates; code validates identities, lengths and every citation before
merging the batch. Existing capabilities and unmentioned variants are retained.

Updates run at state/page-round boundaries and commit/return boundaries. A round
sends one bounded batch, with at most one extra request to repair invalid
JSON/schema output. Current-run evidence is processed before historical-only evidence.
All requests, including finalization and format repair, share a cumulative time budget
(`--summary-budget-seconds`, default 120; 0 collects evidence without requests).
Each request timeout is capped by the remaining budget. Finalization processes remaining
batches within that budget; an unavailable service stops further requests for this run.
Deferred evidence remains pending in the saved catalog. Calls are synchronous and add latency. Identical evidence
within the service lifetime is deduplicated, including overlapping history
snapshots. Reading memory and saving files never call the model.

Invalid output and service failures preserve the accepted catalog and original
evidence. Invalid output is listed as failed; timeouts and outages remain pending for a
later rebuild. Neither is retried indefinitely within the run.
An empty summary model disables model generation while preserving collected
evidence. To retry a failed historical batch, rebuild from its saved graph.

Action selection and coverage scheduling receive a bounded, relevant excerpt of
the same catalog, including `next_checks`. Those suggestions can guide choices
among available candidates; they do not create actions, mark work completed,
bypass deduplication, or override state-recovery checks and exploration budgets.

### Outputs and cross-run reuse

| Artifact | Contents |
| --- | --- |
| `<output>/app_capabilities.json` | Canonical catalog, functional variants, evidence/provenance, pending/failed evidence and model metadata |
| `<output>/app_capabilities.md` | Human-readable rendering of the same catalog |
| `<output>/runs/<run_id>/app_capabilities.json` / `.md` | Per-run copies |
| `<output>/runs/<run_id>/app_capability_events.jsonl` | Batch IDs, model, revision, acceptance and bounded validation diagnostics |
| `app_graph.json.app_capabilities` | Embedded catalog snapshot; `summary_kind` is `app_capabilities` |

The catalog exposes `current_run_evidence_ids` and `historical_only_evidence_ids`.
Historical capabilities do not count as branches covered in the current run.

Only graphs with matching `app_package` and `memory_scope` contribute history.
The latest output graph is archived by the existing graph-memory loader before
replacement. Raw action evidence (including inherited observations) and structured
untried surfaces are reprocessed. Earlier model summaries and synthetic examples
are never treated as evidence. Historical behavior remains advisory until checked
against current screens; use a different `--memory-scope` for incompatible
App versions or environments.

### Rebuild old exploration results

The device-free rebuild command uses the same configured summary model and
writes to a separate output directory. It does not change the source graph,
operate ADB or reuse old summary prose. Run from `auto-explore`:

```bash
python summarize_app.py --graph output/tasks/app_graph.json --output output/tasks_capabilities
```

Known misclicks cannot be corrected automatically from old textual observations.
Optionally supply `--reviews path/to/reviews.json`, mapping real observation IDs
to explicit reviews:

```json
{
  "actual-run-id:actual-observation-id": {
    "status": "target_mismatch",
    "reason": "The attachment tap opened the location control."
  }
}
```

Supported review statuses are `target_mismatch` and `recognition_conflict`.
Reviews affect only the rebuilt projection; source records remain unchanged.
A manifest under the new run directory links the source, review file and output.
Exit status is nonzero when the catalog is empty, pending or contains failed
batches. Rebuilding invokes the configured model and therefore uses API quota.

## ETCSLV mapping

| Harness part | Module | Responsibility |
| --- | --- | --- |
| E — Loop | `harness/loop.py`, `harness/state_loop.py` | State continuation, guarded scheduling, and page-mode compatibility |
| State frontier | `harness/work.py` | Work identity, independent progress, conditional coverage and budgets |
| T — Tools | `harness/tools.py` | ADB device operations and VLM calls |
| C — Context | `harness/context.py`, `harness/memory.py`, `harness/planning.py`, `harness/knowledge.py`, `harness/summary.py`, `harness/capabilities.py` | Bounded retrieval, the App capability catalog, evidence, checklists and action selection |
| S — State | `harness/state.py` | Graph, queue, caches, screenshots, JSON output |
| L — Lifecycle | `harness/lifecycle.py` | Start, stop, Back recovery, restart and replay |
| V — Verify | `harness/verify.py`, `harness/coverage.py` | Page/action checks, topology audit and coverage-priority ranking |

Shared dataclasses live in `harness/contracts.py`; model action normalization lives
in `harness/actions.py`; CLI parsing lives in `harness/cli.py`.

Existing graph evidence remains readable. Unused legacy summary-log and null service fields are no longer exported; see architecture.md for the consolidation map.
Disk checkpoint/resume, full append-only trajectory logging, and a separate final verifier can
be added later without growing the execution loop again.
