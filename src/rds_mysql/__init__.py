"""rds-mysql: pick an RDS cluster with fzf and open mysql on it.

Clusters come from the current AWS profile. For each user you connect
as, the password is either found in SSM Parameter Store (fuzzy search
over the parameter names) or typed in. A parameter you pick is
remembered for that profile, cluster and user in
~/.config/rds-mysql/config.json (its path only, never the password), so
next time it connects straight away; -f forgets it and asks again.
The user name can come from a parameter too (remembered the same way,
read again each time). Cluster and parameter names are cached per
profile for a week (-r refreshes); passwords never are. mysql checks the
server against Amazon's RDS CA certificates, fetched once.
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import NoCredentialsError, NoRegionError, ProfileNotFound
from iterfzf import iterfzf
from rich.console import Console
from rich.markup import escape

console = Console()

CACHE_DIR = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'rds-mysql'
CONFIG_FILE = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'rds-mysql' / 'config.json'
CACHE_TTL = 7 * 24 * 3600
# Amazon's CA for RDS: not in the system's trust store, and MariaDB's
# mysql client (11.4+) verifies the server's certificate by default.
RDS_CA_URL = 'https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem'


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


# The key a cluster's user-name parameter is saved under, beside its users.
USER_PARAM = '@user'


class Config:
    """Saved SSM paths: {profile: {cluster: {user: parameter path}}}, and
    under "@user" the parameter the user name came from."""

    def __init__(self):
        try:
            self.data = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            self.data = {}

    def users(self, cluster_id: str) -> dict[str, str]:
        saved = self.data.get(profile(), {}).get(cluster_id, {})
        return {u: p for u, p in saved.items() if u != USER_PARAM}

    def user_param(self, cluster_id: str) -> str | None:
        return self.data.get(profile(), {}).get(cluster_id, {}).get(USER_PARAM)

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

    def get_value(self, path: str) -> str:
        return self.client.get_parameter(Name=path, WithDecryption=True)['Parameter']['Value']

    def get_parameter(self, path: str) -> str:
        """The password in a parameter."""
        return password_of(self.get_value(path))

    def fetch_names(self) -> list[str]:
        paginator = self.client.get_paginator('describe_parameters')
        return sorted(p['Name'] for page in paginator.paginate() for p in page['Parameters'])

    def list_names(self) -> list[str]:
        # Names only (describe_parameters is slow); values are never cached
        return cached('parameters', self.fetch_names,
                      refresh=self.refresh, status="Fetching SSM parameter names...")


def field_of(value: str, *keys: str) -> str | None:
    """The first of keys in a parameter's value, when it's a JSON object
    ({"username": ..., "password": ...}); otherwise None."""
    if not value.lstrip().startswith('{'):
        return None
    try:
        v = json.loads(value)
    except ValueError:
        return None
    if isinstance(v, dict):
        for k in keys:
            if isinstance(v.get(k), str):
                return v[k]
    return None


def password_of(value: str) -> str:
    """The password in a parameter's value: its "password" in JSON, or the value."""
    return field_of(value, 'password') or value


def username_of(value: str) -> str:
    """The user name in a parameter's value: its "username" (or "user") in
    JSON, or the value."""
    return (field_of(value, 'username', 'user') or value).strip()


def rds_ca(refresh: bool = False) -> Path:
    """Amazon's RDS CA bundle, fetched once into the cache."""
    path = CACHE_DIR / 'rds-global-bundle.pem'
    if refresh or not path.exists():
        with console.status("Fetching the RDS CA certificates..."):
            with urllib.request.urlopen(RDS_CA_URL, timeout=15) as r:
                pem = r.read()
        if b'BEGIN CERTIFICATE' not in pem:
            raise OSError(f"{RDS_CA_URL} returned no certificates")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(pem)
        tmp.replace(path)
    return path


SSM_USER = 'ssm   search Parameter Store for the user name'


