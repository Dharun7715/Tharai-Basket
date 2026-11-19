"""Basic smoke test. Run with: python tests_smoke.py"""
import os
os.environ['SECRET_KEY'] = 'test-only-secret'
from app import create_app

app = create_app()
with app.test_client() as client:
    response = client.get('/')
    assert response.status_code == 200, response.status_code
    health = client.get('/health')
    assert health.status_code == 200
    assert health.json['status'] == 'ok'
print('Smoke tests passed: homepage and health endpoint.')
