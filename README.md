# rds-mysql

Pick an RDS MySQL cluster with fzf and connect to it, the password taken
from SSM Parameter Store or typed in.

```sh
rds-mysql              # pick a cluster, writer or reader, user, password
rds-mysql prod api     # only clusters whose name has both words
rds-mysql -u ada       # connect as ada, no user picker
rds-mysql -e 'SHOW PROCESSLIST'   # run one statement instead of a shell
```

## Install

```sh
uv tool install git+https://github.com/eugenetaranov/rds-mysql
```

It needs the `mysql` client (MySQL's, or MariaDB's `mariadb-client`) and
AWS credentials and a region for the profile you're in (`AWS_PROFILE`).

`mysql` checks the cluster's TLS certificate against Amazon's RDS CA
certificates, fetched once from
`truststore.pki.rds.amazonaws.com` into `~/.cache/rds-mysql` (`-r`
fetches them again). MariaDB's client (11.4 and later) won't connect to
RDS without them.

## Who you connect as

The user picker lists the users you've connected as before; type a name
to use a new one, or pick **ssm** to search Parameter Store for a
parameter holding the user name. That parameter is remembered for the
cluster once a login works, and read again each time, so a renamed user
follows. A parameter whose value is JSON gives its `username`, and its
`password` too when it has one.

## How it finds the password

The first time you connect as a user, rds-mysql asks where the password
is:

- **SSM Parameter Store**: a fuzzy search over the account's parameter
  names (a JSON value gives its `password`). Once the password logs in, the
  parameter's path (never the password) is saved for that AWS profile,
  cluster and user in `~/.config/rds-mysql/config.json`, and the next
  time you connect straight away.
- **Typed in**: asked for and used once, never saved.

Each password is tried with `SELECT 1` before the shell opens. If it's
refused (access denied), rds-mysql asks again, and a saved parameter
whose password stopped working is forgotten. Any other error, such as a
timeout or no route to the cluster, stops it and leaves what's saved
alone. `-f` forgets the saved parameter for this cluster and user and asks
again.

Cluster and parameter names are cached per profile for a week in
`~/.cache/rds-mysql` (`-r` re-fetches them); passwords never are. The
password reaches `mysql` through `MYSQL_PWD`, not the command line.

The terminal turns red for a cluster whose name starts with a word
containing `prod`, blue for `stag`, green otherwise.

## Development

```sh
uv run pytest
```

## License

MIT
