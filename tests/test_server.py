"""tests for dashboard/server.py route handling"""
import json, pathlib, sys, threading, time
from http.client import HTTPConnection

# Add project paths
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'dashboard'))
sys.path.insert(0, str(ROOT / 'scripts'))


def test_healthz(tmp_path):
    """GET /healthz returns 200 with status ok."""
    # Create minimal data dir
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    (data_dir / 'live_status.json').write_text('{}')
    (data_dir / 'agent_config.json').write_text('{}')

    # Import and patch server
    import server as srv
    srv.DATA = data_dir

    from http.server import HTTPServer
    port = 18971

    httpd = HTTPServer(('127.0.0.1', port), srv.Handler)
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()

    time.sleep(0.1)
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('GET', '/healthz')
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()

    assert resp.status == 200
    assert body['status'] in ('ok', 'degraded')

    httpd.server_close()


def test_live_status_triggers_auto_refresh(tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    (data_dir / 'live_status.json').write_text(json.dumps({'tasks': []}))
    (data_dir / 'tasks_source.json').write_text('[]')
    (data_dir / 'agent_config.json').write_text('{}')

    import server as srv
    srv.DATA = data_dir

    def fake_refresh(force=False, max_age_sec=15):
        (data_dir / 'live_status.json').write_text(json.dumps({'tasks': [{'id': 'JJC-TEST-001'}]}))

    monkeypatch.setattr(srv, 'ensure_dashboard_data_fresh', fake_refresh)

    from http.server import HTTPServer
    port = 18972

    httpd = HTTPServer(('127.0.0.1', port), srv.Handler)
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()

    time.sleep(0.1)
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('GET', '/api/live-status')
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()

    assert resp.status == 200
    assert body['tasks'][0]['id'] == 'JJC-TEST-001'

    httpd.server_close()
