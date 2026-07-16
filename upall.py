#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich"]
# ///
"""upall — 一键检查并升级本机包管理器（brew / npm / uv / pipx）。

设计文档：docs/superpowers/specs/2026-07-15-upall-design.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()

BREW_UPDATE_TIMEOUT = 120  # brew update 最慢（拉 git）
CMD_TIMEOUT = 30           # 其它子命令
PYPI_TIMEOUT = 10          # 单个 PyPI 查询


# ---------- 数据结构 ----------

@dataclass
class Outdated:
    pkg: str
    current: str
    latest: str  # 查询失败时为 "?"


@dataclass
class CheckResult:
    manager: str
    items: list[Outdated] = field(default_factory=list)
    error: str | None = None


@dataclass
class UpgradeResult:
    manager: str
    pkg: str
    ok: bool
    detail: str = ""  # 失败时为输出尾部若干行


# ---------- 解析器（纯函数） ----------

def parse_brew_outdated(text: str) -> list[Outdated]:
    """解析 `brew outdated --json=v2`。formula+cask 都要；pinned 排除。"""
    data = json.loads(text)
    items = []
    for entry in data.get("formulae", []) + data.get("casks", []):
        if entry.get("pinned"):
            continue
        installed = entry.get("installed_versions") or ["?"]
        items.append(Outdated(entry["name"], installed[-1], entry["current_version"]))
    return items


def parse_npm_outdated(text: str) -> list[Outdated]:
    """解析 `npm outdated -g --json`。排除 npm 自身（随 brew node 走）。"""
    data = json.loads(text) if text.strip() else {}
    items = []
    for pkg, info in data.items():
        if pkg == "npm":
            continue
        current = info.get("current", "?")
        latest = info.get("latest", "?")
        if current != latest:
            items.append(Outdated(pkg, current, latest))
    return items


UV_TOOL_RE = re.compile(r"^(\S+)\s+v(\S+)")


def parse_uv_tool_list(text: str) -> list[tuple[str, str]]:
    """解析 `uv tool list` 文本：`名字 v版本` 行；`- 命令` 行跳过。"""
    tools = []
    for line in text.splitlines():
        if line.startswith("-"):
            continue
        m = UV_TOOL_RE.match(line)
        if m:
            tools.append((m.group(1), m.group(2)))
    return tools


def parse_pipx_list(text: str) -> list[tuple[str, str]]:
    """解析 `pipx list --json`。"""
    data = json.loads(text)
    out = []
    for venv in data.get("venvs", {}).values():
        mp = venv["metadata"]["main_package"]
        out.append((mp["package"], mp["package_version"]))
    return out


def parse_pypi_version(text: str) -> str:
    return json.loads(text)["info"]["version"]


def diff_versions(
    installed: list[tuple[str, str]], latest: dict[str, str | None]
) -> list[Outdated]:
    """相等性对比（spec 决策：不引入 packaging）。latest=None → '?' 仅展示。"""
    items = []
    for pkg, cur in installed:
        lat = latest.get(pkg)
        if lat is None:
            items.append(Outdated(pkg, cur, "?"))
        elif cur != lat:
            items.append(Outdated(pkg, cur, lat))
    return items


APT_UPGRADABLE_RE = re.compile(r"^(\S+?)/\S+\s+(\S+)\s+\S+\s+\[upgradable from:\s*([^\]]+)\]")


def parse_apt_upgradable(text: str) -> list[Outdated]:
    """解析 `apt list --upgradable`：`包/仓库 最新 架构 [upgradable from: 当前]`。"""
    items = []
    for line in text.splitlines():
        m = APT_UPGRADABLE_RE.match(line.strip())
        if m:
            pkg, latest, current = m.groups()
            items.append(Outdated(pkg, current.strip(), latest))
    return items


def parse_rpm_qa(text: str) -> dict[str, str]:
    """解析 `rpm -qa --qf '%{NAME} %{EVR}\\n'` 为 名字→已装版本。"""
    installed = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            installed[parts[0]] = parts[1]
    return installed


def parse_dnf_check_update(text: str, installed: dict[str, str]) -> list[Outdated]:
    """解析 `dnf -q check-update`：`名字.架构 版本 仓库`；Obsoleting 段起忽略。"""
    items = []
    for line in text.splitlines():
        if line.startswith("Obsoleting"):
            break
        parts = line.split()
        if len(parts) == 3 and "." in parts[0]:
            name = parts[0].rsplit(".", 1)[0]
            items.append(Outdated(name, installed.get(name, "?"), parts[1]))
    return items


PACMAN_OUTDATED_RE = re.compile(r"^(\S+)\s+(\S+)\s+->\s+(\S+)")


def parse_pacman_outdated(text: str) -> list[Outdated]:
    """解析 `checkupdates` / `pacman -Qu`：`包 旧 -> 新`。"""
    items = []
    for line in text.splitlines():
        m = PACMAN_OUTDATED_RE.match(line.strip())
        if m:
            items.append(Outdated(*m.groups()))
    return items


def parse_github_release_tag(text: str, prefix: str = "") -> str:
    """解析 GitHub releases/latest 响应的 tag_name，去掉版本前缀。"""
    return json.loads(text)["tag_name"].removeprefix(prefix)


# ---------- 网络 ----------

def pypi_latest(pkg: str) -> str | None:
    """查 PyPI 最新版；任何失败返回 None（调用方显示 ?）。"""
    url = f"https://pypi.org/pypi/{pkg}/json"
    try:
        with urllib.request.urlopen(url, timeout=PYPI_TIMEOUT) as resp:
            return parse_pypi_version(resp.read().decode())
    except Exception:
        return None


def pypi_latest_many(pkgs: list[str]) -> dict[str, str | None]:
    if not pkgs:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(pypi_latest, p): p for p in pkgs}
        return {futs[f]: f.result() for f in as_completed(futs)}


def github_latest(repo: str, prefix: str = "") -> str | None:
    """查 GitHub 最新 release 版本；任何失败返回 None（调用方显示 ?）。"""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "upall"})
    try:
        with urllib.request.urlopen(req, timeout=PYPI_TIMEOUT) as resp:
            return parse_github_release_tag(resp.read().decode(), prefix)
    except Exception:
        return None


# ---------- subprocess helpers ----------

def run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    """跑一个命令，返回 (退出码, stdout, stderr)。超时/不存在返回码 -1。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"超时（>{timeout}s）"
    except FileNotFoundError:
        return -1, "", "命令不存在"


