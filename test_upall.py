"""upall 纯函数单测。fixture 均为 2026-07-15 本机真实输出样例。

运行：uv run --with pytest --with rich pytest -q test_upall.py
"""
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
        ("codexbar", "0.41.0", "0.43.0"),  # cask 也纳入
    ]  # pinned-pkg 被排除


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
    ]  # npm 自身随 brew 的 node 走，排除


def test_parse_npm_outdated_empty_output():
    assert upall.parse_npm_outdated("") == []      # 无过期包时 npm 可能输出空
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
    latest = {"a": "1.0", "b": "2.1", "c": None}  # None = PyPI 查询失败
    items = upall.diff_versions(installed, latest)
    assert [(i.pkg, i.current, i.latest) for i in items] == [
        ("b", "2.0", "2.1"),  # 不等 → 可升级
        ("c", "3.0", "?"),    # 查询失败 → 显示 ?，调用方不将其纳入升级
    ]  # a 相等 → 不出现


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
    ]  # Listing... 头行跳过


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
    ]  # Obsoleting Packages 段忽略；.arch 后缀剥离


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
