"""rds-mysql: pick an RDS cluster with fzf and open mysql on it.

Clusters come from the current AWS profile. For each user you connect
as, the password is either found in SSM Parameter Store (fuzzy search
over the parameter names) or typed in. A parameter you pick is
remembered for that profile, cluster and user in
~/.config/rds-mysql/config.json (its path only, never the password), so
next time it connects straight away; -f forgets it and asks again.
Cluster and parameter names are cached per profile for a week (-r
refreshes); passwords never are.
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from iterfzf import iterfzf
from rich.console import Console

console = Console()

CACHE_DIR = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'rds-mysql'
CONFIG_FILE = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'rds-mysql' / 'config.json'
CACHE_TTL = 7 * 24 * 3600


def profile() -> str:
    return os.environ.get('AWS_PROFILE', 'default')


def cached(name: str, fetch, refresh: bool = False, status: str = "Fetching..."):
    """Return JSON-serializable fetch() result, cached per AWS profile for CACHE_TTL seconds."""
    cache_file = CACHE_DIR / f'{name}-{profile()}.json'

    if not refresh:
        try:
            if time.time() - cache_file.stat().st_mtime < CACHE_TTL:
                return json.loads(cache_file.read_text())
        except (OSError, ValueError):
            pass

    with console.status(status):
        data = fetch()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data))
    return data


class Config:
    """Saved SSM paths: {profile: {cluster: {user: parameter path}}}."""

    def __init__(self):
        try:
            self.data = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            self.data = {}

    def users(self, cluster_id: str) -> dict[str, str]:
        return self.data.get(profile(), {}).get(cluster_id, {})

    def set(self, cluster_id: str, user: str, path: str | None):
        users = self.data.setdefault(profile(), {}).setdefault(cluster_id, {})
        if path is None:
            users.pop(user, None)
        else:
            users[user] = path
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, indent=2) + '\n')
        tmp.chmod(0o600)
        tmp.replace(CONFIG_FILE)


class RDSClient:
    def __init__(self, refresh: bool = False):
        self.refresh = refresh

    def fetch_clusters(self) -> list[dict]:
        # Keep only what we use: raw cluster dicts hold datetimes and aren't JSON-serializable
        paginator = boto3.client('rds').get_paginator('describe_db_clusters')
        return [
            {
                'DBClusterIdentifier': c['DBClusterIdentifier'],
                'Endpoint': c['Endpoint'],
                'ReaderEndpoint': c.get('ReaderEndpoint', c['Endpoint']),
            }
            for page in paginator.paginate()
            for c in page['DBClusters']
        ]

    def list_clusters(self) -> list[dict]:
        return cached('clusters', self.fetch_clusters,
                      refresh=self.refresh, status="Fetching RDS clusters...")

    def find_clusters(self, search_terms: list[str]) -> list[dict]:
        clusters = self.list_clusters()
        return [c for c in clusters
                if all(term.lower() in c['DBClusterIdentifier'].lower()
                       for term in search_terms)]


class SSMClient:
    def __init__(self, refresh: bool = False):
        self.client = boto3.client('ssm')
        self.refresh = refresh

    def get_parameter(self, path: str) -> str:
        resp = self.client.get_parameter(Name=path, WithDecryption=True)
        return resp['Parameter']['Value']

    def fetch_names(self) -> list[str]:
        paginator = self.client.get_paginator('describe_parameters')
        return sorted(p['Name'] for page in paginator.paginate() for p in page['Parameters'])

    def list_names(self) -> list[str]:
        # Names only (describe_parameters is slow); values are never cached
        return cached('parameters', self.fetch_names,
                      refresh=self.refresh, status="Fetching SSM parameter names...")


def select_user(saved: dict[str, str], given: str | None) -> str | None:
    if given:
        return given.strip()
    if not saved:
        try:
            return input('DB user: ').strip() or None
        except EOFError:
            return None
    # Saved users first; typing a name that isn't listed connects as it
    query, selected = iterfzf(sorted(saved), prompt='Select user (or type a new one)> ', print_query=True)
    return (selected or query or '').strip() or None


def select_parameter(ssm: SSMClient, cluster_id: str, user: str) -> str | None:
    names = ssm.list_names()
    if not names:
        console.print("[red]No SSM parameters in this account and region.[/red]")
        return None
    return iterfzf(names, prompt=f'SSM parameter with {user}\'s password> ', query=user)


def ask_password(ssm: SSMClient, cluster_id: str, user: str) -> tuple[str | None, str | None]:
    """Ask where the password comes from: an SSM parameter or typed in.
    Returns (password, parameter path or None when typed)."""
    choice = iterfzf(['ssm   search Parameter Store (remembered once it works)',
                      'type  enter the password'],
                     prompt=f'Password for {user}> ')
    if choice is None:
        return None, None
    if choice.startswith('type'):
        try:
            return getpass.getpass(f'Password for {user}: '), None
        except EOFError:
            return None, None
    path = select_parameter(ssm, cluster_id, user)
    if not path:
        return None, None
    with console.status(f"Fetching {path}..."):
        return ssm.get_parameter(path), path


def check_login(endpoint: str, username: str, password: str) -> str | None:
    """Log in once with SELECT 1. None when it works, else mysql's error;
    an access-denied error starts with ERROR 1045."""
    with console.status(f"Logging in to {endpoint} as {username}..."):
        r = subprocess.run(
            ['mysql', '--connect-timeout=5', '-h', endpoint, '-u', username,
             '--batch', '--skip-column-names', '-e', 'SELECT 1'],
            env={**os.environ, 'MYSQL_PWD': password},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
    return None if r.returncode == 0 else (r.stderr.strip() or f'mysql exited {r.returncode}')


def select_endpoint(cluster: dict) -> str | None:
    rw = cluster['Endpoint']
    ro = cluster.get('ReaderEndpoint', rw)

    options = [f"rw  {rw}", f"ro  {ro}"]
    selected = iterfzf(options, prompt='Select endpoint> ', query='rw')

    if selected is None:
        return None

    return rw if selected.startswith('rw') else ro


def select_cluster(clusters: list[dict]) -> dict | None:
    if not clusters:
        console.print("[red]No matching clusters found.[/red]")
        return None

    if len(clusters) == 1:
        return clusters[0]

    cluster_map = {c['DBClusterIdentifier']: c for c in clusters}
    selected = iterfzf(cluster_map.keys(), prompt='Select cluster> ')

    if selected is None:
        return None

    return cluster_map[selected]


def get_env_color(env: str) -> str:
    if 'prod' in env.lower():
        return '\033[0;31m'  # Red
    elif 'stag' in env.lower():
        return '\033[1;34m'  # Blue
    else:
        return '\033[0;32m'  # Green


def connect_mysql(endpoint: str, username: str, password: str, env: str, execute: str | None = None):
    console.print(f"[yellow]Connecting to MySQL on [bold]{endpoint}[/bold]...[/yellow]")

    print(get_env_color(env))

    os.environ['MYSQL_PWD'] = password
    argv = [
        'mysql',
        '--connect-timeout=2',
        '--auto-vertical-output',
        '--no-auto-rehash',  # -A: skip table/column completion load for a fast connect
        '-h', endpoint,
        '-u', username,
    ]
    if execute:
        argv += ['-e', execute]

    os.execvp('mysql', argv)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select an RDS MySQL cluster via fzf and connect to it.")
    parser.add_argument('search_terms', nargs='*', help="Terms to filter cluster identifiers (e.g. prod api)")
    parser.add_argument('-e', '--execute', metavar='SQL', help="Run this statement non-interactively (mysql -e) instead of opening a shell")
    parser.add_argument('-u', '--user', help="DB user to connect as, skipping the picker")
    parser.add_argument('-f', '--forget', action='store_true',
                        help="Forget the saved SSM parameter for this cluster and user, and ask again")
    parser.add_argument('-r', '--refresh', action='store_true',
                        help=f"Ignore cached cluster and parameter names ({CACHE_TTL // 86400}d TTL) and re-fetch them")
    return parser.parse_args()


def main():
    args = parse_args()

    rds = RDSClient(refresh=args.refresh)
    if args.search_terms:
        matches = rds.find_clusters(args.search_terms)
    else:
        matches = rds.list_clusters()

    cluster = select_cluster(matches)
    if not cluster:
        sys.exit(1)

    cluster_id = cluster['DBClusterIdentifier']
    env = cluster_id.split('-')[0]

    endpoint = select_endpoint(cluster)
    if not endpoint:
        sys.exit(1)

    config = Config()
    user = select_user(config.users(cluster_id), args.user)
    if not user:
        sys.exit(1)

    ssm = SSMClient(refresh=args.refresh)
    if args.forget:
        config.set(cluster_id, user, None)

    password = None
    saved = path = config.users(cluster_id).get(user)
    if path:
        try:
            with console.status(f"Fetching {path}..."):
                password = ssm.get_parameter(path)
        except ssm.client.exceptions.ParameterNotFound:
            console.print(f"[yellow]Saved parameter {path} is gone; pick another.[/yellow]")
            config.set(cluster_id, user, None)
            saved = path = None

    # Until a login works: a wrong password (access denied) asks again;
    # any other error (network, timeout) stops, keeping what's saved.
    while True:
        if password is None:
            password, path = ask_password(ssm, cluster_id, user)
            if password is None:
                sys.exit(1)
        err = check_login(endpoint, user, password)
        if err is None:
            break
        console.print(f"[red]{err}[/red]")
        if not err.startswith('ERROR 1045'):
            sys.exit(1)
        if saved and path == saved:
            console.print(f"[yellow]Forgetting {path}: its password doesn't work.[/yellow]")
            config.set(cluster_id, user, None)
            saved = None
        password = path = None

    # Remembered only now that it has logged in
    if path and path != saved:
        config.set(cluster_id, user, path)
        console.print(f"[dim]Saved {path} for {user} on {cluster_id} ({CONFIG_FILE}).[/dim]")

    connect_mysql(endpoint, user, password, env, execute=args.execute)


def run():
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