def select_user(ssm: 'SSMClient', config: Config, cluster_id: str,
                given: str | None) -> tuple[str | None, str | None, str | None]:
    """Who to connect as: given (-u), a saved user, one typed in, or one
    read from an SSM parameter. Returns (user, the parameter it came from,
    the password when that parameter holds one too)."""
    if given:
        return given.strip(), None, None
    users = sorted(config.users(cluster_id))
    # The saved user-name parameter, read again: the name may have changed.
    if path := config.user_param(cluster_id):
        try:
            with console.status(f"Fetching {path}..."):
                name = username_of(ssm.get_value(path))
            users = [name] + [u for u in users if u != name]
        except ssm.client.exceptions.ParameterNotFound:
            console.print(f"[yellow]Saved parameter {path} is gone.[/yellow]")
            config.set(cluster_id, USER_PARAM, None)
    # Typing a name that isn't listed connects as it
    query, selected = iterfzf(users + [SSM_USER], prompt='Select user (or type a new one)> ', print_query=True)
    if selected != SSM_USER:
        return (selected or query or '').strip() or None, None, None
    path = select_parameter(ssm, 'SSM parameter with the user name> ')
    if not path:
        return None, None, None
    with console.status(f"Fetching {path}..."):
        value = ssm.get_value(path)
    return username_of(value) or None, path, field_of(value, 'password')


def select_parameter(ssm: SSMClient, prompt: str) -> str | None:
    names = ssm.list_names()
    if not names:
        console.print("[red]No SSM parameters in this account and region.[/red]")
        return None
    return iterfzf(names, prompt=prompt)


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
    path = select_parameter(ssm, f'SSM parameter with {user}\'s password> ')
    if not path:
        return None, None
    with console.status(f"Fetching {path}..."):
        return ssm.get_parameter(path), path


def check_login(endpoint: str, username: str, password: str, ca: Path) -> str | None:
    """Log in once with SELECT 1. None when it works, else mysql's error;
    an access-denied error starts with ERROR 1045."""
    with console.status(f"Logging in to {endpoint} as {username}..."):
        r = subprocess.run(
            ['mysql', '--connect-timeout=5', f'--ssl-ca={ca}', '-h', endpoint, '-u', username,
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


def connect_mysql(endpoint: str, username: str, password: str, env: str, ca: Path, execute: str | None = None):
    console.print(f"[yellow]Connecting to MySQL on [bold]{endpoint}[/bold]...[/yellow]")

    print(get_env_color(env))

    os.environ['MYSQL_PWD'] = password
    argv = [
        'mysql',
        '--connect-timeout=5',
        f'--ssl-ca={ca}',
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
    ssm = SSMClient(refresh=args.refresh)
    user, user_path, user_password = select_user(ssm, config, cluster_id, args.user)
    if not user:
        sys.exit(1)

    try:
        ca = rds_ca(refresh=args.refresh)
    except OSError as e:
        console.print(f"[red]Can't get Amazon's RDS CA certificates ({escape(str(e))}); "
                      "mysql needs them to check the server.[/red]")
        sys.exit(1)
    if args.forget:
        config.set(cluster_id, user, None)

    password = None
    saved = path = config.users(cluster_id).get(user)
    # The user-name parameter holds the password too ({"username", "password"})
    if user_password is not None and not saved:
        password, path = user_password, user_path
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
        err = check_login(endpoint, user, password, ca)
        if err is None:
            break
        console.print(f"[red]{escape(err)}[/red]")
        if 'SSL' in err or 'TLS' in err or 'certificate' in err:
            console.print(f"[yellow]The server's certificate didn't check out against {ca}: "
                          "-r fetches Amazon's RDS CA certificates again.[/yellow]")
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
    if user_path and user_path != config.user_param(cluster_id):
        config.set(cluster_id, USER_PARAM, user_path)
        console.print(f"[dim]Saved {user_path} as the user name for {cluster_id}.[/dim]")

    connect_mysql(endpoint, user, password, env, ca, execute=args.execute)


def run():
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except (NoRegionError, ProfileNotFound, NoCredentialsError) as e:
        console.print(f"[red]{escape(aws_problem(e))}[/red]")
        sys.exit(1)


def aws_problem(e: Exception) -> str:
    """What's missing from the AWS setup, and where to put it."""
    p = profile()
    section = 'default' if p == 'default' else f'profile {p}'
    if isinstance(e, NoRegionError):
        return (f"No AWS region for profile {p}: add 'region = <region>' under [{section}] "
                "in ~/.aws/config, or export AWS_REGION.")
    if isinstance(e, ProfileNotFound):
        return f"No AWS profile {p} in ~/.aws/config or ~/.aws/credentials."
    return f"No AWS credentials for profile {p}: log in (aws sso login) or add its keys to ~/.aws/credentials."