def stream(cmd: list[str]) -> tuple[int, str]:
    """跑一个命令并实时弱化显示输出，返回 (退出码, 输出尾部5行)。"""
    console.print(f"[bold cyan]$ {escape(' '.join(cmd))}[/]")
    p = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    tail: list[str] = []
    assert p.stdout is not None
    try:
        for line in p.stdout:
            line = line.rstrip()
            console.print(f"[dim]{escape(line)}[/]", highlight=False)
            tail = (tail + [line])[-5:]
    except KeyboardInterrupt:
        p.terminate()
        p.wait()
        raise
    return p.wait(), "\n".join(tail)


def root_prefix() -> list[str]:
    """Linux 系统级包管理需要 root：非 root 时用免密 sudo（-n 不挂起等密码）。"""
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


# ---------- Adapters ----------

class Adapter:
    name: str = ""

    def available(self) -> bool:
        return shutil.which(self.name) is not None

    def check(self) -> CheckResult:
        raise NotImplementedError

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        raise NotImplementedError


class BrewAdapter(Adapter):
    name = "brew"

    def check(self) -> CheckResult:
        code, _, err = run(["brew", "update"], timeout=BREW_UPDATE_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"brew update 失败: {err.strip()[-200:]}")
        code, out, err = run(["brew", "outdated", "--json=v2"], timeout=CMD_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"brew outdated 失败: {err.strip()[-200:]}")
        try:
            return CheckResult(self.name, items=parse_brew_outdated(out))
        except (json.JSONDecodeError, KeyError, TypeError):
            return CheckResult(self.name, error="brew outdated 输出不是预期 JSON")

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        # 一次传入全部（brew 自己会规划依赖顺序）；失败归因整批
        code, tail = stream(["brew", "upgrade", *pkgs])
        ok = code == 0
        return [UpgradeResult(self.name, p, ok, "" if ok else tail) for p in pkgs]


