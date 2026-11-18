"""Offline tests for hosting: which sites may call the API, the message limit, and the page's API address.
Who sees which chats (accounts, sign-in) is in test_auth.py.

    python -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import access, auth, config, server  # noqa: E402

NETLIFY = 'https://ramplacesearch.netlify.app'
CHAT = {'title': 'gyms', 'updated': 1, 'log': [{'u': 'hi'}]}
PASSWORD = 'correct horse battery'


class Hosted(unittest.TestCase):
    """The app as a public deployment runs it: a page on Netlify, an API of its own, everyone signed in."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        access._hits.clear()
        self.patches = [mock.patch.object(config, 'DATA_DIR', Path(self.folder.name)), mock.patch.object(config, 'ALLOWED_ORIGINS', [NETLIFY]),
                        mock.patch.object(config, 'CHAT_RATE_PER_HOUR', 0)]
        for p in self.patches:
            p.start()
        self.client = TestClient(server.create_app())
        self.alice = {'Authorization': 'Bearer ' + self.client.post('/api/auth/signup', json={'email': 'alice@example.com', 'password': PASSWORD}).json()['token']}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.folder.cleanup()

    def put(self, headers=None, chat_id='chat00000001', **extra):
        return self.client.put(f'/api/chats/{chat_id}', json=CHAT, headers={**(headers or self.alice), **extra})

    # ---- which sites ----
    def test_the_page_on_netlify_may_call_the_api(self):
        response = self.put(Origin=NETLIFY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['access-control-allow-origin'], NETLIFY)

    def test_the_browsers_preflight_is_answered_for_netlify_only(self):
        ask = {'Access-Control-Request-Method': 'PUT', 'Access-Control-Request-Headers': 'content-type,authorization'}
        ok = self.client.options('/api/chats/chat00000001', headers={'Origin': NETLIFY, **ask})
        self.assertEqual(ok.status_code, 200)
        self.assertIn('authorization', ok.headers['access-control-allow-headers'].lower())
        self.assertEqual(self.client.options('/api/chats/chat00000001', headers={'Origin': 'https://evil.example', **ask}).status_code, 400)

    def test_signing_in_works_from_the_page_on_netlify(self):
        for path, body in (('/api/auth/signup', {'email': 'bob@example.com', 'password': PASSWORD}), ('/api/auth/login', {'email': 'bob@example.com', 'password': PASSWORD})):
            response = self.client.post(path, json=body, headers={'Origin': NETLIFY})
            self.assertEqual((response.status_code, response.headers['access-control-allow-origin']), (200, NETLIFY), path)

    def test_another_site_is_refused(self):
        self.assertEqual(self.put(Origin='https://evil.example').status_code, 403)
        self.assertEqual(self.client.post('/api/chat', json={'thread_id': 'chat00000001', 'message': 'hi'},
                                          headers={**self.alice, 'Origin': 'https://evil.example'}).status_code, 403)
        for path in ('/api/auth/login', '/api/auth/signup', '/api/auth/google'):
            self.assertEqual(self.client.post(path, json={'email': 'x@example.com', 'password': PASSWORD}, headers={'Origin': 'https://evil.example'}).status_code, 403, path)

    def test_the_site_serving_the_api_is_always_allowed_over_http_or_https(self):
        for origin in ('http://testserver', 'https://testserver'):
            self.assertEqual(self.put(Origin=origin).status_code, 200, origin)

    # ---- how often ----
    def test_message_limit_per_account(self):
        def fake(thread, text):
            yield {'type': 'done', 'waiting': False}
        body = {'thread_id': 'chat00000001', 'message': 'hi'}
        with mock.patch.object(config, 'CHAT_RATE_PER_HOUR', 2), mock.patch.object(server, 'stream_events', fake):
            codes = [self.client.post('/api/chat', json=body, headers=self.alice).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_the_limit_is_per_account_not_shared(self):
        def fake(thread, text):
            yield {'type': 'done', 'waiting': False}
        bob = {'Authorization': 'Bearer ' + self.client.post('/api/auth/signup', json={'email': 'bob@example.com', 'password': PASSWORD}).json()['token']}
        with mock.patch.object(config, 'CHAT_RATE_PER_HOUR', 1), mock.patch.object(server, 'stream_events', fake):
            first = self.client.post('/api/chat', json={'thread_id': 'chat00000001', 'message': 'hi'}, headers=self.alice).status_code
            second = self.client.post('/api/chat', json={'thread_id': 'chat00000002', 'message': 'hi'}, headers=bob).status_code
        self.assertEqual((first, second), (200, 200))

    def test_the_limit_is_off_unless_set(self):
        def fake(thread, text):
            yield {'type': 'done', 'waiting': False}
        with mock.patch.object(server, 'stream_events', fake):
            codes = {self.client.post('/api/chat', json={'thread_id': 'chat00000001', 'message': 'hi'}, headers=self.alice).status_code for _ in range(5)}
        self.assertEqual(codes, {200})


class Local(unittest.TestCase):
    """The defaults: one person on their own machine, no other site, no limit, no Google, no seeded accounts."""

    def test_defaults(self):
        self.assertEqual((config.CHAT_RATE_PER_HOUR, config.ALLOWED_ORIGINS, config.HOST), (0, [], '127.0.0.1'))

    def test_no_cors_headers_when_no_other_site_is_allowed(self):
        client = TestClient(server.create_app())
        response = client.get('/api/health', headers={'Origin': NETLIFY})
        self.assertNotIn('access-control-allow-origin', response.headers)


class Page(unittest.TestCase):
    def test_the_page_reads_its_api_address_from_config_js(self):
        client = TestClient(server.create_app())
        html = client.get('/').text
        self.assertLess(html.index('/config.js'), html.index('/app.js'))
        self.assertLess(html.index('/config.js'), html.index('/places.js'))
        self.assertLess(html.index('/auth.js'), html.index('/app.js'))  # app.js signs in through Auth
        self.assertIn("window.SCOUT_API_BASE = ''", client.get('/config.js').text)  # empty: the same site serves the API

    def test_every_api_call_from_the_page_goes_through_auth_js(self):
        """A plain fetch() would reach the API without the account's token and get a 401 (this once hid the results of a search)."""
        import re
        for name in ('app.js', 'places.js'):
            found = re.search(r'(?<![.\w])fetch\(', (Path(server.STATIC) / name).read_text(encoding='utf-8'))
            self.assertIsNone(found, f'{name} calls fetch() directly: use Auth.fetch')
        self.assertIn('Authorization', (Path(server.STATIC) / 'auth.js').read_text(encoding='utf-8'))

    def test_the_page_is_a_complete_site_of_its_own(self):
        """static/ can be published on its own (Netlify): everything index.html loads is a file in it."""
        import re
        folder = Path(server.STATIC)
        html = (folder / 'index.html').read_text(encoding='utf-8')
        for path in re.findall(r'(?:src|href)="(/[^"]+)"', html):
            self.assertTrue((folder / path.lstrip('/')).is_file(), path)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class NetlifyBuild(unittest.TestCase):
    """scripts/write-config.mjs: the build step that tells the published page where the API is."""

    def build(self, **env):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'config.js'
            done = subprocess.run(['node', str(Path(__file__).resolve().parent.parent / 'scripts' / 'write-config.mjs')], capture_output=True, text=True,
                                  env={**os.environ, 'CONFIG_OUT': str(out), 'CONTEXT': '', 'SCOUT_API_BASE': '', **env})
            return done.returncode, out.read_text(encoding='utf-8') if out.exists() else None

    def test_writes_the_api_address(self):
        code, text = self.build(SCOUT_API_BASE='https://scout-place-finder.onrender.com/')
        self.assertEqual(code, 0)
        self.assertIn('window.SCOUT_API_BASE = "https://scout-place-finder.onrender.com";', text)  # no trailing slash

    def test_a_preview_without_an_address_serves_itself(self):
        code, text = self.build(CONTEXT='deploy-preview')
        self.assertEqual((code, 'window.SCOUT_API_BASE = "";' in text), (0, True))

    def test_production_without_a_proper_address_fails_instead_of_publishing(self):
        for bad in ('', 'http://insecure.example', 'not a url', 'https://x.onrender.com/path'):
            code, text = self.build(CONTEXT='production', SCOUT_API_BASE=bad)
            self.assertEqual((code, text), (1, None), bad)


if __name__ == '__main__':
    unittest.main()
