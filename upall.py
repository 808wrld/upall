#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich==14.3.4"]
# ///
"""upall — check and upgrade every package manager on this machine in one go.

Supported: brew, npm, uv, pipx, bun, plus apt / dnf / pacman on Linux.
Managers that are not installed are skipped automatically.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()

BREW_UPDATE_TIMEOUT = 120  # `brew update` is the slowest step (it pulls git)
CMD_TIMEOUT = 30           # every other subcommand
PYPI_TIMEOUT = 10          # a single PyPI / GitHub lookup

# apt upgrades keep existing config files instead of stopping at dpkg's "keep or
# replace conffile?" prompt, and abort rather than remove packages. These are
# command-line options, so they also apply through sudo.
# DEBIAN_FRONTEND additionally silences debconf questions, but only when it
# reaches apt: as root it always does; through sudo, the default env_reset
# drops it, and any remaining prompt is shown and can be answered.
APT_ENV = {"DEBIAN_FRONTEND": "noninteractive"}
APT_UPGRADE_OPTS = [
    "--no-remove",
    "-o", "Dpkg::Options::=--force-confdef",
    "-o", "Dpkg::Options::=--force-confold",
]


# ---------- Data types ----------

@dataclass
class Outdated:
    pkg: str
    current: str
    latest: str  # "?" when the lookup failed


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
    detail: str = ""  # last few lines of output on failure


# ---------- Parsers (pure functions) ----------

def parse_brew_outdated(text: str) -> list[Outdated]:
    """Parse `brew outdated --json=v2`: formulae and casks, pinned packages excluded."""
    data = json.loads(text)
    items = []
    for entry in data.get("formulae", []) + data.get("casks", []):
        if entry.get("pinned"):
            continue
        installed = entry.get("installed_versions") or ["?"]
        items.append(Outdated(entry["name"], installed[-1], entry["current_version"]))
    return items


def parse_npm_outdated(text: str) -> list[Outdated]:
    """Parse `npm outdated -g --json`. npm itself is excluded (it ships with brew's node)."""
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
    """Parse `uv tool list`: `name vVERSION` lines; `- command` lines are skipped."""
    tools = []
    for line in text.splitlines():
        if line.startswith("-"):
            continue
        m = UV_TOOL_RE.match(line)
        if m:
            tools.append((m.group(1), m.group(2)))
    return tools


def parse_pipx_list(text: str) -> list[tuple[str, str]]:
    """Parse `pipx list --json`."""
    data = json.loads(text)
    out = []
    for venv in data.get("venvs", {}).values():
        mp = venv["metadata"]["main_package"]
        out.append((mp["package"], mp["package_version"]))
    return out


def parse_pypi_version(text: str) -> str:
    return json.loads(text)["info"]["version"]


_RELEASE_RE = re.compile(r"\d+(?:\.\d+)*")


def _release_tuple(version: str) -> tuple[int, ...] | None:
    """Leading numeric release segment of a version: "1.2.3rc1" -> (1, 2, 3)."""
    m = _RELEASE_RE.match(version.strip().lstrip("vV"))
    return tuple(int(part) for part in m.group().split(".")) if m else None


def installed_is_ahead(current: str, latest: str) -> bool:
    """True when the installed release number is strictly newer than the "latest" one.

    Covers pre-releases and local builds that are ahead of the published
    release, which a plain string comparison would report as outdated forever.
    Versions that can't be parsed are never considered ahead.
    """
    cur, lat = _release_tuple(current), _release_tuple(latest)
    if cur is None or lat is None:
        return False
    width = max(len(cur), len(lat))
    cur += (0,) * (width - len(cur))
    lat += (0,) * (width - len(lat))
    return cur > lat


def diff_versions(
    installed: list[tuple[str, str]], latest: dict[str, str | None]
) -> list[Outdated]:
    """Report packages whose installed version differs from the latest one.

    latest=None means the lookup failed: the package is shown with "?" but
    never upgraded. Installs that are ahead of the latest release are skipped.
    """
    items = []
    for pkg, cur in installed:
        lat = latest.get(pkg)
        if lat is None:
            items.append(Outdated(pkg, cur, "?"))
        elif cur != lat and not installed_is_ahead(cur, lat):
            items.append(Outdated(pkg, cur, lat))
    return items


APT_UPGRADABLE_RE = re.compile(r"^(\S+?)/\S+\s+(\S+)\s+\S+\s+\[upgradable from:\s*([^\]]+)\]")


def parse_apt_upgradable(text: str) -> list[Outdated]:
    """Parse `apt list --upgradable`: `pkg/suite LATEST arch [upgradable from: CURRENT]`."""
    items = []
    for line in text.splitlines():
        m = APT_UPGRADABLE_RE.match(line.strip())
        if m:
            pkg, latest, current = m.groups()
            items.append(Outdated(pkg, current.strip(), latest))
    return items


def parse_rpm_qa(text: str) -> dict[str, str]:
    """Parse `rpm -qa --qf '%{NAME} %{EVR}\\n'` into name -> installed version."""
    installed = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            installed[parts[0]] = parts[1]
    return installed


def parse_dnf_check_update(text: str, installed: dict[str, str]) -> list[Outdated]:
    """Parse `dnf -q check-update`: `name.arch VERSION repo`; stops at "Obsoleting".

    dnf wraps long package names: the name sits alone on one line and the
    version + repo follow on the next. Those two lines are joined back up.
    """
    items = []
    pending_name: str | None = None
    for line in text.splitlines():
        if line.startswith("Obsoleting"):
            break
        parts = line.split()
        if pending_name is not None:
            if len(parts) == 2:
                parts = [pending_name, *parts]
            pending_name = None
        if len(parts) == 1 and "." in parts[0]:
            pending_name = parts[0]
            continue
        if len(parts) == 3 and "." in parts[0]:
            name = parts[0].rsplit(".", 1)[0]
            items.append(Outdated(name, installed.get(name, "?"), parts[1]))
    return items


PACMAN_OUTDATED_RE = re.compile(r"^(\S+)\s+(\S+)\s+->\s+(\S+)")


def parse_pacman_outdated(text: str) -> list[Outdated]:
    """Parse `checkupdates` / `pacman -Qu`: `pkg OLD -> NEW`."""
    items = []
    for line in text.splitlines():
        m = PACMAN_OUTDATED_RE.match(line.strip())
        if m:
            items.append(Outdated(*m.groups()))
    return items


def parse_github_release_tag(text: str, prefix: str = "") -> str:
    """Return tag_name from a GitHub releases/latest response, minus a version prefix."""
    return json.loads(text)["tag_name"].removeprefix(prefix)


# ---------- Network ----------

def pypi_latest(pkg: str) -> str | None:
    """Latest version on PyPI; None on any failure (shown as "?")."""
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
    """Latest GitHub release version; None on any failure (shown as "?")."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "upall"})
    try:
        with urllib.request.urlopen(req, timeout=PYPI_TIMEOUT) as resp:
            return parse_github_release_tag(resp.read().decode(), prefix)
    except Exception:
        return None


# ---------- Subprocess helpers ----------

def run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    """Run a command and return (exit code, stdout, stderr); -1 on timeout or missing binary."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out (>{timeout}s)"
    except FileNotFoundError:
        return -1, "", "command not found"


class OutputTail:
    """Keeps the last few complete lines of a streamed output."""

    def __init__(self, size: int = 5) -> None:
        self._lines: deque[str] = deque(maxlen=size)
        self._partial = ""

    def feed(self, text: str) -> None:
        *complete, self._partial = (self._partial + text).split("\n")
        for line in complete:
            self._add(line)

    def _add(self, line: str) -> None:
        # Drop a CRLF line ending first, then keep only the final state of a
        # progress line that redraws itself with "\r".
        line = line.rstrip("\r").rsplit("\r", 1)[-1].rstrip()
        if line:
            self._lines.append(line)

    def text(self) -> str:
        if self._partial:
            self._add(self._partial)
            self._partial = ""
        return "\n".join(self._lines)


class DisplayNewlines:
    """Turns "\\r\\n" and lone "\\r" into "\\n" for display, across chunk boundaries.

    rich drops carriage returns, so without this every redraw of a progress
    bar (brew, curl, pip …) would be glued onto one ever-growing line. Each
    redraw gets its own line instead, as in a text-mode line reader.
    """

    def __init__(self) -> None:
        self._pending_cr = False

    def feed(self, text: str) -> str:
        if self._pending_cr:
            text = "\r" + text
            self._pending_cr = False
        if text.endswith("\r"):  # might be the first half of a "\r\n" split across chunks
            text = text[:-1]
            self._pending_cr = True
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def flush(self) -> str:
        pending, self._pending_cr = self._pending_cr, False
        return "\n" if pending else ""


def stream(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a command, echoing its output dimmed as it arrives; return (exit code, last 5 lines).

    Output is read in raw chunks rather than whole lines, so a prompt that
    doesn't end in a newline (dpkg's "[Y/n]", a sudo message, ...) shows up
    immediately instead of the run looking hung. stdin is inherited, so such
    a prompt can still be answered.
    """
    console.print(f"[bold cyan]$ {escape(' '.join(cmd))}[/]")
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=None if env is None else {**os.environ, **env},
    )
    assert p.stdout is not None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    display = DisplayNewlines()
    tail = OutputTail(5)
    at_line_start = True

    def emit(text: str) -> None:
        nonlocal at_line_start
        tail.feed(text)
        shown = display.feed(text)
        if not shown:
            return
        console.print(f"[dim]{escape(shown)}[/]", end="", highlight=False, emoji=False, soft_wrap=True)
        at_line_start = shown.endswith("\n")

    try:
        while chunk := p.stdout.read1(4096):
            emit(decoder.decode(chunk))
        emit(decoder.decode(b"", final=True) + display.flush())
    except KeyboardInterrupt:
        p.terminate()
        p.wait()
        raise
    if not at_line_start:
        console.print()
    return p.wait(), tail.text()


def root_prefix() -> list[str]:
    """System package managers need root: use passwordless sudo when not root (-n never hangs)."""
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
            return CheckResult(self.name, error=f"brew update failed: {err.strip()[-200:]}")
        code, out, err = run(["brew", "outdated", "--json=v2"], timeout=CMD_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"brew outdated failed: {err.strip()[-200:]}")
        try:
            return CheckResult(self.name, items=parse_brew_outdated(out))
        except (json.JSONDecodeError, KeyError, TypeError):
            return CheckResult(self.name, error="brew outdated did not return the expected JSON")

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        # One call for everything (brew orders dependencies itself); a failure fails the batch.
        code, tail = stream(["brew", "upgrade", *pkgs])
        ok = code == 0
        return [UpgradeResult(self.name, p, ok, "" if ok else tail) for p in pkgs]


class NpmAdapter(Adapter):
    name = "npm"

    def check(self) -> CheckResult:
        code, out, err = run(["npm", "outdated", "-g", "--json"], timeout=CMD_TIMEOUT)
        # Exit codes: 0 = nothing outdated; 1 + JSON on stdout = some outdated;
        # 1 + empty stdout = a real error (must not be reported as "up to date").
        if code not in (0, 1) or (code == 1 and not out.strip()):
            return CheckResult(self.name, error=f"npm outdated failed: {err.strip()[-200:]}")
        try:
            return CheckResult(self.name, items=parse_npm_outdated(out))
        except (json.JSONDecodeError, KeyError, TypeError):
            return CheckResult(self.name, error="npm outdated did not return valid JSON")

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        results = []
        for p in pkgs:  # one package per call so failures are attributed correctly
            code, tail = stream(["npm", "install", "-g", f"{p}@latest"])
            results.append(UpgradeResult(self.name, p, code == 0, "" if code == 0 else tail))
        return results


class UvToolAdapter(Adapter):
    name = "uv"

    def check(self) -> CheckResult:
        code, out, err = run(["uv", "tool", "list"], timeout=CMD_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"uv tool list failed: {err.strip()[-200:]}")
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
            return CheckResult(self.name, error=f"pipx list failed: {err.strip()[-200:]}")
        try:
            installed = parse_pipx_list(out)
        except (json.JSONDecodeError, KeyError, TypeError):
            return CheckResult(self.name, error="pipx list did not return the expected JSON")
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
    # bun is a standalone install (~/.bun, not brew) that updates itself with
    # `bun upgrade`. Only bun itself is managed, not bun global packages.

    def check(self) -> CheckResult:
        code, out, err = run(["bun", "--version"], timeout=CMD_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"bun --version failed: {err.strip()[-200:]}")
        current = out.strip()
        latest = github_latest("oven-sh/bun", prefix="bun-v")
        return CheckResult(self.name, items=diff_versions([("bun", current)], {"bun": latest}))

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        code, tail = stream(["bun", "upgrade"])
        return [UpgradeResult(self.name, "bun", code == 0, "" if code == 0 else tail)]


class LinuxAdapter(Adapter):
    """Base class for Linux system package managers: enabled only on Linux when the binary exists."""

    binary: str = ""  # binary checked by available() (apt checks apt-get)

    def available(self) -> bool:
        return sys.platform.startswith("linux") and shutil.which(self.binary or self.name) is not None


class AptAdapter(LinuxAdapter):
    name = "apt"
    binary = "apt-get"

    def check(self) -> CheckResult:
        # Index refresh is best effort: without root or passwordless sudo it fails
        # and the existing (possibly slightly stale) index is used.
        run([*root_prefix(), "apt-get", "update"], timeout=BREW_UPDATE_TIMEOUT)
        code, out, err = run(["apt", "list", "--upgradable"], timeout=CMD_TIMEOUT)
        if code != 0:
            return CheckResult(self.name, error=f"apt list failed: {err.strip()[-200:]}")
        return CheckResult(self.name, items=parse_apt_upgradable(out))

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        code, tail = stream(
            [*root_prefix(), "apt-get", "install", "--only-upgrade", "-y", *APT_UPGRADE_OPTS, *pkgs],
            env=APT_ENV,
        )
        ok = code == 0
        return [UpgradeResult(self.name, p, ok, "" if ok else tail) for p in pkgs]


class DnfAdapter(LinuxAdapter):
    name = "dnf"

    def check(self) -> CheckResult:
        # dnf check-update exit codes: 0 = no updates, 100 = updates available, anything else = error
        code, out, err = run(["dnf", "-q", "check-update"], timeout=BREW_UPDATE_TIMEOUT)
        if code == 0:
            return CheckResult(self.name)
        if code != 100:
            return CheckResult(self.name, error=f"dnf check-update failed: {err.strip()[-200:]}")
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
        # Prefer checkupdates (pacman-contrib: no root, fresh index); otherwise
        # fall back to pacman -Qu (uses the last synced index).
        if shutil.which("checkupdates"):
            code, out, err = run(["checkupdates"], timeout=BREW_UPDATE_TIMEOUT)
            if code == 2:  # checkupdates: 2 = no updates
                return CheckResult(self.name)
        else:
            code, out, err = run(["pacman", "-Qu"], timeout=CMD_TIMEOUT)
            if code == 1 and not out.strip():  # pacman -Qu: exit 1 with no output = no updates
                return CheckResult(self.name)
        if code != 0:
            return CheckResult(self.name, error=f"pacman check failed: {err.strip()[-200:]}")
        return CheckResult(self.name, items=parse_pacman_outdated(out))

    def upgrade(self, pkgs: list[str]) -> list[UpgradeResult]:
        # Arch doesn't support partial upgrades: always run a full -Syu and
        # attribute the result to the listed packages.
        code, tail = stream([*root_prefix(), "pacman", "-Syu", "--noconfirm"])
        ok = code == 0
        return [UpgradeResult(self.name, p, ok, "" if ok else tail) for p in pkgs]


ADAPTERS: list[Adapter] = [
    BrewAdapter(), NpmAdapter(), UvToolAdapter(), PipxAdapter(), BunAdapter(),
    AptAdapter(), DnfAdapter(), PacmanAdapter(),
]


# ---------- Orchestrator ----------

def check_all(adapters: list[Adapter]) -> list[CheckResult]:
    """Run every adapter.check() in parallel; results come back in adapter order."""
    results: dict[str, CheckResult] = {}
    with console.status("[bold]Checking…[/]") as status:
        with ThreadPoolExecutor(max_workers=len(adapters)) as ex:
            futs = {ex.submit(a.check): a for a in adapters}
            done: list[str] = []
            for f in as_completed(futs):
                a = futs[f]
                try:
                    results[a.name] = f.result()
                except Exception as e:  # unexpected error inside an adapter
                    results[a.name] = CheckResult(a.name, error=str(e))
                done.append(a.name)
                status.update(f"[bold]Checking… done: {', '.join(done)}[/]")
    return [results[a.name] for a in adapters]


def render_table(results: list[CheckResult]) -> None:
    table = Table(title="Outdated packages")
    table.add_column("Manager", style="bold")
    table.add_column("Package")
    table.add_column("Current")
    table.add_column("Latest", style="green")
    for r in results:
        if r.error:
            table.add_row(r.manager, f"[yellow]⚠️  check failed: {escape(r.error)}[/]", "", "")
        elif not r.items:
            table.add_row(r.manager, "[dim]✓ up to date[/]", "", "")
        else:
            for it in r.items:
                latest = escape(it.latest) if it.latest != "?" else "[yellow]?[/]"
                table.add_row(r.manager, escape(it.pkg), escape(it.current), latest)
    console.print(table)


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="upall",
        description="Check and upgrade every package manager on this machine "
                    "(brew/npm/uv/pipx/bun, plus apt/dnf/pacman on Linux)",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="upgrade without asking for confirmation")
    parser.add_argument("--check", action="store_true",
                        help="check only, never upgrade (wins over -y)")
    parser.add_argument("managers", nargs="*", metavar="manager",
                        help="only handle these managers: brew npm uv pipx bun apt dnf pacman")
    args = parser.parse_args()

    by_name = {a.name: a for a in ADAPTERS}
    if args.managers:
        unknown = sorted(set(args.managers) - set(by_name))
        if unknown:
            console.print(
                f"[red]Unknown manager(s): {escape(', '.join(unknown))} (choose from: {', '.join(by_name)})[/]")
            return 2
        requested = [a for a in ADAPTERS if a.name in args.managers]
    else:
        requested = list(ADAPTERS)

    selected = [a for a in requested if a.available()]
    if args.managers:
        missing = [a.name for a in requested if a not in selected]
        if missing:
            console.print(
                f"[yellow]Not installed or not supported on this system: {escape(', '.join(missing))}[/]")
        if not selected:
            return 1
    if not selected:
        console.print("[yellow]No supported package managers found[/]")
        return 0

    results = check_all(selected)
    render_table(results)

    check_failed = any(r.error for r in results)
    # latest == "?" (lookup failed) is shown only, never upgraded
    upgradable = {
        r.manager: [i.pkg for i in r.items if i.latest != "?"]
        for r in results if not r.error
    }
    total = sum(len(v) for v in upgradable.values())

    if total == 0:
        if not check_failed:
            console.print("[green]✅ Everything is up to date[/]")
        return 1 if check_failed else 0

    if args.check:
        return 1 if check_failed else 0

    if not args.yes:
        try:
            resp = console.input(f"[bold]Upgrade all {total} package(s)? {escape('[y/N]')} [/]")
        except EOFError:  # non-interactive stdin (cron, < /dev/null) counts as cancel
            resp = ""
        if resp.strip().lower() not in ("y", "yes"):
            console.print("Cancelled")
            return 1 if check_failed else 0

    all_results: list[UpgradeResult] = []
    for a in selected:  # sequential on purpose: avoids brew/npm lock contention
        pkgs = upgradable.get(a.name, [])
        if pkgs:
            console.rule(f"[bold]{a.name}[/]")
            try:
                all_results.extend(a.upgrade(pkgs))
            except Exception as e:  # mirrors check_all: the summary must always run
                all_results.extend(
                    UpgradeResult(a.name, p, False, str(e)) for p in pkgs
                )

    failed = [r for r in all_results if not r.ok]
    console.rule("[bold]Summary[/]")
    console.print(f"Succeeded [green]{len(all_results) - len(failed)}[/] / failed [red]{len(failed)}[/]")
    for r in failed:
        console.print(f"[red]✗ {r.manager}: {escape(r.pkg)}[/]")
        if r.detail:
            console.print(f"[dim]{escape(r.detail)}[/]")
    return 1 if failed or check_failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/]")
        sys.exit(130)
