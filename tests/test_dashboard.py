"""Tests for the Phase 3 static visualizer dashboard."""
from fastapi.testclient import TestClient

from app.main import app


def test_dashboard_root_serves_html():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "cytoscape" in resp.text.lower()
    assert "/api/v1/topology" in resp.text
    assert "Refresh Topology" in resp.text


def test_dashboard_static_assets_served():
    client = TestClient(app)
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "cytoscape" in js.text.lower()
    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert "#cy" in css.text
