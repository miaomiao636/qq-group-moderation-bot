# AI Rule Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the current correctness and safety blockers, then add database-backed dynamic rules, remote AI soft evidence, administrator feedback learning, and guarded official action orchestration.

**Architecture:** Keep QQ Official Bot as the primary message and official action channel. Keep moderation decisions deterministic: local rules, media analysis, AI evidence, and feedback candidates feed a policy merger, but no model or group message can directly execute actions or publish rules. Implement in phases so R-103 closes the immediate duplicate-processing, false-positive, media quota, and admin security risks before later AI and feedback features depend on it.

**Tech Stack:** Python 3.12, FastAPI server-rendered admin pages, SQLAlchemy async ORM, Alembic, SQLite WAL, pytest, mypy, ruff, httpx.

## Global Constraints

- Do not enable real revoke, mute, warn, or NapCat kick by default; development and tests must remain in `SHADOW`.
- Do not implement QQ risk-control evasion, device fingerprint spoofing, proxy rotation, or simulated-human behavior.
- Never write API keys, access tokens, passwords, cookies, private keys, real group secrets, or raw sensitive member content into source, Markdown, tests, logs, or Git history.
- All `/admin/*` pages require login. All state-changing admin POST routes require CSRF validation and write an audit record when the action changes persisted state.
- AI is disabled by default and enabled per group only. AI responses are soft evidence unless independent-review criteria are explicitly satisfied.
- Unknown manual recall is not ground truth. Unrecalled messages are unlabeled, not negative training examples. Candidate rules cannot auto-publish.
- Media failure, AI failure, missing rule snapshot, and uncertain external action results must degrade to record-only or manual review.
- Each phase must pass `uv run pytest`, `uv run mypy app`, `uv run ruff check app tests alembic`, and `uv run ruff format --check app tests alembic` before it is described as complete.

---

## File Structure

- `app/models.py`: extend shared persistence models for event leases and admin audit.
- `app/runtime/models.py`: make shadow decisions idempotent and trace rule/model versions.
- `app/moderation/rules.py`: replace high-risk hard-coded false-positive logic with context-aware deterministic evaluation; later consume rule snapshots.
- `app/moderation/dynamic_rules.py`: new typed runtime rule snapshot evaluator loaded from database.
- `app/moderation/ai.py`: new provider-neutral moderation DTOs and contracts.
- `app/adapters/ai/openai_compatible.py`: new OpenAI-compatible text and vision moderation adapter.
- `app/moderation/feedback.py`: new feedback labels, candidate mining, and replay service.
- `app/actions/orchestrator.py`: new guarded official action orchestration, defaulting to `SHADOW`.
- `app/adapters/qq_official/dedup.py`: atomic event claim, lease, retry, and token-aware completion.
- `app/adapters/qq_official/media.py`: SHA-256 filenames, stream quota accounting, `.part` handling, and bounded sniffing.
- `app/web/auth.py`: CSRF token generation and validation tied to admin session.
- `app/web/routes.py`: add CSRF fields, authenticated rules UI, feedback UI, and action-mode display.
- `alembic/versions/*.py`: migrations for event leases, admin audit, dynamic rules, feedback, AI cache, and action intents.
- `tests/test_r103.py`: correctness regressions for leases, permanent failures, media safety, rule false positives, and CSRF.
- `tests/test_dynamic_rules.py`: database-backed rule versioning, hot reload, publish, rollback, and conflict behavior.
- `tests/test_ai_moderation.py`: fixed fake-provider tests for text/vision, contract validation, timeout, budget, circuit breaker, and privacy.
- `tests/test_feedback_learning.py`: feedback labels, candidate generation, replay, no auto-publish, and poisoning protections.
- `tests/test_action_orchestrator.py`: official action mode gates, persisted intents, idempotency, protected-role blocking, unknown results, and no kick.
- `PROJECT_CONTEXT.md`, `NEXT_TASKS.md`, `PROGRESS.md`, `DECISIONS.md`, `HANDOFF.md`, `MEMORY_INDEX.md`, `README.md`: update after each completed phase.

---

