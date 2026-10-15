"""Exercise the repository CLI against isolated local Git remotes."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[1]


@dataclass
class Repository:
    checkout: Path
    initial_revision: str
    latest_revision: str
    environment: dict[str, str]
    component_name: str = "sanex"


def run_git(directory: Path, *arguments: str, environment: dict[str, str]) -> str:
    result = subprocess.run(
        [
            "git", "-c", "user.name=Integration test", "-c", "user.email=integration@example.invalid",
            "-c", "commit.gpgsign=false", "-C", f"{directory}", *arguments,
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def run_repoutil(repository: Repository, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    script = repository.checkout / "repoutil.sh"
    return subprocess.run(
        [f"{script}", *arguments],
        cwd=cwd,
        env=repository.environment,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repository(tmp_path: Path, request: pytest.FixtureRequest) -> Repository:
    component_name = getattr(request, "param", "sanex")
    environment = {
        **{name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_TERMINAL_PROMPT": "0",
    }
    checkout = tmp_path / "saneinteg"
    checkout.mkdir()
    run_git(checkout, "init", "-b", "main", environment=environment)
    script = checkout / "repoutil.sh"
    script.write_bytes((PROJECT_ROOT / "repoutil.sh").read_bytes())
    script.chmod(0o755)

    for component in ("sanea", "sanex", "sanelib", "landing"):
        source = tmp_path / "sources" / component
        source.mkdir(parents=True)
        run_git(source, "init", "-b", "main", environment=environment)
        (source / "application.txt").write_text("initial\n")
        run_git(source, "add", "application.txt", environment=environment)
        run_git(source, "commit", "-m", "Initial component", environment=environment)
        run_git(checkout, "submodule", "add", f"{source}", f"components/{component}", environment=environment)

    run_git(checkout, "add", "repoutil.sh", environment=environment)
    run_git(checkout, "commit", "-m", "Pin components", environment=environment)
    source = tmp_path / "sources" / component_name
    initial_revision = run_git(source, "rev-parse", "HEAD", environment=environment)
    (source / "application.txt").write_text("updated\n")
    run_git(source, "commit", "-am", "New component revision", environment=environment)
    latest_revision = run_git(source, "rev-parse", "HEAD", environment=environment)
    run_git(source, "tag", "release", environment=environment)
    return Repository(checkout, initial_revision, latest_revision, environment, component_name)


@pytest.fixture(params=["unstaged", "staged", "untracked", "unpublished"])
def dirty_component(repository: Repository, request: pytest.FixtureRequest) -> tuple[Path, str]:
    component = repository.checkout / "components" / repository.component_name
    change = request.param
    filename = "new.txt" if change == "untracked" else "application.txt"
    changed_file = component / filename
    changed_file.write_text("local change\n")
    message = f"{repository.component_name} has local changes"

    if change == "staged":
        run_git(component, "add", filename, environment=repository.environment)

    elif change == "unpublished":
        run_git(component, "commit", "-am", "Unpublished change", environment=repository.environment)
        message = "not present on origin"

    return changed_file, message


def test_init_uses_recorded_revisions_from_an_unrelated_directory(repository: Repository, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    run_git(
        tmp_path, "clone", "--no-recurse-submodules", f"{repository.checkout}", f"{clone}",
        environment=repository.environment,
    )
    cloned_repository = Repository(clone, repository.initial_revision, repository.latest_revision, repository.environment)

    result = run_repoutil(cloned_repository, "init", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    actual_revision = run_git(clone / "components" / "sanex", "rev-parse", "HEAD", environment=repository.environment)
    assert actual_revision == repository.initial_revision
    assert run_git(clone, "status", "--porcelain", environment=repository.environment) == ""


@pytest.mark.parametrize("component_name", ["sanex", "landing"])
def test_status_reports_local_changes_without_modifying_them(
    repository: Repository, tmp_path: Path, component_name: str,
) -> None:
    changed_file = repository.checkout / "components" / component_name / "new.txt"
    changed_file.write_text("local change\n")

    result = run_repoutil(repository, "status", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert repository.initial_revision in result.stdout
    assert "new.txt" in result.stdout
    assert changed_file.read_text() == "local change\n"


@pytest.mark.parametrize("repository", ["sanex", "landing"], indirect=True)
@pytest.mark.parametrize("revision_kind", ["branch", "tag", "commit"])
def test_update_fetches_one_component_without_staging_its_pointer(
    repository: Repository, revision_kind: str, tmp_path: Path,
) -> None:
    revision = {"branch": "origin/main", "tag": "release", "commit": repository.latest_revision}[revision_kind]

    result = run_repoutil(repository, "update", repository.component_name, revision, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    pointer = f"components/{repository.component_name}"
    component = repository.checkout / pointer
    assert run_git(component, "rev-parse", "HEAD", environment=repository.environment) == repository.latest_revision
    recorded_revision = run_git(repository.checkout, "rev-parse", f":{pointer}", environment=repository.environment)
    assert recorded_revision == repository.initial_revision
    assert run_git(repository.checkout, "diff", "--cached", "--name-only", environment=repository.environment) == ""
    assert run_git(repository.checkout, "diff", "--name-only", environment=repository.environment) == pointer


@pytest.mark.parametrize("repository", ["sanex", "landing"], indirect=True)
@pytest.mark.parametrize("command", ["init", "update"])
def test_switching_refuses_local_changes(
    repository: Repository, dirty_component: tuple[Path, str], command: str, tmp_path: Path,
) -> None:
    changed_file, message = dirty_component
    arguments = {"init": ("init",), "update": ("update", repository.component_name, "origin/main")}[command]
    current_revision = run_git(changed_file.parent, "rev-parse", "HEAD", environment=repository.environment)

    result = run_repoutil(repository, *arguments, cwd=tmp_path)

    assert result.returncode == 1
    assert message in result.stderr
    assert changed_file.read_text() == "local change\n"
    actual_revision = run_git(changed_file.parent, "rev-parse", "HEAD", environment=repository.environment)
    assert actual_revision == current_revision


@pytest.mark.parametrize("arguments", [
    ("unexpected",),
    ("update", "sanex"),
    ("update", "../sanex", "origin/main"),
    ("update", "sanex", "--force"),
    ("update", "sanex", "missing-revision"),
])
def test_invalid_arguments_do_not_switch_a_component(
    repository: Repository, arguments: tuple[str, ...], tmp_path: Path,
) -> None:
    result = run_repoutil(repository, *arguments, cwd=tmp_path)

    assert result.returncode != 0
    actual_revision = run_git(
        repository.checkout / "components" / "sanex", "rev-parse", "HEAD", environment=repository.environment,
    )
    assert actual_revision == repository.initial_revision
