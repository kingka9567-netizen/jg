import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from settlement.auth import LoginGate, valid_password


class LoginTests(unittest.TestCase):
    def test_pin_and_existing_long_password_policy(self):
        self.assertTrue(valid_password('0042'))
        self.assertTrue(valid_password('a sufficiently long passphrase'))
        for value in ('', '123', '12345', 'abcd', '１２３４'):
            self.assertFalse(valid_password(value))

    def test_block_survives_new_instance_and_correct_pin_cannot_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            url='sqlite:///'+str(Path(tmp)/'auth.sqlite3')
            for i in range(5):
                ok, wait=LoginGate(url).attempt('bad', '0042', now=100+i)
                self.assertFalse(ok)
            self.assertEqual(wait,900)
            self.assertEqual(LoginGate(url).attempt('0042','0042',now=200),(False,805))
            self.assertEqual(LoginGate(url).attempt('0042','0042',now=1005),(True,0))
            with LoginGate(url).connection() as c:
                self.assertEqual(c.execute('SELECT failures FROM settlement_login_gate').fetchone()[0],0)
                self.assertNotIn('0042',repr(c.execute('SELECT * FROM settlement_login_gate').fetchall()))

    def test_attempt_window_expires_and_success_resets_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=LoginGate('sqlite:///'+str(Path(tmp)/'auth.sqlite3'))
            for i in range(4):gate.attempt('wrong','0042',now=100+i)
            self.assertEqual(gate.attempt('0042','0042',now=110),(True,0))
            for i in range(4):self.assertEqual(gate.attempt('wrong','0042',now=111+i),(False,0))
            self.assertEqual(gate.attempt('wrong','0042',now=1100),(False,0))

    def test_parallel_attempts_share_one_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            url='sqlite:///'+str(Path(tmp)/'auth.sqlite3')
            def fail(_):return LoginGate(url).attempt('wrong','0042',now=100)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results=list(pool.map(fail,range(12)))
            self.assertEqual(sum(wait==0 for ok,wait in results),4)
            with LoginGate(url).connection() as c:
                self.assertEqual(c.execute('SELECT failures FROM settlement_login_gate').fetchone()[0],5)

    def test_tls_required_and_invalid_config_fails_before_connection(self):
        with self.assertRaises(ValueError):LoginGate('postgresql://invalid/no-tls')
        with self.assertRaises(ValueError):LoginGate('sqlite:///never-created.sqlite3').attempt('x','bad')