### Task 1: R-103 Event Leases And Idempotent Shadow Decisions

**Files:**
- Modify: `app/models.py`
- Modify: `app/adapters/qq_official/dedup.py`
- Modify: `app/runtime/models.py`
- Modify: `app/runtime/pipeline.py`
- Create: `alembic/versions/15f0e4b7a901_r103_event_leases.py`
- Create: `tests/test_r103.py`

**Interfaces:**
- Produces: `ProcessingClaim(accepted: bool, token: str, status: str, reason: str)` in `app.adapters.qq_official.dedup`.
- Produces: `begin_processing(session, message_id, event_type="GROUP_MESSAGE_CREATE", lease_seconds=300) -> ProcessingClaim`.
- Produces: `mark_processed(session, message_id, token) -> bool`.
- Produces: `mark_failed(session, message_id, token, error, error_kind="retryable") -> bool`.
- Produces: `upsert_shadow_decision(session, **values) -> ShadowDecision` in `app.runtime.pipeline`.
- Consumes: existing `ProcessedEvent` and `ShadowDecision` tables.

- [ ] **Step 1: Write failing lease and idempotency tests**

```python
@pytest.mark.asyncio
async def test_processing_lease_blocks_second_claim() -> None:
    mid = f"LEASE_{uuid.uuid4().hex}"
    async with SessionLocal() as s1, SessionLocal() as s2:
        first = await begin_processing(s1, mid, lease_seconds=300)
        second = await begin_processing(s2, mid, lease_seconds=300)
    assert first.accepted is True
    assert first.token
    assert second.accepted is False
    assert second.status == "PROCESSING"


@pytest.mark.asyncio
async def test_permanent_parse_error_is_not_retried_or_duplicated() -> None:
    payload = {"id": f"BAD_{uuid.uuid4().hex}", "author": {"member_openid": "M_BAD"}}
    async with SessionLocal() as session:
        first = await run_pipeline(payload, session)
        second = await run_pipeline(payload, session)
    assert first is not None
    assert first.verdict == "record_only"
    assert second is None
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_r103.py::test_processing_lease_blocks_second_claim tests/test_r103.py::test_permanent_parse_error_is_not_retried_or_duplicated -q`

Expected: the first test shows two accepted claims under current semantics; the second exposes the duplicate `shadow_decisions.message_id` path.

- [ ] **Step 3: Add event lease columns and migration**

```python
lease_token: Mapped[str] = mapped_column(String(64), default="", index=True)
lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
attempts: Mapped[int] = mapped_column(default=0)
next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
error_kind: Mapped[str] = mapped_column(String(24), default="")
```

Migration must add the same columns with SQLite-safe defaults and preserve existing rows.

- [ ] **Step 4: Implement token-aware claims**

```python
@dataclass(frozen=True)
class ProcessingClaim:
    accepted: bool
    token: str
    status: str
    reason: str = ""
```

`begin_processing` must insert a fresh token for new rows, reject non-expired `PROCESSING`, allow expired `PROCESSING`, allow `FAILED` only after `next_retry_at`, and reject `DEAD` and `PROCESSED`.

- [ ] **Step 5: Make completion token-aware**

```python
async def mark_processed(session: AsyncSession, message_id: str, token: str) -> bool:
    row = await session.get(ProcessedEvent, message_id)
    if row is None or row.lease_token != token:
        return False
    row.status = "PROCESSED"
    row.error_message = ""
    row.error_kind = ""
    row.processed_at = datetime.now(UTC)
    await session.commit()
    return True
```

`mark_failed(..., error_kind="permanent")` must set `status="DEAD"` and no future retry. Retryable failures must set `status="FAILED"` and bounded `next_retry_at`.

- [ ] **Step 6: Upsert shadow decisions**

```python
async def upsert_shadow_decision(session: AsyncSession, **values: Any) -> ShadowDecision:
    existing = await session.scalar(select(ShadowDecision).where(ShadowDecision.message_id == values["message_id"]))
    if existing is None:
        existing = ShadowDecision(**values)
        session.add(existing)
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    await session.commit()
    return existing
```

