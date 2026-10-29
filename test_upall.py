"""Unit tests for upall's pure functions. Fixtures are real command output recorded on 2026-07-15.

Run: uv run --with pytest --with rich pytest -q test_upall.py
"""
import sys

import pytest

import upall


def test_smoke_import():
    assert callable(upall.main)


BREW_SAMPLE = """
{
  "formulae": [
    {"name": "c-ares", "installed_versions": ["1.34.6"], "current_version": "1.34.8",
     "pinned": false, "pinned_version": null},
    {"name": "pinned-pkg", "installed_versions": ["1.0.0"], "current_version": "2.0.0",
     "pinned": true, "pinned_version": "1.0.0"}
  ],
  "casks": [
    {"name": "codexbar", "installed_versions": ["0.41.0"], "current_version": "0.43.0",
     "pinned": false, "pinned_version": null}
  ]
}
"""


def test_parse_brew_outdated():
    items = upall.parse_brew_outdated(BREW_SAMPLE)
    assert [(i.pkg, i.current, i.latest) for i in items] == [
        ("c-ares", "1.34.6", "1.34.8"),   # formula
        ("codexbar", "0.41.0", "0.43.0"),  # casks are included
    ]  # pinned-pkg is excluded


NPM_SAMPLE = """
{
  "@jackwener/opencli": {"current": "1.8.4", "wanted": "1.8.6", "latest": "1.8.6",
    "dependent": "global", "location": "/opt/homebrew/lib/node_modules/@jackwener/opencli"},
  "npm": {"current": "11.16.0", "wanted": "12.0.1", "latest": "12.0.1",
    "dependent": "global", "location": "/opt/homebrew/lib/node_modules/npm"},
  "undici": {"current": "8.5.0", "wanted": "8.7.0", "latest": "8.7.0",
    "dependent": "global", "location": "/opt/homebrew/lib/node_modules/undici"}
}
"""


def test_parse_npm_outdated_excludes_npm_itself():
    items = upall.parse_npm_outdated(NPM_SAMPLE)
    assert [(i.pkg, i.current, i.latest) for i in items] == [
        ("@jackwener/opencli", "1.8.4", "1.8.6"),
        ("undici", "8.5.0", "8.7.0"),
    ]  # npm itself ships with brew's node, so it's excluded


def test_parse_npm_outdated_empty_output():
    assert upall.parse_npm_outdated("") == []      # npm may print nothing when nothing is outdated
    assert upall.parse_npm_outdated("{}") == []


UV_TOOL_SAMPLE = """bilibili-cli v0.6.2
- bili
twitter-cli v0.8.5
- twitter
"""

PIPX_SAMPLE = """
{"venvs": {
  "bilibili-cli": {"metadata": {"main_package":
    {"package": "bilibili-cli", "package_version": "0.6.2"}}},
  "twitter-cli": {"metadata": {"main_package":
    {"package": "twitter-cli", "package_version": "0.8.5"}}}
}}
"""


def test_parse_uv_tool_list():
    assert upall.parse_uv_tool_list(UV_TOOL_SAMPLE) == [
        ("bilibili-cli", "0.6.2"),
        ("twitter-cli", "0.8.5"),
    ]


def test_parse_uv_tool_list_empty():
    assert upall.parse_uv_tool_list("No tools installed\n") == []
    assert upall.parse_uv_tool_list("") == []


def test_parse_pipx_list():
    assert upall.parse_pipx_list(PIPX_SAMPLE) == [
        ("bilibili-cli", "0.6.2"),
        ("twitter-cli", "0.8.5"),
    ]


def test_parse_pipx_list_no_venvs():
    assert upall.parse_pipx_list('{"venvs": {}}') == []


def test_parse_pypi_version():
    assert upall.parse_pypi_version('{"info": {"version": "0.6.2"}}') == "0.6.2"


def test_diff_versions():
    installed = [("a", "1.0"), ("b", "2.0"), ("c", "3.0")]
    latest = {"a": "1.0", "b": "2.1", "c": None}  # None = PyPI lookup failed
    items = upall.diff_versions(installed, latest)
    assert [(i.pkg, i.current, i.latest) for i in items] == [
        ("b", "2.0", "2.1"),  # different -> upgradable
        ("c", "3.0", "?"),    # lookup failed -> shown as ?, never upgraded by the caller
    ]  # a is equal -> not listed


def test_parse_github_release_tag():
    assert upall.parse_github_release_tag(
        '{"tag_name": "bun-v1.3.14"}', prefix="bun-v") == "1.3.14"
    assert upall.parse_github_release_tag('{"tag_name": "v2.0"}') == "v2.0"


APT_SAMPLE = """Listing...
gzip/noble-updates,noble-security 1.12-1ubuntu3.2 arm64 [upgradable from: 1.12-1ubuntu3.1]
tar/noble-updates,noble-security 1.35+dfsg-3ubuntu0.2 arm64 [upgradable from: 1.35+dfsg-3build1]
"""

DNF_SAMPLE = """xz.aarch64      1:5.8.1-4.fc41 updates
xz-libs.aarch64 1:5.8.1-4.fc41 updates

Obsoleting Packages
old-pkg.noarch  1.0-1.fc41 updates
"""

RPM_QA_SAMPLE = """libgcc 14.3.1-4.fc41
xz 1:5.8.1-4.fc40
xz-libs 1:5.8.1-4.fc40
"""

