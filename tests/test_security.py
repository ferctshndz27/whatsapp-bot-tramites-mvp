from fastapi.testclient import TestClient

from app.main import app

# Sin context manager: no dispara el lifespan/startup (que tocaría MSSQL y el
# backfill); los middlewares se registran al importar la app, así que /health
# ya devuelve las cabeceras de seguridad.
client = TestClient(app)


def test_security_headers_present_on_every_response():
    r = client.get("/health")

    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "noindex" in r.headers["x-robots-tag"]  # el panel no se indexa
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_csp_blocks_framing_and_foreign_origins():
    csp = client.get("/health").headers["content-security-policy"]

    assert "frame-ancestors 'none'" in csp  # anti-clickjacking
    assert "default-src 'self'" in csp