Parse failures must call `mark_failed(..., error_kind="permanent")` after upsert, so repeated delivery is skipped and visible in the admin view.

- [ ] **Step 7: Run lease/idempotency tests**

Run: `uv run pytest tests/test_r103.py tests/test_dedup.py tests/test_shadow_pipeline.py -q`

Expected: all selected tests pass. Update older tests to inspect `ProcessingClaim.accepted` while preserving `check_and_mark` compatibility.

- [ ] **Step 8: Commit**

```bash
git add app/models.py app/adapters/qq_official/dedup.py app/runtime/models.py app/runtime/pipeline.py alembic/versions tests/test_r103.py tests/test_dedup.py tests/test_shadow_pipeline.py
git commit -m "fix: add leased event processing"
```

---

### Task 2: R-103 Media Safety And Rule False-Positive Fixes

**Files:**
- Modify: `app/adapters/qq_official/media.py`
- Modify: `app/moderation/rules.py`
- Modify: `tests/test_r103.py`
- Modify: `tests/test_moderation_rules.py`

**Interfaces:**
- Produces: `safe_filename(message_id: str, idx: int) -> str` based on SHA-256 digest, not truncated message ID.
- Produces: `download_attachment(..., quota_bytes: int = MEDIA_QUOTA_BYTES) -> tuple[str | None, str, str]`.
- Produces: context-aware rule helpers `_is_discussion_context(text)`, `_is_share_source_allowed(msg)`, `_has_ad_intent(text)`.
- Consumes: existing `StandardMessage.kind`, `StandardMessage.share_card`, attachments, and `TextRuleEngine.evaluate`.

- [ ] **Step 1: Write failing media tests**

```python
def test_safe_filename_uses_full_message_identity() -> None:
    a = "A" * 40 + "SAME_TAIL"
    b = "B" * 40 + "SAME_TAIL"
    assert safe_filename(a, 0) != safe_filename(b, 0)


@pytest.mark.asyncio
async def test_download_enforces_quota_during_stream(tmp_path: Path) -> None:
    (tmp_path / "existing.bin").write_bytes(b"12345678")
    client = FakeStreamClient([b"12345"])
    name, _ext, reason = await download_attachment(
        client, "https://example.invalid/a", tmp_path, "MSG", 0, "image/jpeg", quota_bytes=10
    )
    assert name is None
    assert "配额" in reason
    assert total_media_size(tmp_path) == 8
```

- [ ] **Step 2: Write failing context-rule tests**

```python
@pytest.mark.parametrize(
    "text",
    [
        "大家觉得兼职靠谱吗？我怕被骗",
        "学校让我们讨论刷单骗局，请大家不要上当",
        "我不做兼职，谢谢",
    ],
)
def test_discussion_and_negation_are_not_high_risk(text: str) -> None:
    msg = StandardMessage(message_id="CTX", group_openid="G", sender=Sender(member_openid="M"), text=text)
    assert TextRuleEngine().evaluate(msg).verdict != "violation_high"
```

- [ ] **Step 3: Run tests and confirm failure**

Run: `uv run pytest tests/test_r103.py::test_safe_filename_uses_full_message_identity tests/test_r103.py::test_download_enforces_quota_during_stream tests/test_moderation_rules.py::test_discussion_and_negation_are_not_high_risk -q`

Expected: the first and context tests fail on current code; quota test fails because current quota is checked only before download.

- [ ] **Step 4: Implement bounded media storage**

```python
def safe_filename(message_id: str, idx: int) -> str:
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    return f"{digest}_{idx}"


def sniff_file_head(path: Path, limit: int = 16) -> bytes:
    with path.open("rb") as f:
        return f.read(limit)
```

`stream_download` must include the current directory size plus `.part` bytes while streaming, delete `.part` on quota failure, and re-check before final replace.

- [ ] **Step 5: Implement context-aware rule evaluation**

```python
DISCUSSION_MARKERS = ("靠谱吗", "被骗", "骗局", "不要上当", "讨论", "不做", "拒绝")
AD_INTENT_MARKERS = ("招", "接单", "秒结", "日结", "长期", "加微", "私聊", "联系")

def _is_discussion_context(text: str) -> bool:
    normalized = apply_variants(text)
    return any(marker in normalized for marker in DISCUSSION_MARKERS)
```

