# Codex Main-Thread Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Silence Codex subagent rollout notifications by default while preserving all main-session notifications and an explicit opt-in for subagent debugging.

**Architecture:** Parse the first `session_meta` in each rollout as the file identity, classify only explicit `thread_source == "subagent"` records as subagents, and apply the filter before constructing or sending notifications. Keep unknown and legacy metadata on the notifying path to avoid missing main tasks; keep ZCode and every delivery channel unchanged.

**Tech Stack:** Python 3 standard library, `unittest`, JSONL rollout files, shell-based platform installers.

## Global Constraints

- `CODEX_WATCH_NOTIFY_SUBAGENTS` defaults to `0`.
- Explicit subagent sessions are silent for completion, failure, abort, and custom event types unless the opt-in is enabled.
- Missing or old metadata continues to notify.
- Bark, ntfy, webhook, command, and local macOS delivery behavior remains unchanged.
- ZCode behavior remains unchanged.
- No third-party dependency is added.

---

### Task 1: Session Classification and Event Filtering

**Files:**
- Create: `tests/test_codex_watch_notifier.py`
- Modify: `codex_watch_notifier.py:428-590`

**Interfaces:**
- Consumes: rollout `session_meta` dictionaries and existing `env_flag(name: str, default: bool) -> bool`.
- Produces: `notify_subagents_enabled() -> bool`, `is_subagent_session(meta: dict[str, Any]) -> bool`, and `trigger_from_record(..., meta: dict[str, Any] | None = None) -> dict[str, Any] | None`.

- [ ] **Step 1: Write failing behavior tests**

Create standard-library tests that write temporary rollout files and assert:

```python
def test_main_session_task_complete_is_not_filtered(self):
    path = self.write_rollout(self.session_meta("user"))
    event = notifier.trigger_from_record(path, 1, self.task_complete(), set())
    self.assertIsNotNone(event)

def test_subagent_task_complete_is_filtered_by_default(self):
    path = self.write_rollout(self.session_meta("subagent", parent_thread_id="parent"))
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CODEX_WATCH_NOTIFY_SUBAGENTS", None)
        event = notifier.trigger_from_record(path, 1, self.task_complete(), set())
    self.assertIsNone(event)

def test_subagent_abort_is_filtered_by_default(self):
    path = self.write_rollout(self.session_meta("subagent", parent_thread_id="parent"))
    event = notifier.trigger_from_record(path, 1, self.turn_aborted(), set())
    self.assertIsNone(event)

def test_subagent_notification_can_be_enabled(self):
    path = self.write_rollout(self.session_meta("subagent", parent_thread_id="parent"))
    with mock.patch.dict(os.environ, {"CODEX_WATCH_NOTIFY_SUBAGENTS": "1"}):
        event = notifier.trigger_from_record(path, 1, self.task_complete(), set())
    self.assertIsNotNone(event)

def test_missing_metadata_is_not_filtered(self):
    path = self.write_rollout({"type": "event_msg", "payload": {"type": "task_started"}})
    event = notifier.trigger_from_record(path, 1, self.task_complete(), set())
    self.assertIsNotNone(event)

def test_first_session_meta_defines_rollout_identity(self):
    path = self.write_rollout(
        self.session_meta("subagent", parent_thread_id="parent"),
        self.session_meta("user"),
    )
    self.assertEqual("subagent", notifier.load_session_meta(path)["thread_source"])
    self.assertIsNone(notifier.trigger_from_record(path, 1, self.task_complete(), set()))
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python3 -m unittest -v tests.test_codex_watch_notifier`

Expected: the main and legacy tests pass under existing behavior, while the default subagent filtering tests fail because they receive notification dictionaries.

- [ ] **Step 3: Implement minimal classification and filtering**

Add:

```python
def notify_subagents_enabled() -> bool:
    return env_flag("CODEX_WATCH_NOTIFY_SUBAGENTS", False)


def is_subagent_session(meta: dict[str, Any]) -> bool:
    return str(meta.get("thread_source") or "").strip().lower() == "subagent"
```

