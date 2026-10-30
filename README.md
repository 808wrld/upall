# upall

One-shot check & upgrade for every package manager on your machine: Homebrew (formulae + casks), npm globals, uv tools, pipx, bun itself, and apt / dnf / pacman on Linux. A single file that works on both macOS and Linux — managers that aren't installed are skipped automatically.

## Usage

    upall              # parallel check → summary table → y/N confirm → sequential upgrade
    upall -y           # skip confirmation, fully automatic
    upall --check      # check only, never upgrade (takes priority over -y)
    upall brew npm     # limit to specific managers (brew / npm / uv / pipx / bun / apt / dnf / pacman)

Exit codes: `0` success or nothing to do; `1` upgrade or check failure, or none of the requested managers is installed; `2` usage error.

## Design notes

- Layered self-updates: `brew update` updates Homebrew itself → brew upgrades the node/uv/pipx binaries → each manager upgrades its own global packages (npm itself is excluded from the npm list; it ships with brew's node)
- gem is not managed (system Ruby on macOS — touching it is risky)
- brew pinned packages are excluded; packages whose PyPI/GitHub lookup fails show `?` and are never upgraded
- uv / pipx / bun versions are compared by release number, so installs that are ahead of the latest published release (pre-releases, local builds) are not reported as outdated
- Checks run in parallel (thread pool); upgrades run sequentially (avoids package-manager lock contention)
- Non-interactive stdin (EOF) counts as cancel; Ctrl-C exits cleanly (130)
- bun is a standalone install (not brew-managed), so its self-update runs `bun upgrade`; the latest version is looked up from GitHub releases
- The Linux trio needs root or passwordless sudo to upgrade (`sudo -n` — it never hangs waiting for a password); checking works without root, and the apt index refresh is best-effort
- apt upgrades keep existing config files (`--force-confdef --force-confold`) and abort instead of removing packages (`--no-remove`); these are command-line options, so they also work through sudo. `DEBIAN_FRONTEND=noninteractive` is set too, but sudo's default `env_reset` drops it, so a remaining debconf question is shown and can be answered
- Upgrade output is streamed as it arrives, including prompts without a trailing newline, so nothing looks hung and prompts can still be answered
- pacman only performs a full `-Syu` (Arch does not support partial upgrades) and prefers `checkupdates` (pacman-contrib) when present
- cargo / gem / mas are out of scope

## Install

    chmod +x upall.py && ln -sf "$PWD/upall.py" ~/.local/bin/upall

Linux servers: install uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`), copy the single `upall.py` over, and use it the same way.

## Tests

    uv run --with pytest --with rich==14.3.4 pytest -q test_upall.py

The `rich` version is pinned in the script header; bump both places together.

Test fixtures are recorded from real command output (macOS host + Ubuntu/Fedora containers).