Single words such as `兼职`, `刷单`, and `一单` must become weak signals unless paired with ad intent plus contact, QR/media hit, bad domain, or repeated behavior. Porn, gambling, weapons, drugs, and confirmed malicious domains remain high-risk categories, but protected roles still record only.

- [ ] **Step 6: Fix R006 share-card behavior**

```python
ALLOWED_SHARE_SOURCES = ("万能校园墙",)

def _is_share_source_allowed(msg: StandardMessage) -> bool:
    card = msg.share_card
    source = (card.source if card else "") or msg.text
    return any(source_name in source for source_name in ALLOWED_SHARE_SOURCES)
```

Allowed share sources return `allow` when no other hard illegal evidence exists. Unknown share cards return `record_only`. Only share cards with explicit引流/contact/malicious-domain evidence can become high-risk.

- [ ] **Step 7: Run selected tests**

Run: `uv run pytest tests/test_r103.py tests/test_moderation_rules.py tests/test_r102.py -q`

Expected: selected tests pass; any old “share card always high-risk” expectation must be updated to the approved source-aware behavior.

- [ ] **Step 8: Commit**

```bash
git add app/adapters/qq_official/media.py app/moderation/rules.py tests/test_r103.py tests/test_moderation_rules.py tests/test_r102.py
git commit -m "fix: harden media storage and rule context"
```

---

### Task 3: Admin CSRF And Audit Foundation

**Files:**
- Modify: `app/models.py`
- Modify: `app/web/auth.py`
- Modify: `app/web/routes.py`
- Create: `alembic/versions/2c8a41d9f0b7_admin_audit_csrf.py`
- Modify: `tests/test_admin_web.py`

**Interfaces:**
- Produces: `auth.csrf_token(session_token: str) -> str`.
- Produces: `auth.validate_csrf(session_token: str | None, submitted: str | None) -> bool`.
- Produces: `AdminAudit` table with `id`, `operator`, `action`, `target_type`, `target_id`, `detail_json`, and `created_at`.
- Produces: `record_admin_audit(session, operator, action, target_type, target_id, details) -> None` helper in `app.web.routes` or `app.web.audit`.

- [ ] **Step 1: Write failing CSRF tests**

```python
def test_admin_rules_requires_login(client: TestClient) -> None:
    resp = client.get("/admin/rules", follow_redirects=True)
    assert "管理后台登录" in resp.text


def test_state_changing_post_requires_csrf(logged_in: TestClient) -> None:
    resp = logged_in.post("/admin/groups/alias", data={"group_openid": "G", "name": "测试群"})
    assert resp.status_code in (400, 403)
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_admin_web.py::test_admin_rules_requires_login tests/test_admin_web.py::test_state_changing_post_requires_csrf -q`

Expected: `/admin/rules` is currently public and POST routes accept no CSRF.

- [ ] **Step 3: Add CSRF helpers**

```python
_CSRF_TOKENS: dict[str, str] = {}

def csrf_token(session_token: str) -> str:
    token = _CSRF_TOKENS.get(session_token)
    if token is None:
        token = secrets.token_urlsafe(24)
        _CSRF_TOKENS[session_token] = token
    return token


def validate_csrf(session_token: str | None, submitted: str | None) -> bool:
    return bool(session_token and submitted) and secrets.compare_digest(
        _CSRF_TOKENS.get(session_token, ""), submitted
    )
```

- [ ] **Step 4: Add CSRF enforcement in every state-changing POST**

Every admin POST except `/admin/login` must call a shared guard:

```python
async def _require_admin_post(request: Request, csrf: str) -> str | None:
    token = await _require_login(request)
    if not token:
        return None
    if not auth.validate_csrf(token, csrf):
        raise HTTPException(403, "CSRF token invalid")
    return token
```

Forms must include `<input type=hidden name=csrf value="...">`.

- [ ] **Step 5: Add admin audit persistence**