class NpmAdapter(Adapter):
    name = "npm"

    def check(self) -> CheckResult:
        code, out, err = run(["npm", "outdated", "-g", "--json"], timeout=CMD_TIMEOUT)
        # 退出码语义：0=无过期包；1+JSON stdout=有过期包；1+空 stdout=真实错误（勿当"已最新"）
        if code not in (0, 1) or (code == 1 and not out.strip()):
            return CheckResult(self.name, error=f"npm outdated 失败: {err.strip()[-200:]}")
        try:
            return CheckResult(self.name, items=parse_npm_outdated(out))
        except (json.JSONDecodeError, KeyError, TypeError):
            return CheckResult(self.name, error="npm outdated 输出不是合法 JSON")

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        results = []
        for p in pkgs:  # 每包单独执行，便于失败归因
            code, tail = stream(["npm", "install", "-g", f"{p}@latest"])
            results.append(UpgradeResult(self.name, p, code == 0, "" if code == 0 else tail))
        return results


class UvToolAdapter(Adapter):
    name = "uv"

    def check(self) -> CheckResult:
        code, out, err = run(["uv", "tool", "list"], timeout=CMD_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"uv tool list 失败: {err.strip()[-200:]}")
        installed = parse_uv_tool_list(out)
        latest = pypi_latest_many([p for p, _ in installed])
        return CheckResult(self.name, items=diff_versions(installed, latest))

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        results = []
        for p in pkgs:
            code, tail = stream(["uv", "tool", "upgrade", p])
            results.append(UpgradeResult(self.name, p, code == 0, "" if code == 0 else tail))
        return results


class PipxAdapter(Adapter):
    name = "pipx"

    def check(self) -> CheckResult:
        code, out, err = run(["pipx", "list", "--json"], timeout=CMD_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"pipx list 失败: {err.strip()[-200:]}")
        try:
            installed = parse_pipx_list(out)
        except (json.JSONDecodeError, KeyError, TypeError):
            return CheckResult(self.name, error="pipx list 输出不是预期 JSON")
        latest = pypi_latest_many([p for p, _ in installed])
        return CheckResult(self.name, items=diff_versions(installed, latest))

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        results = []
        for p in pkgs:
            code, tail = stream(["pipx", "upgrade", p])
            results.append(UpgradeResult(self.name, p, code == 0, "" if code == 0 else tail))
        return results


class BunAdapter(Adapter):
    name = "bun"
    # bun 为独立安装（~/.bun，非 brew），自更新走 bun upgrade；
    # 本机未使用 bun 全局包（无 global package.json），故只管 bun 本体。

    def check(self) -> CheckResult:
        code, out, err = run(["bun", "--version"], timeout=CMD_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"bun --version 失败: {err.strip()[-200:]}")
        current = out.strip()
        latest = github_latest("oven-sh/bun", prefix="bun-v")
        return CheckResult(self.name, items=diff_versions([("bun", current)], {"bun": latest}))

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        code, tail = stream(["bun", "upgrade"])
        return [UpgradeResult(self.name, "bun", code == 0, "" if code == 0 else tail)]


class LinuxAdapter(Adapter):
    """Linux 系统包管理器公共基类：仅在 Linux 且命令存在时启用。"""

    binary: str = ""  # available() 检测的命令（如 apt 检测 apt-get）

    def available(self) -> bool:
        return sys.platform.startswith("linux") and shutil.which(self.binary or self.name) is not None


