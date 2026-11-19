"""Streamlit analyst console over the durable EventLens application layer."""

from __future__ import annotations

import atexit
from html import escape
import json
import os
from uuid import uuid4
from pathlib import Path
from typing import Any, Callable

import streamlit as st

from eventlens.config import EventLensSettings
from eventlens.web.presentation import (
    error_session_view,
    product_session_view,
)
from eventlens.web.jobs import BackgroundJobCoordinator, JobSnapshot, JobStatus, UnknownJobError
from eventlens.web.handoff import HandoffImportResult, HandoffView, SessionHandoffFacade
from eventlens.web.product_session import (
    ProductSession,
    ProductSessionFacade,
    SessionMode,
)
from eventlens.web.context_baseline import (
    ContextCardsView,
    DeterministicBaselineView,
    load_context_cards,
    load_deterministic_baseline,
)
from eventlens.web.artifact_trace import (
    ArtifactTraceReader,
    ArtifactTraceSpec,
    AuditTraceReader,
    TraceStatus,
)


def main() -> None:
    st.set_page_config(
        page_title="EventLens Analyst Console",
        page_icon="◉",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _styles()
    _defaults()
    coordinator: BackgroundJobCoordinator = st.session_state.job_coordinator
    coordinator.cleanup()
    base_settings = EventLensSettings.load(os.getenv("EVENTLENS_ENV_FILE", ".env"))
    settings = _settings_for_mode(base_settings, st.session_state.mode)
    product_service = ProductSessionFacade(
        settings,
        session_root=settings.workflow_artifact_dir / "product",
    )
    with st.sidebar:
        st.markdown('<div class="brand">EVENT<span>LENS</span></div>', unsafe_allow_html=True)
        st.caption("Earnings research protocol · decision ledger")
        st.selectbox("Research mode", ("Fixture", "Live"), key="mode")
        if st.session_state.mode == "Live":
            st.warning("Live mode can consume provider/API quota. No orders can be placed.")
        else:
            st.info("Synthetic data · quota-free")
        session = st.text_input("Resume session ID", value=st.session_state.session_id)
        if st.button("Resume durable session", use_container_width=True, disabled=not session):
            _run_action(
                lambda: product_service.load(session),
                session_id=session,
                result_kind="session_auto",
                action_key=f"resume:{session}",
            )
        st.divider()
        st.caption("Vibe-Trading is the laboratory. EventLens is the earnings-specific research protocol and decision ledger.")

    _header()
    _render_global_error()
    if _render_pending_job():
        return
    product_session = st.session_state.product_session
    if isinstance(product_session, ProductSession):
        try:
            product_session = product_service.load(product_session.session_id)
            st.session_state.product_session = product_session
        except Exception:
            # The already-validated in-memory view remains renderable; a safe
            # reload error is surfaced by an explicit resume action.
            pass
        _product_session_page(product_service, settings, product_session)
        return
    _new_session(product_service)


def _defaults() -> None:
    defaults = {
        "mode": "Fixture",
        "session_id": "",
        "pending_job_id": None,
        "pending_job_kind": None,
        "last_job_error": None,
        "last_job_notice": None,
        "product_session": None,
        "product_idempotency_key": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if "job_coordinator" not in st.session_state:
        st.session_state.job_coordinator = _shared_job_coordinator()


@st.cache_resource
def _shared_job_coordinator() -> BackgroundJobCoordinator:
    coordinator = BackgroundJobCoordinator()
    atexit.register(coordinator.shutdown, wait=False)
    return coordinator


def _settings_for_mode(settings: EventLensSettings, mode: str) -> EventLensSettings:
    """Bind the visible UI mode to the actual backend data mode."""
    if mode not in {"Fixture", "Live"}:
        raise ValueError(f"unsupported research mode: {mode}")
    return settings.model_copy(update={"data_mode": "live" if mode == "Live" else "fixture"})


def _header() -> None:
    left, right = st.columns([4, 1])
    with left:
        st.markdown('<div class="eyebrow">PRE-EARNINGS INTELLIGENCE</div>', unsafe_allow_html=True)
        st.title("Research with a memory.")
        st.caption("Independent evidence, explicit uncertainty, and a durable analyst decision trail.")
    with right:
        st.markdown('<div class="no-trade">DECISION SUPPORT<br><b>NO EXECUTION</b></div>', unsafe_allow_html=True)


def _new_session(product_service: ProductSessionFacade) -> None:
    st.subheader("Start a new research session")
    st.write("Describe the assignment. EventLens will draft candidates, but it will not choose or execute for you.")
    default = "Prepare DEMO earnings research and explain the research-only action."
    prompt = st.text_area("Analyst request", value=default, height=110)
    mode = (
        SessionMode.FIXTURE_REPLAY
        if st.session_state.mode == "Fixture"
        else SessionMode.LIVE
    )
    if mode is SessionMode.FIXTURE_REPLAY:
        st.info(
            "Key-free D0 replay: three readable DEMO context cards, a fixed "
            "deterministic baseline, and an external-agent pause. No provider or model calls."
        )
    else:
        st.warning(
            "Live mode may consume Finnhub/Alpha Vantage quota and uses yfinance. "
            "Missing provider readiness is shown without starting a partial run."
        )
    if st.button("Start product research", type="primary", use_container_width=True):
        if mode is SessionMode.FIXTURE_REPLAY:
            key = None
        elif st.session_state.product_idempotency_key is None:
            st.session_state.product_idempotency_key = f"web:{uuid4().hex}"
            key = st.session_state.product_idempotency_key
        else:
            key = st.session_state.product_idempotency_key
        _run_action(
            lambda: product_service.submit(
                prompt,
                mode,
                idempotency_key=key,
            ),
            result_kind="product_session",
            action_key=f"product:{mode.value}:{key or 'canonical'}",
        )


def _product_session_page(
    service: ProductSessionFacade,
    settings: EventLensSettings,
    session: ProductSession,
) -> None:
    view = product_session_view(session, settings)
    st.markdown(
        f'<div class="session-line"><span>{escape(view.ticker or session.ticker or "—")}</span>'
        f'<b>{escape(view.run_mode or "unknown")}</b><em>{escape(view.stage_status.label)}</em>'
        f'<code>{escape(view.session_id)}</code></div>',
        unsafe_allow_html=True,
    )
    columns = st.columns(5)
    columns[0].metric("Stage", view.stage_status.label)
    columns[1].metric("Origin", view.provider_origin or "Unknown")
    columns[2].metric("Provider readiness", "Ready" if session.readiness.ready else "Blocked")
    columns[3].metric("Prior cards", view.prior_card_coverage.available)
    columns[4].metric(
        "Agent route",
        session.handoff.route.value.title() if session.handoff else "Not started",
    )
    st.caption(session.readiness.detail)
    st.caption(
        f"Event: {view.event_id or 'unresolved'} · Cutoff: "
        f"{view.cutoff_at.isoformat() if view.cutoff_at else 'unavailable'} · "
        f"Persistence: {view.persistence_backend or 'unknown'}"
    )
    if view.warnings:
        st.warning("\n".join(view.warnings))
    if session.error is not None:
        st.error(f"{session.error.code}: {session.error.message}")

    cards = _product_context_view(session)
    baseline = _product_baseline_view(session)
    tabs = st.tabs(
        [
            "Overview",
            "Prior context",
            "Deterministic baseline",
            "External handoff",
            "Artifacts",
            "Unavailable stages",
        ]
    )
    with tabs[0]:
        st.markdown("### Research assignment")
        st.write(session.question)
        st.markdown("### Current product state")
        if session.status == "awaiting_external_agent":
            st.info(
                "The immutable bundle is ready. Download it, run your external agent, "
                "then explicitly upload the typed report. No external model was invoked automatically."
            )
        elif session.status == "validated":
            st.success(
                "The external-agent report passed deterministic validation. Later refined-MVP "
                "committee and QA stages remain unavailable in D0."
            )
        elif session.error:
            st.warning("The run stopped safely before producing research output.")
        elif session.status == "interrupted":
            st.warning(
                "The prior process ended during provider collection. EventLens did not "
                "repeat quota-consuming calls automatically."
            )
            if st.button("Retry as a new durable run", type="primary"):
                retry_key = f"web:{uuid4().hex}"
                st.session_state.product_idempotency_key = retry_key
                _run_action(
                    lambda: service.submit(
                        session.question,
                        SessionMode.LIVE,
                        idempotency_key=retry_key,
                    ),
                    result_kind="product_session",
                    action_key=f"product:live:{retry_key}",
                )
        else:
            st.info(f"Current status: {session.status}")
    with tabs[1]:
        _render_context_cards(cards)
    with tabs[2]:
        _render_baseline(baseline)
    with tabs[3]:
        root = (
            service.fixture_handoff_root
            if session.mode is SessionMode.FIXTURE_REPLAY
            else service.live_artifact_root / "external-agent"
        )
        _handoff_panel(
            settings,
            session.session_id,
            root=root,
            import_report=service.import_report,
        )
    with tabs[4]:
        _render_product_artifacts(service, session)
    with tabs[5]:
        st.warning(
            "D0 intentionally exposes unfinished stages instead of substituting generic prose."
        )
        for future in view.unavailable_future_stages:
            st.markdown(f"○ **{future.label}** — {future.reason}")


def _product_context_view(session: ProductSession) -> ContextCardsView:
    if not session.context_refs:
        return load_context_cards(None)
    payloads = [
        json.loads(reference.path.read_text(encoding="utf-8"))
        for reference in session.context_refs
    ]
    return load_context_cards(payloads)


def _product_baseline_view(
    session: ProductSession,
) -> DeterministicBaselineView | None:
    if session.baseline_ref is None:
        return None
    payload = json.loads(session.baseline_ref.path.read_text(encoding="utf-8"))
    if session.baseline_ref.subresource:
        payload = payload.get(session.baseline_ref.subresource)
    return load_deterministic_baseline(payload)


def _render_context_cards(view: ContextCardsView) -> None:
    st.markdown("### Prior-event memory")
    if not view.cards:
        _empty_panel("No prior cards", view.missing_reason or "No cards were retained.")
        return
    for card in view.cards:
        with st.expander(
            f"{card.event_date.isoformat()} · {card.ticker}",
            expanded=True,
        ):
            st.markdown("#### Executive summary")
            st.write(card.executive_summary)
            st.markdown("#### Key messages")
            for message in card.key_messages:
                st.markdown(f"- {message}")
            st.markdown("#### Metric and reaction interpretation")
            st.write(card.metric_interpretation)
            st.write(card.reaction_interpretation)
            st.markdown("#### Pre-event features")
            st.json(card.pre_event_features.model_dump(mode="json"))
            st.markdown("#### Canonical realized reaction")
            st.json(card.reaction_metrics.model_dump(mode="json"))
            st.caption(card.sample_size_caveat)
            if card.missing_values:
                st.markdown("#### Explicit missing values")
                for missing in card.missing_values:
                    st.caption(f"{missing.field}: {missing.reason}")
            if card.rights_warnings:
                st.warning("\n".join(card.rights_warnings))
            if card.regime_caveats:
                st.caption(" · ".join(card.regime_caveats))
            if card.provenance:
                st.markdown("#### Provenance")
                for source in card.provenance:
                    st.caption(source.citation)
            left, right = st.columns(2)
            left.download_button(
                "Download card JSON",
                card.json_download.content or b"",
                file_name=f"{card.card_id}.json",
                mime="application/json",
                key=f"card-json-{card.card_id}",
            )
            right.download_button(
                "Download card Markdown",
                card.markdown_download.content or b"",
                file_name=f"{card.card_id}.md",
                mime="text/markdown",
                key=f"card-md-{card.card_id}",
            )


def _render_baseline(view: DeterministicBaselineView | None) -> None:
    st.markdown("### Deterministic baseline")
    if view is None:
        _empty_panel("Baseline unavailable", "No persisted deterministic baseline exists.")
        return
    cols = st.columns(3)
    cols[0].metric(
        "Signed reaction (%)",
        str(view.signed_reaction.value_pct)
        if view.signed_reaction.value_pct is not None
        else "Unavailable",
    )
    cols[1].metric(
        "Absolute move (%)",
        str(view.absolute_reaction.value_pct)
        if view.absolute_reaction.value_pct is not None
        else "Unavailable",
    )
    cols[2].metric("Historical sample", view.sample_size)
    if view.confidence_interval.missing_reason:
        st.warning(view.confidence_interval.missing_reason)
    st.markdown("#### Benchmark and costs")
    st.write(f"Benchmark: {view.benchmark_supplement.summary}")
    st.write(f"Transaction costs: {view.transaction_supplement.summary}")
    st.markdown("#### Guardrails versus valuation")
    st.info(view.mechanical_guardrails.summary)
    st.warning(view.valuation.summary)
    left, right = st.columns(2)
    left.download_button(
        "Download baseline JSON",
        view.json_download.content or b"",
        file_name="deterministic-baseline.json",
        mime="application/json",
    )
    right.download_button(
        "Download baseline Markdown",
        view.markdown_download.content or b"",
        file_name="deterministic-baseline.md",
        mime="text/markdown",
    )


def _render_product_artifacts(
    service: ProductSessionFacade, session: ProductSession
) -> None:
    st.markdown("### Verified artifacts")
    records = tuple(
        ArtifactTraceReader(reference.path.parent).read(
            ArtifactTraceSpec(
                path=reference.path,
                artifact_id=f"{session.session_id}:{reference.kind}",
                artifact_type=reference.kind,
                expected_sha256=reference.sha256,
            )
        )
        for reference in sorted(
            session.artifact_refs, key=lambda item: (item.kind, item.path.name)
        )
    )
    for record in records:
        label = record.artifact_type.replace("_", " ").title()
        if record.status is TraceStatus.AVAILABLE:
            st.success(f"{label} · checksum verified")
            with st.expander(label):
                st.json(
                    {
                        "artifact_type": record.artifact_type,
                        "schema_version": record.schema_version,
                        "status": record.status.value,
                        "checksum": record.checksum,
                        "size_bytes": record.size_bytes,
                    }
                )
                if isinstance(record.content, str):
                    st.markdown(record.content)
                else:
                    st.json(record.content)
        else:
            st.warning(f"{label}: {record.warning or record.status.value}")
    if session.status == "awaiting_external_agent":
        st.info(
            "Expected but not yet available: validated external-agent report JSON "
            "and derived Markdown. Import a valid report to create them."
        )

    handoff_root = (
        service.fixture_handoff_root
        if session.mode is SessionMode.FIXTURE_REPLAY
        else service.live_artifact_root / "external-agent"
    )
    audit = AuditTraceReader(
        handoff_root / "audit.jsonl",
        root=handoff_root,
        session_id=session.session_id,
        event_scope_id=session.event_id,
    ).read()
    st.markdown("### Safe audit trace")
    st.json(
        {
            "status": audit.status.value,
            "events": [event.model_dump(mode="json") for event in audit.events],
            "warnings": audit.warnings,
        }
    )


def _handoff_panel(
    settings: EventLensSettings,
    session_id: str,
    *,
    root: Path | None = None,
    import_report: Callable[[str, bytes], HandoffImportResult] | None = None,
) -> None:
    """Render the route-safe external handoff surface.

    The panel is backed only by the UI-neutral façade.  It displays checksums
    and downloads bytes returned by that façade, never a persisted local path;
    importing a report is the sole mutation and remains explicit.
    """

    st.markdown("### External-agent handoff")
    if not session_id:
        _empty_panel("No session selected", "Start or resume a session to inspect its handoff.")
        return
    facade = SessionHandoffFacade(
        root or settings.workflow_artifact_dir / "external-agent", settings=settings
    )
    view = facade.view(session_id)
    _render_handoff_view(facade, view, import_report=import_report)


def _render_handoff_view(
    facade: SessionHandoffFacade,
    view: HandoffView,
    *,
    import_report: Callable[[str, bytes], HandoffImportResult] | None = None,
) -> None:
    route_label = view.route.value.title()
    cols = st.columns(4)
    cols[0].metric("Route", route_label)
    cols[1].metric("Route readiness", "Ready" if view.route_readiness.ready else "Not ready")
    cols[2].metric("Handoff status", view.status.replace("_", " ").title())
    cols[3].metric("Report", "Available" if view.report_available else "Pending")
    st.caption(view.route_readiness.detail)

    if view.failure is not None:
        if view.failure.code.value == "not_found":
            st.info(view.failure.message)
        else:
            st.warning(f"{view.failure.code.value}: {view.failure.message}")
        return

    if view.bundle_id:
        st.caption(f"Bundle {view.bundle_id} · revision {view.bundle_revision}")
    if view.checksums:
        st.markdown("#### Bundle checksums")
        st.code(
            "Content checksum (canonical bundle): "
            f"{view.checksums.content_sha256}\n"
            "File checksum (download bytes, including newline): "
            f"{view.checksums.file_sha256}"
        )
    if view.bundle_download is not None:
        st.download_button(
            "Download verified bundle",
            view.bundle_download.content,
            file_name=view.bundle_download.filename,
            mime=view.bundle_download.media_type,
            use_container_width=True,
        )

    if view.route.value == "deepseek":
        st.info(
            "DeepSeek is the configured internal route. Its result is produced by "
            "EventLens; external report upload is disabled."
        )
    elif view.upload_enabled and import_report is None:
        st.info(
            "Report import is available only from a repository-backed product session."
        )
    elif view.upload_enabled:
        upload = st.file_uploader(
            "Upload the external-agent JSON report",
            type=["json"],
            key=f"handoff-upload-{view.session_id}",
        )
        if st.button(
            "Validate and import report",
            type="primary",
            disabled=upload is None,
            key=f"handoff-import-{view.session_id}",
        ):
            assert upload is not None
            if upload.size > 2 * 1024 * 1024:
                st.error("Report upload exceeds the 2 MiB validation limit.")
            else:
                payload = upload.getvalue()
                _run_action(
                    lambda: import_report(view.session_id, payload),
                    session_id=view.session_id,
                    result_kind="handoff_import",
                    action_key=f"handoff-import:{view.session_id}",
                )
    elif view.validated:
        st.success("Validated report is persisted for this session.")

    if view.report_json_download is not None or view.report_markdown_download is not None:
        st.markdown("#### Validated report downloads")
        left, right = st.columns(2)
        if view.report_json_download is not None:
            left.download_button(
                "Download report JSON",
                view.report_json_download.content,
                file_name=view.report_json_download.filename,
                mime=view.report_json_download.media_type,
                use_container_width=True,
            )
        if view.report_markdown_download is not None:
            right.download_button(
                "Download report Markdown",
                view.report_markdown_download.content,
                file_name=view.report_markdown_download.filename,
                mime=view.report_markdown_download.media_type,
                use_container_width=True,
            )


def _run_action(
    action,
    *,
    session_id: str | None = None,
    result_kind: str = "document",
    action_key: str | None = None,
) -> JobSnapshot[Any] | None:
    """Queue an async action and return immediately to the Streamlit rerun.

    The operation factory is handed to the coordinator, so duplicate clicks
    do not create an unawaited coroutine.  The next rerun polls the snapshot
    and renders queued/running/terminal state.
    """

    coordinator: BackgroundJobCoordinator = st.session_state.job_coordinator
    key = action_key or f"session:{session_id or 'new'}:{result_kind}"
    try:
        snapshot = coordinator.submit(key, action)
        if not snapshot.duplicate:
            st.session_state.pending_job_id = snapshot.job_id
            st.session_state.pending_job_kind = result_kind
        elif st.session_state.pending_job_id != snapshot.job_id:
            # A durable session can be reopened from the sidebar while a
            # different action is active.  Keep the original result kind so
            # its completion is not misinterpreted by this click.
            st.session_state.pending_job_id = snapshot.job_id
            st.session_state.pending_job_kind = st.session_state.pending_job_kind or "document"
        if session_id:
            st.session_state.session_id = session_id
        if snapshot.duplicate:
            st.warning("An action for this session is already running; showing its existing progress.")
        st.rerun()
    except Exception as error:
        safe = error_session_view(error)
        st.error(safe.error or "Workflow action failed safely.")
        return None
    return snapshot


def _render_pending_job() -> bool:
    """Poll the current job and render progress, without waiting for it."""

    job_id = st.session_state.pending_job_id
    if not job_id:
        return False
    coordinator: BackgroundJobCoordinator = st.session_state.job_coordinator
    try:
        snapshot = coordinator.poll(job_id)
    except UnknownJobError:
        st.session_state.pending_job_id = None
        st.session_state.pending_job_kind = None
        st.session_state.last_job_error = "The background job record is no longer available."
        return False

    if snapshot.active:
        label = snapshot.message or snapshot.status.value.title()
        st.info(f"Workflow action {snapshot.status.value}: {label}.")
        if snapshot.progress is not None:
            st.progress(snapshot.progress, text=label)
        st.caption("The UI remains responsive while the workflow runs. Refresh to poll again.")
        if st.button("Refresh progress", key="refresh-background-job"):
            st.rerun()
        return True

    st.session_state.pending_job_id = None
    kind = st.session_state.pending_job_kind
    st.session_state.pending_job_kind = None
    try:
        if snapshot.status is JobStatus.SUCCEEDED:
            if kind in {"product_session", "session_auto"} and isinstance(
                snapshot.result, ProductSession
            ):
                st.session_state.product_session = snapshot.result
                st.session_state.session_id = snapshot.result.session_id
            elif kind == "handoff_import" and isinstance(
                snapshot.result, HandoffImportResult
            ):
                if snapshot.result.ok:
                    st.session_state.last_job_notice = (
                        "Report validated and persisted; the durable handoff was reopened."
                    )
                elif snapshot.result.failure is not None:
                    st.session_state.last_job_error = (
                        f"{snapshot.result.failure.code.value}: "
                        f"{snapshot.result.failure.message}"
                    )
        elif snapshot.status is JobStatus.FAILED:
            safe_error = error_session_view(snapshot.error or "Background action failed.")
            st.session_state.last_job_error = safe_error.error or "Background action failed."
        else:
            st.session_state.last_job_error = snapshot.message or snapshot.status.value
    finally:
        # Results are copied into session state above; remove the coordinator
        # record so a later submit for this session is allowed.
        try:
            coordinator.remove(job_id)
        except (UnknownJobError, ValueError):
            pass
    # Terminal state is consumed during this rerun, after the global message
    # area at the top of main() has already rendered. Surface it immediately.
    _render_global_error()
    return False


def _render_global_error() -> None:
    if st.session_state.last_job_notice:
        st.success(st.session_state.last_job_notice)
        st.session_state.last_job_notice = None
    if st.session_state.last_job_error:
        st.error(f"Product action failed safely: {st.session_state.last_job_error}")
        st.session_state.last_job_error = None


def _empty_panel(title: str, detail: str) -> None:
    st.markdown(
        f'<div class="empty"><b>{escape(title)}</b><br><span>{escape(detail)}</span></div>',
        unsafe_allow_html=True,
    )


def _styles() -> None:
    st.markdown(
        """
        <style>
        :root { --ink:#111827; --muted:#667085; --line:#dfe4ea; --paper:#f7f8f5; --acid:#c8f04a; --navy:#14213d; }
        .stApp { background:linear-gradient(180deg,#fbfbf8 0,#f4f6f2 100%); color:var(--ink); }
        [data-testid="stSidebar"] { background:#101827; color:white; border-right:1px solid #27344b; }
        [data-testid="stSidebar"] * { color:#e8edf5; }
        .brand { font:800 22px/1 sans-serif; letter-spacing:.12em; padding:12px 0 4px; }
        .brand span { color:var(--acid); }
        .eyebrow { font-size:11px; letter-spacing:.18em; font-weight:800; color:#527000; margin-top:6px; }
        .no-trade { text-align:right; color:#667085; font-size:11px; border:1px solid var(--line); padding:12px; background:white; }
        .no-trade b { color:#a03a28; letter-spacing:.08em; }
        .session-line { display:flex; gap:18px; align-items:center; background:var(--navy); color:white; padding:13px 16px; margin:10px 0 16px; border-radius:3px; }
        .session-line span { color:var(--acid); font-weight:800; font-size:18px; }
        .session-line em { font-style:normal; text-transform:uppercase; font-size:11px; letter-spacing:.08em; margin-left:auto; }
        .session-line code { color:#cfd7e6; background:transparent; }
        .empty { border:1px dashed #b9c1cc; background:rgba(255,255,255,.65); padding:38px; text-align:center; color:#344054; margin:16px 0; }
        .empty span { color:var(--muted); }
        .risk { display:inline-block; padding:7px 10px; font-weight:800; font-size:11px; letter-spacing:.1em; margin:5px 0; }
        .risk.review_required { background:#fff0c7; color:#7a4b00; }
        .risk.veto { background:#fee4e2; color:#9d1c1c; }
        .risk.none { background:#e8f7dc; color:#326513; }
        div[data-testid="stMetric"] { background:white; border:1px solid var(--line); padding:12px; }
        .stButton button[kind="primary"] { background:var(--navy); border-color:var(--navy); }
        .stButton button[kind="primary"]:hover { background:#22355f; border-color:#22355f; }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