Persist audit rows for alias save, cleanup, case state changes, future rules, feedback, and action-mode changes. Do not log raw message text or secrets in `detail_json`.

- [ ] **Step 6: Run admin tests**

Run: `uv run pytest tests/test_admin_web.py -q`

Expected: all admin tests pass with updated fixtures extracting CSRF from pages before POST.

- [ ] **Step 7: Commit**

```bash
git add app/models.py app/web/auth.py app/web/routes.py alembic/versions tests/test_admin_web.py
git commit -m "fix: require csrf for admin mutations"
```

---

### Task 4: T-105 Database-Backed Dynamic Rules

**Files:**
- Create: `app/moderation/dynamic_rules.py`
- Modify: `app/moderation/rules.py`
- Modify: `app/runtime/models.py`
- Modify: `app/runtime/pipeline.py`
- Modify: `app/web/routes.py`
- Create: `alembic/versions/8ab12f3d9c60_t105_dynamic_rules.py`
- Create: `tests/test_dynamic_rules.py`

**Interfaces:**
- Produces: `RuleScope = Literal["global", "group"]`.
- Produces: `RuleItemType = Literal["keyword", "phrase_combo", "domain", "share_source", "qr_payload", "media_hash", "contact_combo", "behavior_threshold"]`.
- Produces: `RuleSnapshot(version_id: int, scope_key: str, items: tuple[RuntimeRuleItem, ...])`.
- Produces: `load_active_snapshot(session, group_openid: str | None) -> RuleSnapshot`.
- Produces: `DynamicRuleEngine(snapshot: RuleSnapshot).evaluate(msg: StandardMessage) -> ModerationDecision`.
- Consumes: existing `TextRuleEngine` for fallback during migration.

- [ ] **Step 1: Write failing dynamic-rule tests**

```python
@pytest.mark.asyncio
async def test_draft_rule_does_not_affect_runtime() -> None:
    async with SessionLocal() as session:
        draft = await create_rule_draft(session, scope="global", scope_key="*", name="test")
        await add_rule_item(session, draft.id, item_type="keyword", pattern="仅草稿违规词", category="ad", weight=0.9)
        snapshot = await load_active_snapshot(session, None)
    decision = DynamicRuleEngine(snapshot).evaluate(make_text("仅草稿违规词"))
    assert decision.verdict != "violation_high"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_dynamic_rules.py -q`

Expected: import errors for new service and models.

- [ ] **Step 3: Add rule tables and migration**

Create SQLAlchemy models for `RuleSet`, `RuleVersion`, `RuleItem`, and `RuleAudit`. `RuleVersion.status` must be one of `DRAFT`, `ACTIVE`, `ARCHIVED`; active version per scope must be enforced by service logic because SQLite partial uniqueness is kept optional.

- [ ] **Step 4: Implement safe rule DTO validation**

Reject unsupported rule types, empty patterns, patterns longer than 200 chars, weights outside `0.0..1.0`, arbitrary regex syntax, SQL-looking payloads, and action fields. Rule items may produce evidence and verdict pressure only.

- [ ] **Step 5: Seed built-in rules as initial active version**

During migration or first service call, create a global `ACTIVE` version from the current approved constants, excluding known false-positive hard words that were demoted in R-103. Seed must be idempotent.

- [ ] **Step 6: Runtime evaluation uses active snapshot**

`TextRuleEngine` should accept an optional `RuleSnapshot`. Existing call sites may omit it and use a built-in safe snapshot for tests. `run_pipeline` must load the active snapshot and store `rule_version_id` in `ShadowDecision.detail_json` or a dedicated nullable column if migration added it.

- [ ] **Step 7: Add admin draft/edit/simulate/publish/rollback routes**

Routes must require login and CSRF. Publishing must archive the old active version, activate the selected draft atomically, write `RuleAudit`, and show a preview page before commit.

- [ ] **Step 8: Add hot reload cache**

Implement a small cache with a maximum 5-second TTL. On corrupted snapshot, continue using the last valid snapshot and record an audit or log warning.

- [ ] **Step 9: Run selected tests**

Run: `uv run pytest tests/test_dynamic_rules.py tests/test_moderation_rules.py tests/test_shadow_pipeline.py tests/test_admin_web.py -q`

