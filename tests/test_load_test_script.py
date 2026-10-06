"""The load-test script itself runs end to end (against the in-memory app)."""
import json, os
import httpx, pytest
from app.main import app
import scripts.load_test as lt

pytestmark = pytest.mark.asyncio

async def test_loadtest_script_against_in_memory_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async def no_db():
        class C:
            def close(self): pass
        return C()
    monkeypatch.setattr(lt, "_db", no_db)
    async def fake_banks(): return [{"code": "058", "name": "GTBank"}]
    monkeypatch.setattr("app.api.v1.endpoints.payments.list_banks", fake_banks)
    from app.core.rate_limit import rate_limiter
    orig = rate_limiter.hit
    monkeypatch.setattr(rate_limiter, "hit", lambda key, limit, window_seconds: None if key.startswith("global:") else orig(key, limit, window_seconds))
    await lt.seed(6, 2)
    real = httpx.AsyncClient
    monkeypatch.setattr(lt.httpx, "AsyncClient", lambda **kw: real(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30))
    await lt.run("http://test", 4)
    res = json.load(open("loadtest_results.json"))
    bad = {n: e["statuses"] for n, e in res["endpoints"].items() if any(int(k) >= 400 for k in e["statuses"])}
    print("ENDPOINTS", len(res["endpoints"]), "REQUESTS", res["requests"], "BAD", bad)
    assert not bad