PACMAN_SAMPLE = """linux 6.1.1-1 -> 6.1.2-1
pacman 6.0.2-5 -> 6.0.2-6
"""


def test_parse_apt_upgradable():
    items = upall.parse_apt_upgradable(APT_SAMPLE)
    assert [(i.pkg, i.current, i.latest) for i in items] == [
        ("gzip", "1.12-1ubuntu3.1", "1.12-1ubuntu3.2"),
        ("tar", "1.35+dfsg-3build1", "1.35+dfsg-3ubuntu0.2"),
    ]  # the 'Listing...' header is skipped


def test_parse_apt_upgradable_empty():
    assert upall.parse_apt_upgradable("Listing...\n") == []
    assert upall.parse_apt_upgradable("") == []


def test_parse_rpm_qa():
    assert upall.parse_rpm_qa(RPM_QA_SAMPLE) == {
        "libgcc": "14.3.1-4.fc41",
        "xz": "1:5.8.1-4.fc40",
        "xz-libs": "1:5.8.1-4.fc40",
    }


def test_parse_dnf_check_update():
    items = upall.parse_dnf_check_update(
        DNF_SAMPLE, installed={"xz": "1:5.8.1-4.fc40", "xz-libs": "1:5.8.1-4.fc40"})
    assert [(i.pkg, i.current, i.latest) for i in items] == [
        ("xz", "1:5.8.1-4.fc40", "1:5.8.1-4.fc41"),
        ("xz-libs", "1:5.8.1-4.fc40", "1:5.8.1-4.fc41"),
    ]  # 'Obsoleting Packages' section ignored; .arch suffix stripped


def test_parse_dnf_check_update_unknown_current():
    items = upall.parse_dnf_check_update("foo.x86_64 2.0-1.fc41 updates\n", installed={})
    assert [(i.pkg, i.current, i.latest) for i in items] == [("foo", "?", "2.0-1.fc41")]


def test_parse_pacman_outdated():
    items = upall.parse_pacman_outdated(PACMAN_SAMPLE)
    assert [(i.pkg, i.current, i.latest) for i in items] == [
        ("linux", "6.1.1-1", "6.1.2-1"),
        ("pacman", "6.0.2-5", "6.0.2-6"),
    ]
    assert upall.parse_pacman_outdated("") == []


def test_diff_versions_skips_installs_ahead_of_latest():
    installed = [("ruff", "0.6.0rc1"), ("tool", "2.0.0"), ("pre", "1.0.0rc1"), ("odd", "main")]
    latest = {"ruff": "0.5.9", "tool": "1.9.9", "pre": "1.0.0", "odd": "1.0"}
    items = upall.diff_versions(installed, latest)
    assert [(i.pkg, i.current, i.latest) for i in items] == [
        ("pre", "1.0.0rc1", "1.0.0"),  # same release, pre-release installed -> still upgradable
        ("odd", "main", "1.0"),        # unparseable -> falls back to plain inequality
    ]  # ruff / tool are ahead of the published release -> not listed


@pytest.mark.parametrize(
    ("current", "latest", "ahead"),
    [
        ("1.2.10", "1.2.9", True),
        ("1.2", "1.2.0", False),
        ("v2.0", "1.9", True),
        ("1.3.14", "1.3.14", False),
        ("1.0", "?", False),
    ],
)
def test_installed_is_ahead(current, latest, ahead):
    assert upall.installed_is_ahead(current, latest) is ahead


DNF_WRAPPED_SAMPLE = """python3-some-really-long-package-name-devel.x86_64
                         3.12.1-1.fc41            updates
xz.x86_64 5.8.1-4.fc41 updates
"""


def test_parse_dnf_check_update_joins_wrapped_lines():
    items = upall.parse_dnf_check_update(DNF_WRAPPED_SAMPLE, installed={"xz": "5.8.1-3.fc41"})
    assert [(i.pkg, i.current, i.latest) for i in items] == [
        ("python3-some-really-long-package-name-devel", "?", "3.12.1-1.fc41"),
        ("xz", "5.8.1-3.fc41", "5.8.1-4.fc41"),
    ]


def test_output_tail_keeps_last_lines_and_final_progress_state():
    tail = upall.OutputTail(3)
    tail.feed("one\ntwo\nprogress 10%\rprogress 100%\nfou")
    tail.feed("r\nKeep conffile? [Y/n] ")
    assert tail.text() == "progress 100%\nfour\nKeep conffile? [Y/n]"


@pytest.mark.skipif(sys.platform == "win32", reason="uses sh")
def test_stream_returns_exit_code_and_tail_including_unterminated_prompt():
    code, tail = upall.stream(["sh", "-c", "echo first; printf 'Continue? [Y/n] '; exit 3"])
    assert code == 3
    assert tail == "first\nContinue? [Y/n]"


def test_stream_passes_extra_env():
    code, tail = upall.stream(["sh", "-c", 'echo "$UPALL_TEST_VAR"'], env={"UPALL_TEST_VAR": "hello"})
    assert (code, tail) == (0, "hello")


class _MissingAdapter(upall.Adapter):
    name = "dnf"

    def available(self) -> bool:
        return False


def test_main_reports_requested_manager_that_is_not_installed(monkeypatch, capsys):
    monkeypatch.setattr(upall, "ADAPTERS", [_MissingAdapter()])
    monkeypatch.setattr(sys, "argv", ["upall", "dnf"])
    assert upall.main() == 1