Expected: all selected tests pass.

- [ ] **Step 10: Commit**

```bash
git add app/moderation/dynamic_rules.py app/moderation/rules.py app/runtime/models.py app/runtime/pipeline.py app/web/routes.py alembic/versions tests/test_dynamic_rules.py tests/test_moderation_rules.py tests/test_shadow_pipeline.py tests/test_admin_web.py
git commit -m "feat: add versioned dynamic rules"
```

---

### Task 5: T-204 Remote Text And Vision AI Soft Evidence

**Files:**
- Create: `app/moderation/ai.py`
- Create: `app/adapters/ai/__init__.py`
- Create: `app/adapters/ai/openai_compatible.py`
- Modify: `app/config.py`
- Modify: `app/moderation/review_gate.py`
- Modify: `app/runtime/pipeline.py`
- Create: `alembic/versions/44de03b8a12f_t204_ai_cache.py`
- Create: `tests/test_ai_moderation.py`

**Interfaces:**
- Produces: `AIModerationRequest`, `AIModerationResult`, `AIProviderError`, `TextModerator`, `VisionModerator`.
- Produces: `OpenAICompatibleTextModerator` and `OpenAICompatibleVisionModerator`.
- Produces: `AI_ENABLED`, `AI_BASE_URL`, `AI_TEXT_MODEL`, `AI_VISION_MODEL`, `AI_API_KEY`, `AI_TIMEOUT_SECONDS`, `AI_DAILY_BUDGET_CENTS`, `AI_PER_MINUTE_LIMIT` settings.
- Consumes: existing `ReviewGate`, `CircuitBreaker`, and `CostLedger` concepts from T-203.

- [ ] **Step 1: Write fake-provider contract tests**

```python
def test_single_ai_result_is_soft_evidence_only() -> None:
    result = AIModerationResult(category="ad", confidence=0.99, evidence="疑似广告", model_id="fake", prompt_version="v1")
    decision = merge_ai_evidence(local_allow_decision(), [result])
    assert decision.verdict == "record_only"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_ai_moderation.py -q`

Expected: imports fail for the new AI contracts.

- [ ] **Step 3: Add typed AI contracts**

`AIModerationResult` must reject categories outside the project set, confidence outside `0.0..1.0`, missing model ID, non-JSON provider payloads, and any action-like fields such as `kick`, `recall`, `mute`, `sql`, or `command`.

- [ ] **Step 4: Implement OpenAI-compatible adapter**

Use `httpx.AsyncClient` with configured timeout. Request payload must include only sanitized text/OCR/ASR/card fields or explicitly selected frame/image bytes. The API key must be read at runtime from settings or environment and never logged.

- [ ] **Step 5: Add budget, rate limit, cache, and circuit breaker**

Cache key must include sanitized content digest, media digest, model ID, prompt version, and rule version ID. On disabled AI, missing key, timeout, limit, invalid JSON, or open circuit, return a typed degraded result and keep the moderation chain running.

- [ ] **Step 6: Integrate into pipeline as soft evidence**

AI runs only when enabled for the group and local deterministic evidence is not already enough. Single-model results set `record_only`. Independent-review model confirmation may set `violation_high` only when configured and tested.

- [ ] **Step 7: Run AI tests**

Run: `uv run pytest tests/test_ai_moderation.py tests/test_shadow_pipeline.py tests/test_review_gate.py -q`

Expected: all selected tests pass without real API calls or API keys.

- [ ] **Step 8: Commit**

```bash
git add app/moderation/ai.py app/adapters/ai app/config.py app/moderation/review_gate.py app/runtime/pipeline.py alembic/versions tests/test_ai_moderation.py tests/test_shadow_pipeline.py tests/test_review_gate.py
git commit -m "feat: add remote ai soft evidence"
```

---

### Task 6: T-205 Feedback Learning And Candidate Rules

**Files:**
- Create: `app/moderation/feedback.py`
- Modify: `app/web/routes.py`
- Modify: `app/moderation/dynamic_rules.py`
- Create: `alembic/versions/73cf49a201e8_t205_feedback_learning.py`
- Create: `tests/test_feedback_learning.py`

