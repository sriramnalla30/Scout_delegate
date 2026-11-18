"""Offline tests for accounts: signing up and in (password and Google), sessions, and above all that each account's
chats and conversations stay private to it.

    python -m unittest discover -s tests -v
"""
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import access, auth, config, server, store  # noqa: E402

PASSWORD = 'correct horse battery'
CHAT = {'title': 'gyms in Koramangala', 'updated': 5, 'log': [{'u': 'hi'}, {'ev': {'type': 'done'}}]}
THREAD = 'chat00000001'


class Accounts(unittest.TestCase):
    """The app as a person meets it: a fresh data folder, a real server app, no model and no network."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        access._hits.clear()
        self.patches = [mock.patch.object(config, 'DATA_DIR', Path(self.folder.name)), mock.patch.object(config, 'ALLOWED_ORIGINS', []),
                        mock.patch.object(config, 'CHAT_RATE_PER_HOUR', 0), mock.patch.object(config, 'GOOGLE_CLIENT_ID', ''),
                        mock.patch.object(config, 'SEED_USERS', ''), mock.patch.object(config, 'LEGACY_CHATS_OWNER', '')]
        for p in self.patches:
            p.start()
        self.client = TestClient(server.create_app())

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.folder.cleanup()

    def signup(self, email='alice@example.com', password=PASSWORD, name=None):
        return self.client.post('/api/auth/signup', json={'email': email, 'password': password, 'name': name})

    def login(self, email='alice@example.com', password=PASSWORD):
        return self.client.post('/api/auth/login', json={'email': email, 'password': password})

    def person(self, email):
        """Sign up and return the headers that act as this account."""
        return {'Authorization': 'Bearer ' + self.signup(email).json()['token']}

    # ---- sign up and in ----
    def test_signup_returns_a_session_and_the_profile(self):
        response = self.signup('Alice@Example.COM', name='  Alice   Smith ')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body['user']['email'], body['user']['name'], body['user']['methods']), ('alice@example.com', 'Alice Smith', ['password']))
        self.assertNotIn('password_hash', body['user'])
        self.assertNotIn('id', body['user'])
        me = self.client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + body['token']})
        self.assertEqual(me.json()['user']['email'], 'alice@example.com')

    def test_the_name_defaults_to_the_start_of_the_email(self):
        self.assertEqual(self.signup('rani.k@example.com').json()['user']['name'], 'rani.k')

    def test_signup_rejects_bad_emails_and_short_passwords(self):
        for email in ('', 'nope', 'a@b', 'two words@x.com', None):
            self.assertEqual(self.signup(email).status_code, 400, email)
        self.assertEqual(self.signup('ok@example.com', password='short').status_code, 400)
        self.assertEqual(self.signup('ok@example.com', password='x' * 200).status_code, 400)
        self.assertEqual(self.client.post('/api/auth/signup', content='not json').status_code, 400)
        self.assertEqual(self.client.post('/api/auth/signup', json=[1, 2]).status_code, 400)

    def test_the_email_forms_from_the_task_are_accepted(self):
        self.assertEqual(self.signup('test@rate.gmail.com', password='test@rate123').status_code, 200)
        self.assertEqual(self.signup('thread123@threadmail.com', password='thread123').status_code, 200)

    def test_an_email_can_have_one_account(self):
        self.assertEqual(self.signup().status_code, 200)
        again = self.signup('ALICE@example.com', password='another password')
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.login(password='another password').status_code, 401)  # the second signup changed nothing

    def test_login_with_the_right_password_only(self):
        self.signup()
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.login('ALICE@EXAMPLE.COM').status_code, 200)  # the case of the email does not matter
        wrong, unknown = self.login(password='wrong password'), self.login('nobody@example.com')
        self.assertEqual((wrong.status_code, unknown.status_code), (401, 401))
        self.assertEqual(wrong.json(), unknown.json())  # a stranger cannot tell which emails have accounts
        self.assertEqual(self.login(password='').status_code, 401)

    def test_passwords_are_stored_as_salted_hashes(self):
        self.signup()
        stored = auth.find_by_email('alice@example.com')['password_hash']
        self.assertTrue(stored.startswith('scrypt$'))
        self.assertNotIn(PASSWORD, stored)
        self.signup('bob@example.com')
        self.assertNotEqual(stored, auth.find_by_email('bob@example.com')['password_hash'])  # same password, different salt
        self.assertTrue(auth.check_password(PASSWORD, stored))
        self.assertFalse(auth.check_password('x', stored))
        self.assertFalse(auth.check_password(PASSWORD, 'garbage'))

    def test_repeated_wrong_passwords_lock_that_email_for_a_while(self):
        self.signup()
        codes = [self.login(password=f'wrong password {i}').status_code for i in range(9)]
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(codes[8], 429)
        self.assertEqual(self.login().status_code, 429)  # even the right password waits
        self.assertEqual(self.login('someone@else.com').status_code, 401)  # other emails are not affected

    def test_a_good_sign_in_clears_the_wrong_tries(self):
        self.signup()
        for i in range(5):
            self.login(password=f'wrong password {i}')
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual([self.login(password=f'wrong again {i}').status_code for i in range(8)], [401] * 8)

    def test_new_accounts_per_address_are_limited(self):
        codes = [self.signup(f'user{i}@example.com').status_code for i in range(access.SIGNUP_CAP + 1)]
        self.assertEqual(codes, [200] * access.SIGNUP_CAP + [429])

    # ---- sessions ----
    def test_signing_in_twice_gives_two_sessions_and_logout_ends_only_one(self):
        self.signup()
        first = {'Authorization': 'Bearer ' + self.login().json()['token']}
        second = {'Authorization': 'Bearer ' + self.login().json()['token']}
        self.assertEqual(self.client.post('/api/auth/logout', headers=first).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me', headers=first).status_code, 401)
        self.assertEqual(self.client.get('/api/auth/me', headers=second).status_code, 200)

    def test_a_session_expires(self):
        token = self.signup().json()['token']
        user = auth.user_for_token(token)
        self.assertEqual(user['email'], 'alice@example.com')
        with mock.patch.object(time, 'time', return_value=time.time() + (config.SESSION_DAYS + 1) * 86400):
            self.assertIsNone(auth.user_for_token(token))

    def test_the_database_holds_a_hash_of_the_token_not_the_token(self):
        token = self.signup().json()['token']
        with closing(auth._db()) as db:
            hashes = [row[0] for row in db.execute('SELECT token_hash FROM sessions')]
        self.assertEqual(len(hashes), 1)
        self.assertNotEqual(hashes[0], token)

    def test_bad_tokens_get_no_access(self):
        for header in ({}, {'Authorization': 'Bearer'}, {'Authorization': 'Bearer nonsense'}, {'Authorization': 'Basic abc'}, {'Authorization': 'Bearer ' + 'x' * 500}):
            self.assertEqual(self.client.get('/api/chats', headers=header).status_code, 401, header)

    # ---- everything private needs an account ----
    def test_the_api_needs_an_account_but_the_page_and_health_do_not(self):
        for method, path in (('get', '/api/chats'), ('get', f'/api/chats/{THREAD}'), ('put', f'/api/chats/{THREAD}'), ('delete', f'/api/chats/{THREAD}'),
                             ('get', f'/api/thread/{THREAD}'), ('post', '/api/chat'), ('get', '/api/searches/s0123456789ab'), ('get', '/api/auth/me')):
            self.assertEqual(getattr(self.client, method)(path).status_code, 401, path)
        for path in ('/', '/app.js', '/auth.js', '/auth.css', '/style.css', '/config.js', '/api/health', '/api/auth/config'):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_signing_out_in_one_browser_cannot_be_replayed(self):
        headers = self.person('alice@example.com')
        self.client.post('/api/auth/logout', headers=headers)
        self.assertEqual(self.client.put(f'/api/chats/{THREAD}', json=CHAT, headers=headers).status_code, 401)

    # ---- privacy: the point of accounts ----
    def test_each_account_sees_only_its_own_history(self):
        alice, bob = self.person('alice@example.com'), self.person('bob@example.com')
        self.assertEqual(self.client.put(f'/api/chats/{THREAD}', json=CHAT, headers=alice).status_code, 200)
        self.assertEqual([c['id'] for c in self.client.get('/api/chats', headers=alice).json()], [THREAD])
        self.assertEqual(self.client.get('/api/chats', headers=bob).json(), [])
        self.assertEqual(self.client.get(f'/api/chats/{THREAD}', headers=bob).status_code, 404)
        self.assertEqual(self.client.get(f'/api/chats/{THREAD}', headers=alice).json()['log'], CHAT['log'])

    def test_a_new_account_starts_with_an_empty_history(self):
        alice = self.person('alice@example.com')
        self.client.put(f'/api/chats/{THREAD}', json=CHAT, headers=alice)
        self.assertEqual(self.client.get('/api/chats', headers=self.person('sister@example.com')).json(), [])

    def test_history_survives_signing_out_and_in_again(self):
        alice = self.person('alice@example.com')
        self.client.put(f'/api/chats/{THREAD}', json=CHAT, headers=alice)
        self.client.post('/api/auth/logout', headers=alice)
        again = {'Authorization': 'Bearer ' + self.login().json()['token']}
        self.assertEqual([c['title'] for c in self.client.get('/api/chats', headers=again).json()], ['gyms in Koramangala'])

    def test_another_account_cannot_overwrite_or_delete_a_chat(self):
        alice, bob = self.person('alice@example.com'), self.person('bob@example.com')
        self.client.put(f'/api/chats/{THREAD}', json=CHAT, headers=alice)
        evil = {**CHAT, 'title': 'hijacked', 'log': []}
        self.assertEqual(self.client.put(f'/api/chats/{THREAD}', json=evil, headers=bob).status_code, 404)
        with mock.patch.object(server, 'forget_thread') as forget:
            self.assertEqual(self.client.delete(f'/api/chats/{THREAD}', headers=bob).status_code, 200)
            forget.assert_not_called()  # Alice's agent memory is untouched
        got = self.client.get(f'/api/chats/{THREAD}', headers=alice).json()
        self.assertEqual((got['title'], got['log']), ('gyms in Koramangala', CHAT['log']))
        self.assertEqual(self.client.get('/api/chats', headers=bob).json(), [])

    def test_the_owner_can_delete_a_chat_and_the_agent_forgets_it(self):
        alice = self.person('alice@example.com')
        self.client.put(f'/api/chats/{THREAD}', json=CHAT, headers=alice)
        with mock.patch.object(server, 'forget_thread') as forget:
            self.assertEqual(self.client.delete(f'/api/chats/{THREAD}', headers=alice).status_code, 200)
            forget.assert_called_once_with(THREAD)
        self.assertEqual(self.client.get('/api/chats', headers=alice).json(), [])
        self.assertFalse(auth.owns_thread(THREAD, auth.find_by_email('alice@example.com')['id']))

    def test_another_account_cannot_talk_into_or_peek_at_a_conversation(self):
        alice, bob = self.person('alice@example.com'), self.person('bob@example.com')
        seen = []

        def fake(thread, text):
            seen.append((thread, text))
            yield {'type': 'done', 'waiting': False}
        with mock.patch.object(server, 'stream_events', fake), mock.patch.object(server, 'thread_state', lambda t: {'exists': True, 'waiting': True}):
            self.assertEqual(self.client.post('/api/chat', json={'thread_id': THREAD, 'message': 'hi'}, headers=alice).status_code, 200)
            self.assertEqual(self.client.post('/api/chat', json={'thread_id': THREAD, 'message': 'let me in'}, headers=bob).status_code, 403)
            self.assertEqual(seen, [(THREAD, 'hi')])  # Bob's message never reached the agent
            self.assertEqual(self.client.get(f'/api/thread/{THREAD}', headers=alice).json(), {'exists': True, 'waiting': True})
            self.assertEqual(self.client.get(f'/api/thread/{THREAD}', headers=bob).json(), {'exists': False, 'waiting': False})

    def test_chats_are_filed_under_the_account_not_the_email_or_a_shared_folder(self):
        self.client.put(f'/api/chats/{THREAD}', json=CHAT, headers=self.person('alice@example.com'))
        files = list((Path(self.folder.name) / 'chats').rglob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent.name, auth.find_by_email('alice@example.com')['id'])
        self.assertEqual(list((Path(self.folder.name) / 'chats').glob('*.json')), [])  # nothing in the shared top folder

    # ---- Google ----
    def google(self, **info):
        profile = {'sub': 'g-123', 'email': 'gina@example.com', 'email_verified': True, 'name': 'Gina G', **info}
        return mock.patch.object(auth, 'verify_google', return_value=profile)

    def test_google_is_not_available_until_a_client_id_is_set(self):
        self.assertIsNone(self.client.get('/api/auth/config').json()['google_client_id'])
        response = self.client.post('/api/auth/google', json={'credential': 'token'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('not set up', response.json()['error'])
        with mock.patch.object(config, 'GOOGLE_CLIENT_ID', 'abc.apps.googleusercontent.com'):
            self.assertEqual(self.client.get('/api/auth/config').json()['google_client_id'], 'abc.apps.googleusercontent.com')

    def test_google_creates_an_account_then_signs_the_same_person_in_again(self):
        with self.google():
            first = self.client.post('/api/auth/google', json={'credential': 'token'})
            second = self.client.post('/api/auth/google', json={'credential': 'token'})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        user = first.json()['user']
        self.assertEqual((user['email'], user['name'], user['methods']), ('gina@example.com', 'Gina G', ['google']))
        headers = {'Authorization': 'Bearer ' + first.json()['token']}
        self.client.put(f'/api/chats/{THREAD}', json=CHAT, headers=headers)
        again = {'Authorization': 'Bearer ' + second.json()['token']}
        self.assertEqual(len(self.client.get('/api/chats', headers=again).json()), 1)  # one account, one history

    def test_a_google_only_account_cannot_sign_in_with_a_password(self):
        with self.google():
            self.client.post('/api/auth/google', json={'credential': 'token'})
        self.assertEqual(self.login('gina@example.com', 'anything at all').status_code, 401)
        self.assertEqual(self.signup('gina@example.com').status_code, 409)  # nobody can attach a password to it either

    def test_google_with_an_unverified_email_is_refused(self):
        with self.google(email_verified=False):
            self.assertEqual(self.client.post('/api/auth/google', json={'credential': 'token'}).status_code, 401)
        self.assertIsNone(auth.find_by_email('gina@example.com'))

    def test_a_bad_google_token_is_refused(self):
        with mock.patch.object(config, 'GOOGLE_CLIENT_ID', 'abc.apps.googleusercontent.com'), \
                mock.patch('google.oauth2.id_token.verify_oauth2_token', side_effect=ValueError('bad token')):
            response = self.client.post('/api/auth/google', json={'credential': 'forged'})
        self.assertEqual(response.status_code, 401)

    def test_google_joins_an_existing_password_account_and_drops_the_unproven_password(self):
        self.signup('gina@example.com', password='stranger knows this')  # could have been made by someone who does not own the address
        before = auth.find_by_email('gina@example.com')['id']
        with self.google():
            response = self.client.post('/api/auth/google', json={'credential': 'token'})
        self.assertEqual(response.status_code, 200)
        user = auth.find_by_email('gina@example.com')
        self.assertEqual((user['id'], user['google_sub'], user['email_verified']), (before, 'g-123', 1))  # same account, same history
        self.assertIsNone(user['password_hash'])
        self.assertEqual(self.login('gina@example.com', 'stranger knows this').status_code, 401)

    def test_google_keeps_the_password_of_a_verified_account(self):
        with mock.patch.object(config, 'SEED_USERS', 'gina@example.com:seeded password'):
            auth.seed_users()
        with self.google():
            self.client.post('/api/auth/google', json={'credential': 'token'})
        self.assertEqual(self.login('gina@example.com', 'seeded password').status_code, 200)

    def test_an_email_linked_to_another_google_person_is_refused(self):
        with self.google():
            self.client.post('/api/auth/google', json={'credential': 'token'})
        with self.google(sub='g-999'):
            self.assertEqual(self.client.post('/api/auth/google', json={'credential': 'token'}).status_code, 409)

    # ---- accounts for a laptop and the history from before accounts ----
    def test_seeded_accounts_can_sign_in_and_follow_the_setting(self):
        with mock.patch.object(config, 'SEED_USERS', 'test@rate.gmail.com:test@rate123; thread123@threadmail.com:thread123;bad;nobody:x'):
            auth.startup()
        self.assertEqual(self.login('test@rate.gmail.com', 'test@rate123').status_code, 200)
        self.assertEqual(self.login('thread123@threadmail.com', 'thread123').status_code, 200)
        self.assertEqual(self.login('test@rate.gmail.com', 'wrong').status_code, 401)
        with mock.patch.object(config, 'SEED_USERS', 'test@rate.gmail.com:a-new-password'):
            auth.startup()  # the setting is the truth: the password follows it, the account (and its history) stays
        self.assertEqual(self.login('test@rate.gmail.com', 'a-new-password').status_code, 200)
        self.assertEqual(self.login('test@rate.gmail.com', 'test@rate123').status_code, 401)

    def test_nothing_is_seeded_without_the_setting(self):
        auth.startup()
        with closing(auth._db()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)

    def legacy_chats(self, *ids):
        folder = Path(self.folder.name) / 'chats'
        folder.mkdir(parents=True, exist_ok=True)
        for chat_id in ids:
            (folder / f'{chat_id}.json').write_text('{"title": "old %s", "updated": 3, "log": []}' % chat_id, encoding='utf-8')
        return folder

    def test_old_history_becomes_the_named_accounts_and_the_old_files_stay(self):
        folder = self.legacy_chats('oldchat00000001', 'oldchat00000002')
        with mock.patch.object(config, 'SEED_USERS', 'thread123@threadmail.com:thread123;test@rate.gmail.com:test@rate123'), \
                mock.patch.object(config, 'LEGACY_CHATS_OWNER', 'thread123@threadmail.com'):
            auth.startup()
            auth.startup()  # a second start copies nothing again
        mine = {'Authorization': 'Bearer ' + self.login('thread123@threadmail.com', 'thread123').json()['token']}
        other = {'Authorization': 'Bearer ' + self.login('test@rate.gmail.com', 'test@rate123').json()['token']}
        self.assertEqual(sorted(c['id'] for c in self.client.get('/api/chats', headers=mine).json()), ['oldchat00000001', 'oldchat00000002'])
        self.assertEqual(self.client.get('/api/chats', headers=other).json(), [])  # private from the other account
        self.assertEqual(self.client.get('/api/chats/oldchat00000001', headers=other).status_code, 404)
        self.assertEqual(sorted(p.name for p in folder.glob('*.json')), ['oldchat00000001.json', 'oldchat00000002.json'])  # nothing cleared
        # the agent's conversations came along: the account can continue them, the other cannot
        with mock.patch.object(server, 'stream_events', lambda t, m: iter([{'type': 'done', 'waiting': False}])):
            self.assertEqual(self.client.post('/api/chat', json={'thread_id': 'oldchat00000001', 'message': 'more'}, headers=mine).status_code, 200)
            self.assertEqual(self.client.post('/api/chat', json={'thread_id': 'oldchat00000001', 'message': 'more'}, headers=other).status_code, 403)

    def test_a_chat_deleted_after_the_import_does_not_come_back(self):
        self.legacy_chats('oldchat00000001')
        with mock.patch.object(config, 'SEED_USERS', 'thread123@threadmail.com:thread123'), mock.patch.object(config, 'LEGACY_CHATS_OWNER', 'thread123@threadmail.com'):
            auth.startup()
            mine = {'Authorization': 'Bearer ' + self.login('thread123@threadmail.com', 'thread123').json()['token']}
            with mock.patch.object(server, 'forget_thread'):
                self.client.delete('/api/chats/oldchat00000001', headers=mine)
            auth.startup()
        self.assertEqual(self.client.get('/api/chats', headers=mine).json(), [])

    def test_old_history_without_a_named_owner_is_left_alone(self):
        self.legacy_chats('oldchat00000001')
        with mock.patch.object(config, 'LEGACY_CHATS_OWNER', 'nobody@example.com'):
            auth.startup()  # no such account: nothing is copied, nothing breaks
        self.assertEqual(list((Path(self.folder.name) / 'chats').glob('*/')), [])
        self.assertEqual(self.client.get('/api/chats', headers=self.person('alice@example.com')).json(), [])


class StoreMoves(unittest.TestCase):
    def test_import_copies_each_file_once_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(config, 'DATA_DIR', Path(folder)):
            store.save_chat('keepme0000000001', 'new', 9, [], None)
            self.assertEqual(store.import_legacy('owner1'), ['keepme0000000001'])
            self.assertEqual(store.import_legacy('owner1'), [])  # the marker: once only
            self.assertEqual(store.chat_ids('owner1'), ['keepme0000000001'])
            self.assertTrue((Path(folder) / 'chats' / 'keepme0000000001.json').is_file())


class GoogleButtonTellsTheTruth(unittest.TestCase):
    """A phone opened the page while the hosted API was still waking, tapped Google, and was told 'not set up on this server' although it
    is. The page code must only say that once the server has answered, ask again when the answer is late, and retry a script that failed."""

    def setUp(self):
        self.code = (Path(server.STATIC) / 'auth.js').read_text(encoding='utf-8')

    def test_not_set_up_is_said_only_after_the_settings_arrived(self):
        say = self.code.index("note('Google sign-in is not set up")
        self.assertIn('if (!configLoaded)', self.code[:say])
        self.assertIn('configLoaded = true', self.code)

    def test_late_settings_are_asked_for_again_and_never_cached(self):
        self.assertIn("cache: 'no-store'", self.code)
        self.assertRegex(self.code, r'tries < \d+\) setTimeout\(\(\) => loadConfig\(tries \+ 1\)')

    def test_a_google_script_that_failed_can_be_loaded_again(self):
        self.assertRegex(self.code, r'tag\.onerror = \(\) => \{ tag\.remove\(\)')


if __name__ == '__main__':
    unittest.main()
