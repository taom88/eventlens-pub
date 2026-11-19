from __future__ import annotations

from eventlens.web import streamlit_app


def test_empty_panel_escapes_untrusted_html(monkeypatch) -> None:
    rendered: list[str] = []

    def markdown(value: str, **_: object) -> None:
        rendered.append(value)

    monkeypatch.setattr(streamlit_app.st, "markdown", markdown)
    streamlit_app._empty_panel(
        '<img src=x onerror="alert(1)">',
        "<script>window.location='bad'</script>",
    )

    assert "<script>" not in rendered[0]
    assert "<img" not in rendered[0]
    assert "&lt;script&gt;" in rendered[0]
    assert "&lt;img" in rendered[0]