**Interfaces:**
- Produces: `FeedbackLabel = Literal["confirmed_violation", "confirmed_normal", "false_positive", "unknown_recall", "other_recall"]`.
- Produces: `record_feedback(session, message_id, label, category, operator, reason) -> FeedbackRecord`.
- Produces: `mine_rule_candidates(session, min_messages=3, min_members=2) -> list[RuleCandidate]`.
- Produces: `replay_candidate(session, candidate_id) -> ReplayReport`.
- Consumes: T-105 dynamic-rule draft service.

- [ ] **Step 1: Write label semantics tests**

```python
@pytest.mark.asyncio
async def test_unknown_recall_is_not_positive_truth() -> None:
    async with SessionLocal() as session:
        await record_feedback(session, "MSG1", "unknown_recall", "ad", "web:test", "管理员撤回但原因未知")
        candidates = await mine_rule_candidates(session)
    assert candidates == []
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_feedback_learning.py -q`

Expected: imports fail for new feedback service.

- [ ] **Step 3: Add feedback and candidate tables**

Add `feedback_records`, `rule_candidates`, and `rule_candidate_examples`. Store message IDs, labels, categories, operator, reason, structured candidate JSON, support counts, conflict counts, replay report JSON, and timestamps. Do not store raw sensitive message content beyond existing retention policy.

- [ ] **Step 4: Implement feedback recording and admin UI**

Admins can mark recorded messages and cases as confirmed violation, confirmed normal, false positive, unknown recall, or other recall. Every POST requires CSRF and writes `AdminAudit`.

- [ ] **Step 5: Implement candidate mining**

Extract repeated phrases, domains, contact combinations, QR payloads, share sources, media hashes, and behavior patterns from confirmed feedback only. Default threshold: at least 3 independent messages from at least 2 members.

- [ ] **Step 6: Implement replay**

Replay candidates over retained messages and report positive coverage, confirmed negative conflicts, unlabeled impact preview, and conflicts with allow rules. Candidates can only be copied into a T-105 draft; they cannot publish themselves.

- [ ] **Step 7: Run feedback tests**

Run: `uv run pytest tests/test_feedback_learning.py tests/test_dynamic_rules.py tests/test_admin_web.py -q`

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add app/moderation/feedback.py app/web/routes.py app/moderation/dynamic_rules.py alembic/versions tests/test_feedback_learning.py tests/test_dynamic_rules.py tests/test_admin_web.py
git commit -m "feat: add feedback rule candidates"
```

---

### Task 7: T-106 Guarded Official Action Orchestration

**Files:**
- Create: `app/actions/__init__.py`
- Create: `app/actions/orchestrator.py`
- Modify: `app/config.py`
- Modify: `app/models.py`
- Modify: `app/runtime/pipeline.py`
- Create: `alembic/versions/a0b4d72e5f31_t106_action_intents.py`
- Create: `tests/test_action_orchestrator.py`

**Interfaces:**
- Produces: `ActionMode = Literal["SHADOW", "OFFICIAL"]`.
- Produces: `ActionIntent` table with idempotency key, action, target, message, status, external result, and timestamps.
- Produces: `orchestrate_actions(session, msg, decision, official_client=None) -> list[ActionIntent]`.
- Consumes: existing QQ official action adapter for recall, mute, and warn only.

- [ ] **Step 1: Write default-shadow tests**

```python
@pytest.mark.asyncio
async def test_default_shadow_never_calls_official_client() -> None:
    client = FakeOfficialClient()
    async with SessionLocal() as session:
        intents = await orchestrate_actions(session, msg(), high_decision(), official_client=client)
    assert intents == []
    assert client.calls == []
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_action_orchestrator.py -q`

Expected: imports fail for new orchestration module.

- [ ] **Step 3: Add action-intent table and settings**

Settings must keep `ACTION_MODE="SHADOW"` by default. `OFFICIAL` must require explicit production configuration, migrated database, admin password, QQ credentials, emergency stop not active, and tests may assert missing prerequisites are rejected.

- [ ] **Step 4: Implement idempotent action intents**

Persist each intended `recall`, `mute`, or `warn` before calling QQ. Never create `kick`. Protected sender, whitelist, missing DB commit, emergency stop, or unsupported action yields no external call and a recorded reason.

- [ ] **Step 5: Implement unknown-result handling**

Timeout or ambiguous external exceptions must set `UNKNOWN` and route to manual review. Known success becomes `SUCCEEDED`; known permanent failure becomes `FAILED`. Unknown actions are not blindly replayed on process restart.

- [ ] **Step 6: Run orchestrator tests**

Run: `uv run pytest tests/test_action_orchestrator.py tests/test_actions.py tests/test_cases.py tests/test_shadow_pipeline.py -q`

Expected: all selected tests pass; default tests prove external call count is zero.

- [ ] **Step 7: Commit**

```bash
git add app/actions app/config.py app/models.py app/runtime/pipeline.py alembic/versions tests/test_action_orchestrator.py tests/test_actions.py tests/test_cases.py tests/test_shadow_pipeline.py
git commit -m "feat: add guarded official action orchestration"
```

---

### Task 8: Documentation, Migration Cycle, And Full Gate

**Files:**
- Modify: `AGENTS.md`
- Modify: `MEMORY_INDEX.md`
- Modify: `PROJECT_CONTEXT.md`
- Modify: `NEXT_TASKS.md`
- Modify: `PROGRESS.md`
- Modify: `DECISIONS.md`
- Modify: `HANDOFF.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: committed results from Tasks 1-7.
- Produces: updated project memory that accurately separates implemented, shadow-only, config-gated, and unverified real-world behavior.

