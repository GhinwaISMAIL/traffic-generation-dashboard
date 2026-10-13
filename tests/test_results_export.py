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


def test_latency_figure_uses_packet_percentiles():
    observed = pd.DataFrame({"ue": ["ue1", "ue1"], "direction": ["dl", "dl"],
                             "latency_ms_median": [10., 1000.]})
    packets = pd.DataFrame({"ue": ["ue1"], "direction": ["dl"],
                            "latency_ms_p50": [1000.], "latency_ms_p95": [2000.]})
    assert results_view._latency_fig(observed, packets).data[0].y[0] == 1000.
    tail = results_view._latency_fig(observed, packets, "p95")
    assert tail.data[0].y[0] == 2000.
    assert "p95" in tail.layout.title.text


def test_traffic_comparison_shares_scale_and_preserves_asymmetry():
    observed = pd.DataFrame({"app": ["youtube", "youtube"],
                             "direction": ["dl", "ul"], "recv_mbps": [.05, .005]})
    figure = results_view._traffic_comparison(observed)
    assert figure.data[0].y[0] / figure.data[1].y[0] == 10
    assert figure.data[0].yaxis == figure.data[1].yaxis


def test_missing_design_is_not_displayed_as_zero_plan():
    observed = pd.DataFrame({"ue": ["ue1"], "sent_packets": [10], "recv_packets": [9]})
    figure = results_view._designed_vs_realized_fig(observed, None)
    assert [trace.name for trace in figure.data] == ["Transmitted", "Received"]


def test_long_latency_uses_seconds():
    packets = pd.DataFrame({"ue": ["ue1"], "direction": ["dl"], "latency_ms_p50": [80000.]})
    figure = results_view._latency_fig(pd.DataFrame(), packets)
    assert figure.data[0].y[0] == 80
    assert figure.layout.yaxis.title.text == "Delay (s)"


def test_traffic_timeline_includes_idle_bins_and_preserves_payload():
    packets = pd.DataFrame({"direction": ["dl"], "received": [True],
                            "sent_time_utc": [1000.], "received_time_utc": [1001.],
                            "size_bytes": [1000]})
    figure = results_view._traffic_timeline(packets, 1000, 10, 1)
    received = next(trace for trace in figure.data if trace.name == "DL received")
    assert len(received.y) == 10
    assert sum(received.y) == .008
    assert received.y[-1] == 0
