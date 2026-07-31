import plotly.graph_objects as go
import pandas as pd

from dashboard import results_view


def test_figure_export_uses_vector_pdf(monkeypatch):
    observed = {}

    def fake_to_image(self, **kwargs):
        observed.update(kwargs)
        return b"%PDF-1.4 test"

    monkeypatch.setattr(go.Figure, "to_image", fake_to_image)

    payload = results_view._pdf_bytes(go.Figure())

    assert payload.startswith(b"%PDF")
    assert observed == {"format": "pdf", "width": 1100, "height": 600}


def test_efficiency_figure_removes_nullable_values():
    data = pd.DataFrame({
        "ue": ["ue1", "ue1"],
        "direction": ["dl", "dl"],
        "t_s": [0.0, 1.0],
        "bits_per_prb": pd.Series([125.0, pd.NA], dtype="object"),
    })

    figure = results_view._efficiency_fig(data, ["ue1"])

    assert list(figure.data[0].y) == [125.0]
    assert figure.data[0].y.dtype.kind == "f"
