"""Persistent account-wide throttling; never store attempted or configured passwords."""
import hmac
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def valid_password(value):
    return bool(re.fullmatch(r'[0-9]{4}', value)) or len(value) >= 16


class LoginGate:
    LIMIT = 5
    WINDOW = 15 * 60

    def __init__(self, url):
        self.url = url
        self.pg = url.startswith(('postgres://', 'postgresql://'))
        if self.pg:
            if not any(s in url for s in ('sslmode=require', 'sslmode=verify-full')):
                raise ValueError('TLS connection required')
        elif not url.startswith('sqlite:///'):
            raise ValueError('Unsupported login storage')

    @contextmanager
    def connection(self):
        if self.pg:
            import psycopg
            c = psycopg.connect(self.url, connect_timeout=10)
        else:
            path = self.url[len('sqlite:///'):]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(path, timeout=15)
            c.execute('BEGIN IMMEDIATE')
        try:
            if self.pg:
                c.execute('SELECT pg_advisory_xact_lock(716294019)')
            c.execute('CREATE TABLE IF NOT EXISTS settlement_login_gate '
                      '(id INTEGER PRIMARY KEY, failures INTEGER NOT NULL, started DOUBLE PRECISION NOT NULL, '
                      'blocked_until DOUBLE PRECISION NOT NULL)')
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def attempt(self, supplied, expected, now=None):
        if not valid_password(expected):
            raise ValueError('Invalid configured password')
        with self.connection() as c:
            # Read the clock after the write lock so concurrent submissions are ordered.
            now = time.time() if now is None else now
            row = c.execute('SELECT failures,started,blocked_until FROM settlement_login_gate WHERE id=1').fetchone()
            failures, started, blocked = row or (0, now, 0)
            if blocked > now:
                return False, int(blocked - now) + 1
            if blocked or now - started >= self.WINDOW:
                failures, started, blocked = 0, now, 0
            ok = hmac.compare_digest(supplied.encode(), expected.encode())
            if ok:
                failures, started, blocked = 0, now, 0
            else:
                failures += 1
                if failures >= self.LIMIT:
                    blocked = now + self.WINDOW
            marker = '%s' if self.pg else '?'
            c.execute('INSERT INTO settlement_login_gate(id,failures,started,blocked_until) '
                      f'VALUES (1,{marker},{marker},{marker}) ON CONFLICT(id) DO UPDATE SET '
                      'failures=excluded.failures,started=excluded.started,blocked_until=excluded.blocked_until',
                      (failures, started, blocked))
            return ok, int(blocked - now) if blocked else 0