- [ ] **Step 1: Run full validation**

Run:

```bash
uv run pytest
uv run mypy app
uv run ruff check app tests alembic
uv run ruff format --check app tests alembic
uv run alembic downgrade base
uv run alembic upgrade head
git diff --check
```

Expected: all commands pass locally. Real QQ, real MiMo, and real Windows service tests are not claimed unless separately performed by the project owner on the Windows test machine.

- [ ] **Step 2: Inspect Git scope**

Run: `git status --short` and `git diff --stat`

Expected: only planned code, migrations, tests, and project docs changed.

- [ ] **Step 3: Update project memory**

Docs must state:

```text
R-103 implemented and locally verified.
T-105 implemented with database-backed editable rules and hot reload.
T-204 implemented as disabled-by-default AI soft evidence with fake-provider tests only.
T-205 implemented as human-approved feedback candidates, no auto-publish.
T-106 implemented as guarded orchestration, default SHADOW, no real QQ action enabled by code sync.
Windows W3/W5 and real MiMo validation remain external validation items.
```

- [ ] **Step 4: Commit docs**

```bash
git add AGENTS.md MEMORY_INDEX.md PROJECT_CONTEXT.md NEXT_TASKS.md PROGRESS.md DECISIONS.md HANDOFF.md README.md
git commit -m "docs: update rule learning implementation status"
```

- [ ] **Step 5: Final verification before handoff**

Run: `git log --oneline -8`, `git status --short --branch`, and a secret-pattern scan over staged and committed diffs.

Expected: branch is clean except intentional unpushed commits; no secrets are present.

## Self-Review

Spec coverage: R-103 is covered by Tasks 1-3. Dynamic rules are covered by Task 4. Remote AI is covered by Task 5. Feedback learning is covered by Task 6. Official action orchestration is covered by Task 7. Documentation and full gates are covered by Task 8.

Specificity scan: This plan avoids open-ended marker text and uses fixed Alembic revision filenames for every migration file named above.

Type consistency: The interfaces introduced by earlier tasks are named consistently in later tasks: `ProcessingClaim`, `RuleSnapshot`, `AIModerationResult`, `FeedbackRecord`, `RuleCandidate`, and `ActionIntent`.

Execution choice for this task: the project rules currently forbid proactive sub-agent delegation unless the user explicitly asks for it, and the user has already confirmed “开始实现”. Use Inline Execution in this session and implement the tasks sequentially with review checkpoints after each commit.