class AptAdapter(LinuxAdapter):
    name = "apt"
    binary = "apt-get"

    def check(self) -> CheckResult:
        # 刷新索引尽力而为：非 root 且无免密 sudo 时失败，退回用现有索引（可能略旧）
        run([*root_prefix(), "apt-get", "update"], timeout=BREW_UPDATE_TIMEOUT)
        code, out, err = run(["apt", "list", "--upgradable"], timeout=CMD_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"apt list 失败: {err.strip()[-200:]}")
        return CheckResult(self.name, items=parse_apt_upgradable(out))

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        code, tail = stream(
            [*root_prefix(), "apt-get", "install", "--only-upgrade", "-y", *pkgs])
        ok = code == 0
        return [UpgradeResult(self.name, p, ok, "" if ok else tail) for p in pkgs]


class DnfAdapter(LinuxAdapter):
    name = "dnf"

    def check(self) -> CheckResult:
        # dnf check-update 退出码：0=无更新，100=有更新，其余=错误
        code, out, err = run(["dnf", "-q", "check-update"], timeout=BREW_UPDATE_TIMEOUT)
        if code == 0:
            return CheckResult(self.name)
        if code != 100:
            return CheckResult(self.name, error=f"dnf check-update 失败: {err.strip()[-200:]}")
        rc, rpm_out, _ = run(["rpm", "-qa", "--qf", "%{NAME} %{EVR}\n"], timeout=CMD_TIMEOUT)
        installed = parse_rpm_qa(rpm_out) if rc == 0 else {}
        return CheckResult(self.name, items=parse_dnf_check_update(out, installed))

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        code, tail = stream([*root_prefix(), "dnf", "upgrade", "-y", *pkgs])
        ok = code == 0
        return [UpgradeResult(self.name, p, ok, "" if ok else tail) for p in pkgs]


class PacmanAdapter(LinuxAdapter):
    name = "pacman"

    def check(self) -> CheckResult:
        # 优先 checkupdates（pacman-contrib，免 root 且索引新）；否则 pacman -Qu（用上次同步的索引）
        if shutil.which("checkupdates"):
            code, out, err = run(["checkupdates"], timeout=BREW_UPDATE_TIMEOUT)
            if code == 2:  # checkupdates：2 = 无更新
                return CheckResult(self.name)
        else:
            code, out, err = run(["pacman", "-Qu"], timeout=CMD_TIMEOUT)
            if code == 1 and not out.strip():  # pacman -Qu：无更新时退出 1 且无输出
                return CheckResult(self.name)
        if code != 0:
            return CheckResult(self.name, error=f"pacman 检查失败: {err.strip()[-200:]}")
        return CheckResult(self.name, items=parse_pacman_outdated(out))

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        # Arch 不支持部分升级：一律全量 -Syu，结果归因到列出的包
        code, tail = stream([*root_prefix(), "pacman", "-Syu", "--noconfirm"])
        ok = code == 0
        return [UpgradeResult(self.name, p, ok, "" if ok else tail) for p in pkgs]


ADAPTERS: list[Adapter] = [
    BrewAdapter(), NpmAdapter(), UvToolAdapter(), PipxAdapter(), BunAdapter(),
    AptAdapter(), DnfAdapter(), PacmanAdapter(),
]


# ---------- Orchestrator ----------

def check_all(adapters: list[Adapter]) -> list[CheckResult]:
    """并行跑所有 adapter.check()，按 adapters 顺序返回。"""
    results: dict[str, CheckResult] = {}
    with console.status("[bold]检查中…[/]") as status:
        with ThreadPoolExecutor(max_workers=len(adapters)) as ex:
            futs = {ex.submit(a.check): a for a in adapters}
            done: list[str] = []
            for f in as_completed(futs):
                a = futs[f]
                try:
                    results[a.name] = f.result()
                except Exception as e:  # adapter 内部未捕获的意外
                    results[a.name] = CheckResult(a.name, error=str(e))
                done.append(a.name)
                status.update(f"[bold]检查中… 已完成: {', '.join(done)}[/]")
    return [results[a.name] for a in adapters]


