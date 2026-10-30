"""The connect flow end to end, against fakes: AWS (one cluster, two SSM
parameters), fzf (scripted answers) and a mysql on PATH that accepts one
password."""

import json
import sys

import pytest

import rds_mysql

PARAMS = {'/a/good': 'pw-good', '/a/bad': 'pw-bad', '/a/down': 'pw-down'}

FAKE_MYSQL = """#!/bin/sh
case "$*" in *"SELECT 1"*)
  [ "$MYSQL_PWD" = pw-good ] && exit 0
  [ "$MYSQL_PWD" = pw-down ] && { echo "ERROR 2003 (HY000): Can't connect" >&2; exit 1; }
  echo "ERROR 1045 (28000): Access denied for user" >&2; exit 1;; esac
exit 0
"""


class NotFound(Exception):
    pass


class FakePaginator:
    def __init__(self, op):
        self.op = op

    def paginate(self, **_):
        if self.op == 'describe_db_clusters':
            return [{'DBClusters': [{'DBClusterIdentifier': 'prod-api',
                                     'Endpoint': 'rw.x', 'ReaderEndpoint': 'ro.x'}]}]
        return [{'Parameters': [{'Name': n} for n in PARAMS]}]


class FakeClient:
    class exceptions:
        ParameterNotFound = NotFound

    def get_paginator(self, op):
        return FakePaginator(op)

    def get_parameter(self, Name, WithDecryption):
        if Name not in PARAMS:
            raise NotFound(Name)
        return {'Parameter': {'Value': PARAMS[Name]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'mysql').write_text(FAKE_MYSQL)
    (bin_dir / 'mysql').chmod(0o755)
    monkeypatch.setenv('PATH', f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    monkeypatch.setattr(rds_mysql, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(rds_mysql, 'CONFIG_FILE', tmp_path / 'cfg' / 'config.json')
    monkeypatch.setattr(rds_mysql.boto3, 'client', lambda _: FakeClient())

    execs = []
    monkeypatch.setattr(rds_mysql.os, 'execvp',
                        lambda _, argv: execs.append((argv, rds_mysql.os.environ['MYSQL_PWD'])))

    class Env:
        exec_calls = execs

        def run(self, answers, *args, password=None):
            """Run with fzf answering from answers (the first item starting
            with each), and getpass returning password."""
            it = iter(answers)

            def fzf(items, print_query=False, query=None, **_):
                a = next(it)
                sel = next((x for x in items if x.startswith(a)), None)
                return (query or a, sel) if print_query else sel

            monkeypatch.setattr(rds_mysql, 'iterfzf', fzf)
            monkeypatch.setattr(rds_mysql.getpass, 'getpass', lambda _: password)
            monkeypatch.setattr(sys, 'argv', ['rds-mysql', '-u', 'app', *args])
            rds_mysql.main()

        def saved(self):
            try:
                return json.loads(rds_mysql.CONFIG_FILE.read_text())['default']['prod-api']
            except FileNotFoundError:
                return {}

        def save(self, path):
            rds_mysql.Config().set('prod-api', 'app', path)

    return Env()


def test_saved_only_once_a_login_works(env):
    env.run(['rw', 'ssm', '/a/bad', 'ssm', '/a/good'])
    assert env.saved() == {'app': '/a/good'}
    assert env.exec_calls[-1][1] == 'pw-good'


def test_saved_parameter_connects_straight_away(env):
    env.save('/a/good')
    env.run(['rw'])
    assert env.exec_calls[-1][1] == 'pw-good'


def test_saved_parameter_that_fails_is_forgotten(env):
    env.save('/a/bad')
    env.run(['rw', 'ssm', '/a/good'])
    assert env.saved() == {'app': '/a/good'}


def test_saved_parameter_that_is_gone_is_forgotten(env):
    env.save('/a/missing')
    env.run(['rw', 'ssm', '/a/good'])
    assert env.saved() == {'app': '/a/good'}


def test_network_error_stops_and_keeps_saved(env):
    env.save('/a/down')
    with pytest.raises(SystemExit):
        env.run(['rw'])
    assert env.saved() == {'app': '/a/down'}
    assert not env.exec_calls


def test_typed_password_is_never_saved(env):
    env.run(['rw', 'type'], password='pw-good')
    assert env.saved() == {}
    assert env.exec_calls[-1][1] == 'pw-good'


def test_forget_asks_again(env):
    env.save('/a/good')
    env.run(['rw', 'type'], '-f', password='pw-good')
    assert env.saved() == {}


def test_aws_setup_problems_say_what_to_fix(monkeypatch, capsys):
    from botocore.exceptions import NoRegionError
    monkeypatch.setenv('AWS_PROFILE', 'prod')

    def no_region():
        raise NoRegionError()
    monkeypatch.setattr(rds_mysql, 'main', no_region)
    with pytest.raises(SystemExit) as e:
        rds_mysql.run()
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "[profile prod]" in out and "~/.aws/config" in out
