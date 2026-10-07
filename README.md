# saneinteg

Integration checks and local development tools for sanecmp. The repository pins
a tested combination of three independently maintained components:

- **sanea** — the parent-facing Django web application;
- **sanex** — the system service installed on a monitored computer;
- **sanelib** — the shared network protocol.

Their Git submodules live in `components/`. The root uv project is only a test
environment: saneinteg does not build or publish a Python package.

The distribution names are `sanecmp-sanea`, `sanecmp-sanex` and
`sanecmp-sanelib`. Python imports, application commands and repository names keep
their short names. In particular, do not install the unrelated PyPI package
named `sanex`.

## Checkout and component revisions

Install Git, [uv](https://docs.astral.sh/uv/), Python 3.12 or newer, and
[makeapp](https://pypi.org/project/makeapp/) (`makeapp>=2.3,<3`) for application
development.

```bash
git clone https://github.com/sanecmp/saneinteg.git
cd saneinteg
./repoutil.sh init
```

`init` obtains the exact revisions recorded in the repository, not the latest
component versions. Inspect branches, revisions and local changes with:

```bash
./repoutil.sh status
```

To explicitly select a new revision in one component:

```bash
./repoutil.sh update sanex origin/main
```

The revision may be a commit, tag or remote branch. The script fetches origin,
then checks out that revision without force. It refuses to switch a component
with tracked or untracked changes, or away from a commit not known on an origin
remote branch. It never stages, commits or pushes changes.

Initializing or updating a submodule can leave it on a detached HEAD. Create or
switch to a component branch before editing its code. Publish component commits
first, then review, test and stage their pointers in saneinteg. A component
publication does not automatically update the recorded pointers.

## Local development

Start the complete isolated local environment with:

```bash
uv run --python 3.12 run.py develop
```

The runner installs each application CLI through `ma up --tool` from its component
directory, starts sanea, registers a development sanex client, and runs sanex in
the foreground. Open <http://127.0.0.1:18000/> and sign in with `demo` / `demo`.
Press `Ctrl+C` to stop both applications. No sudo or system service is needed.

On its first run, `run.py` creates a local `.env` from `.env.example`. It reads
that file through envbox; inherited environment variables take precedence. Edit
`.env` to change the HTTP/HTTPS ports or retain state directories. The local file
is not committed. Each command sets its own `PYTHON_ENV` and isolated state paths;
the wheel check also selects temporary listener ports.

The runner uses uv's inline script metadata and only depends on envbox. It does
not install application code into its own environment. `./run.py` is an executable
shortcut for the same uv invocation and also works from another directory.

Temporary state is removed after exit. To retain runs for inspection:

```bash
SANECMP_DEVELOPMENT_STATE_DIR=/tmp/sanecmp-runs uv run --python 3.12 run.py develop
```

## Integration checks

Run the dedicated integration suite from the repository root:

```bash
uv run --python 3.12 run.py tests -q
```

Additional arguments are passed to pytest, including `-k` for individual
scenarios. To retain certificates, databases and sanex state, supply a parent
directory; each invocation creates a separate run inside it:

```bash
SANECMP_INTEGRATION_STATE_DIR=/tmp/sanecmp-runs uv run --python 3.12 run.py tests -q
```

The suite checks real UDP discovery, HTTPS registration, Cheroot/Django mTLS,
configuration delivery, event and technical-log upload, concurrent clients with
SQLite, multiple monitored accounts, restart recovery, command cancellation and
updates from a temporary HTTPS Simple Index. It also checks process grouping with
synthetic procfs/AT-SPI data and isolated Unix channels for the indicator and
window agent. Repository-tool tests use temporary local Git repositories only.

Build wheels and source distributions for all three components, install the
wheels into a clean temporary Python 3.12 environment, and check application
commands and sanea listeners outside the source checkout with:

```bash
uv run --python 3.12 run.py wheel
```

These checks run without root and do not change `/opt`, `/etc`, `/var/log`, systemd,
D-Bus, GNOME or user sessions. They do not perform privileged installation, real
EUID/prctl operations, or close actual windows. For isolated verification of
`ma up --tool`, set `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` to temporary directories and
put the latter on `PATH`; do not replace the user's installed tools during tests.

Integration checks remain separate from component unit tests. The dedicated
GitHub Actions workflow runs on pushes to `main` and pull requests targeting
`main`, checks the pinned submodule revisions, and can also be started manually
with `workflow_dispatch`.

## Component checks and builds

Prepare the shared development tools once, then run local component tests from
each package directory:

```bash
ma tools
(cd components/sanelib && ma tests)
(cd components/sanex && ma tests)
(cd components/sanea && ma tests)
```

Component CI invokes pytest directly, without makeapp. Build releases with
`uv build` from the corresponding component directory. Application installation
and updates continue to use Python packages, not this integration repository.

## Component guides

- [sanea development](https://github.com/sanecmp/sanea#readme)
- [sanex development and installation](https://github.com/sanecmp/sanex#readme)
- [shared protocol](https://github.com/sanecmp/sanelib#readme)