Include `parent_thread_id` in `load_session_meta()`. Update `trigger_from_record()` to accept optional preloaded metadata and return `None` when `is_subagent_session(meta)` is true and the opt-in is false.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python3 -m unittest -v tests.test_codex_watch_notifier`

Expected: all tests pass.

- [ ] **Step 5: Cache metadata per file and log suppression once**

In `process_file()`, load metadata once, store a `subagent_suppression_logged` flag in that file's state record, emit one concise log line when suppression starts, and pass the metadata to `trigger_from_record()`.

- [ ] **Step 6: Run tests and syntax validation**

Run: `python3 -m unittest discover -s tests -v`

Run: `python3 -m py_compile codex_watch_notifier.py tests/test_codex_watch_notifier.py`

Expected: all tests pass and both files compile without output.

- [ ] **Step 7: Commit behavior and tests**

```bash
git add codex_watch_notifier.py tests/test_codex_watch_notifier.py
git commit -m "Silence Codex subagent notifications"
```

### Task 2: Configuration, Diagnostics, and Local Deployment

**Files:**
- Modify: `env.example`
- Modify: `README.md:166-215`
- Modify: `README_HANDOFF.md:38-60`
- Modify: `codex_watch_notifier.py:940-976`
- Modify: `build_packages.zsh`
- Modify: `docs/superpowers/plans/2026-07-12-main-thread-notifications.md`

**Interfaces:**
- Consumes: `notify_subagents_enabled() -> bool` from Task 1.
- Produces: documented `CODEX_WATCH_NOTIFY_SUBAGENTS` configuration and visible `--doctor` policy output.

- [ ] **Step 1: Add a failing doctor-output test**

Capture `doctor()` output with the opt-in unset and assert it contains:

```text
Codex subagent notifications: main sessions only
```

Run: `python3 -m unittest -v tests.test_codex_watch_notifier.DoctorTests`

Expected: FAIL because the policy line is absent.

- [ ] **Step 2: Add diagnostic output**

Print exactly one policy line:

```python
policy = "enabled" if notify_subagents_enabled() else "main sessions only"
print(f"Codex subagent notifications: {policy}")
```

Run: `python3 -m unittest -v tests.test_codex_watch_notifier.DoctorTests`

Expected: PASS.

- [ ] **Step 3: Document the default**

Add `CODEX_WATCH_NOTIFY_SUBAGENTS=0` to `env.example`, add it to the README configuration table, and explain in both README files that Codex 5.6 child-agent rollouts are ignored by default while main-session completion, attention, and abort events remain enabled.

- [ ] **Step 4: Keep packaged README images complete**

Add `assets/cover-notification-loop.png` to `build_packages.zsh` so the packaged README's second cover reference resolves.

- [ ] **Step 5: Run full verification**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile codex_watch_notifier.py tests/test_codex_watch_notifier.py
python3 codex_watch_notifier.py --dry-run --test
python3 codex_watch_notifier.py --doctor
./build_packages.zsh subagent-filter-test
```

Expected: tests pass, syntax checks are silent, dry-run prints one test notification without network delivery, doctor shows `main sessions only`, and all three platform archives build.

- [ ] **Step 6: Install and restart the local LaunchAgent**

Run: `./install_launch_agent.zsh`

Then compare the repository and installed runtime hashes and inspect `launchctl print gui/$(id -u)/com.xutao.codex-watch-notifier`.

Expected: hashes match and LaunchAgent state is `running`.

- [ ] **Step 7: Verify against real rollout metadata without sending**

Replay one known main-session rollout and one known subagent rollout with an isolated state and `--dry-run`, or invoke the parser directly through the tests. Expected: the main event produces output and the subagent event produces zero notification output.

- [ ] **Step 8: Mark plan checkboxes complete and commit**

```bash
git add env.example README.md README_HANDOFF.md codex_watch_notifier.py build_packages.zsh docs/superpowers/plans/2026-07-12-main-thread-notifications.md
git commit -m "Document main-session notification policy"
```