def render_table(results: list[CheckResult]) -> None:
    table = Table(title="过期包检查")
    table.add_column("管理器", style="bold")
    table.add_column("包")
    table.add_column("当前")
    table.add_column("最新", style="green")
    for r in results:
        if r.error:
            table.add_row(r.manager, f"[yellow]⚠️  检查失败: {escape(r.error)}[/]", "", "")
        elif not r.items:
            table.add_row(r.manager, "[dim]✓ 已最新[/]", "", "")
        else:
            for it in r.items:
                latest = escape(it.latest) if it.latest != "?" else "[yellow]?[/]"
                table.add_row(r.manager, escape(it.pkg), escape(it.current), latest)
    console.print(table)


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="upall", description="一键检查并升级本机包管理器（brew/npm/uv/pipx/bun + Linux: apt/dnf/pacman）"
    )
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认直接升级")
    parser.add_argument("--check", action="store_true",
                        help="只检查不升级（与 -y 同给时 --check 优先）")
    parser.add_argument("managers", nargs="*", metavar="manager",
                        help="只处理指定管理器：brew npm uv pipx bun apt dnf pacman")
    args = parser.parse_args()

    by_name = {a.name: a for a in ADAPTERS}
    if args.managers:
        unknown = sorted(set(args.managers) - set(by_name))
        if unknown:
            console.print(
                f"[red]未知管理器: {escape(', '.join(unknown))}（可选: {', '.join(by_name)}）[/]")
            return 2
        selected = [a for a in ADAPTERS if a.name in args.managers]
    else:
        selected = list(ADAPTERS)

    selected = [a for a in selected if a.available()]
    if not selected:
        console.print("[yellow]没有可用的包管理器[/]")
        return 0

    results = check_all(selected)
    render_table(results)

    check_failed = any(r.error for r in results)
    # latest == "?"（PyPI 查询失败）仅展示，不纳入升级
    upgradable = {
        r.manager: [i.pkg for i in r.items if i.latest != "?"]
        for r in results if not r.error
    }
    total = sum(len(v) for v in upgradable.values())

    if total == 0:
        if not check_failed:
            console.print("[green]✅ 全部已是最新[/]")
        return 1 if check_failed else 0

    if args.check:
        return 1 if check_failed else 0

    if not args.yes:
        try:
            resp = console.input(f"[bold]升级全部 {total} 个包？{escape('[y/N]')} [/]")
        except EOFError:  # 非交互（cron / < /dev/null）视同取消
            resp = ""
        if resp.strip().lower() not in ("y", "yes"):
            console.print("已取消")
            return 1 if check_failed else 0

    all_results: list[UpgradeResult] = []
    for a in selected:  # 顺序升级：规避 brew/npm 锁并发问题
        pkgs = upgradable.get(a.name, [])
        if pkgs:
            console.rule(f"[bold]{a.name}[/]")
            try:
                all_results.extend(a.upgrade(pkgs))
            except Exception as e:  # 与 check_all 对称的兜底，保证汇总环节总能执行
                all_results.extend(
                    UpgradeResult(a.name, p, False, str(e)) for p in pkgs
                )

    failed = [r for r in all_results if not r.ok]
    console.rule("[bold]结果[/]")
    console.print(f"成功 [green]{len(all_results) - len(failed)}[/] / 失败 [red]{len(failed)}[/]")
    for r in failed:
        console.print(f"[red]✗ {r.manager}: {escape(r.pkg)}[/]")
        if r.detail:
            console.print(f"[dim]{escape(r.detail)}[/]")
    return 1 if failed or check_failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/]")
        sys.exit(130)
