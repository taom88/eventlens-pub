from eventlens.graph import WorkflowStage
from eventlens.web.presentation import (
    SessionView,
    citation_label,
    error_session_view,
    redact_secrets,
    session_view,
)


def test_citation_renderer_is_passage_addressable() -> None:
    assert citation_label("prior-quarter-transcript", "capacity") == (
        "[prior-quarter-transcript · capacity]"
    )


def test_secret_redaction_is_recursive_and_does_not_render_values() -> None:
    payload = {
        "provider": "deepseek",
        "api_key": "do-not-render",
        "nested": {"Authorization": "Bearer do-not-render", "safe": "visible"},
    }
    redacted = redact_secrets(payload)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["Authorization"] == "[REDACTED]"
    assert redacted["nested"]["safe"] == "visible"
    assert "do-not-render" not in str(redacted)


def test_error_view_redacts_adversarial_secret_shapes() -> None:
    view = error_session_view(
        'request failed Authorization: Bearer abc.def token=xyz '
        'https://provider.test/path?api_key=query-secret\nraw_prompt: private thesis'
    )
    rendered = view.error or ""
    assert "abc.def" not in rendered
    assert "xyz" not in rendered
    assert "query-secret" not in rendered
    assert "private thesis" not in rendered

    exception_view = error_session_view(
        RuntimeError('{"api_key":"json-secret","provider_body":"private"}')
    )
    assert "json-secret" not in (exception_view.error or "")
    assert "private" not in (exception_view.error or "")


def test_error_view_is_typed_and_does_not_invent_event_values() -> None:
    view = error_session_view("provider timeout: api_key=should-not-render")

    assert isinstance(view, SessionView)
    assert view.is_error
    assert view.event is None
    assert view.stage is None
    assert view.error is not None
    # The error read model retains visibility of every possible future stage,
    # while never presenting a fabricated ticker or cutoff.
    assert WorkflowStage.COMPLETED in view.missing_future_stages
    assert "should-not-render" not in view.error


def test_view_from_state_preserves_cutoff_and_separates_rendering_from_domain() -> None:
    # A deliberately tiny test double is rejected by the typed builder: the
    # presentation boundary accepts validated WorkflowState only.  This also
    # guards against a future implementation quietly accepting Streamlit
    # session-state dictionaries here.
    try:
        session_view({"stage": "completed"})  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        raise AssertionError("presentation builder accepted an untyped state")

    # The helper remains pure for the empty-state case and carries no UI type.
    empty = session_view(None)
    assert empty.cutoff_at is None
    assert empty.provider_origin is None
    assert empty.persistence_backend is None
