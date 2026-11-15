#!/usr/bin/env python3
"""Update Homebrew cask versions and checksums from GitHub Releases.

The script only rewrites the ``version`` and ``sha256`` lines of a cask. All
release metadata is validated before anything is written, and a missing or
unexpected artifact is treated as an error rather than a reason to guess.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

API_ROOT = "https://api.github.com"
USER_AGENT = "alvarolorentedev-homebrew-tap-updater"
SEMVER_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_LINE = re.compile(r"^[ \t]*([0-9a-fA-F]{64})(?:[ \t]+\*?(\S+))?[ \t]*$", re.MULTILINE)
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CASK_TOKEN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class UpdateError(Exception):
    """A cask or release cannot be updated safely."""


class ApiError(UpdateError):
    """The GitHub API request failed."""


class RateLimitError(ApiError):
    """The GitHub API rate limit was reached."""


@dataclass(frozen=True)
class App:
    name: str
    repo: str
    cask: str
    asset: str

    def asset_name(self, version: str) -> str:
        return self.asset.format(version=version)


@dataclass(frozen=True)
class Release:
    version: str
    version_tuple: tuple[int, int, int]
    tag: str
    assets: list[dict]
    asset: dict


@dataclass(frozen=True)
class Change:
    name: str
    display_name: str
    cask: Path
    old_version: str
    new_version: str
    checksum: str


def _version_tuple(version: str) -> tuple[int, int, int] | None:
    match = SEMVER_TAG.fullmatch(version)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _normalize_sha256(value: object, source: str) -> str:
    if not isinstance(value, str):
        raise UpdateError(f"{source}: checksum is not a string")
    algorithm, separator, digest = value.partition(":")
    if separator and algorithm.lower() != "sha256":
        raise UpdateError(f"{source}: unsupported checksum algorithm {algorithm!r}")
    digest = digest.lower() if separator else value.lower()
    if not SHA256_HEX.fullmatch(digest):
        raise UpdateError(f"{source}: invalid SHA-256 checksum")
    return digest


def load_apps(path: Path) -> list[App]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise UpdateError(f"config not found: {path}") from error
    except json.JSONDecodeError as error:
        raise UpdateError(f"config is not valid JSON: {path}: {error}") from error

    entries = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise UpdateError(f"config must contain a non-empty 'apps' list: {path}")

    apps: list[App] = []
    names: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"{path}: apps[{index}]"
        if not isinstance(entry, dict):
            raise UpdateError(f"{where}: must be an object")

        name = entry.get("name")
        repo = entry.get("repo")
        cask = entry.get("cask")
        asset = entry.get("asset")

        if not isinstance(name, str) or not CASK_TOKEN.fullmatch(name):
            raise UpdateError(f"{where}: invalid cask name")
        if name in names:
            raise UpdateError(f"{where}: duplicate cask name {name!r}")
        names.add(name)

        if not isinstance(repo, str) or not REPO_NAME.fullmatch(repo):
            raise UpdateError(f"{where}: repo must be 'owner/name'")
        if not isinstance(cask, str) or not cask.endswith(".rb"):
            raise UpdateError(f"{where}: cask must be a relative .rb path")
        cask_path = Path(cask)
        if cask_path.is_absolute() or ".." in cask_path.parts:
            raise UpdateError(f"{where}: cask path must stay inside the repository")
        if not isinstance(asset, str) or not asset:
            raise UpdateError(f"{where}: asset must be a non-empty filename template")

        try:
            example_asset = asset.format(version="0.0.0")
        except (KeyError, IndexError, ValueError) as error:
            raise UpdateError(f"{where}: invalid asset template: {error}") from error
        if not example_asset.endswith(".dmg"):
            raise UpdateError(f"{where}: asset template must produce a .dmg filename")

        apps.append(App(name=name, repo=repo, cask=cask, asset=asset))

    return apps


def cask_field(text: str, field: str, *, required: bool = True) -> str | None:
    pattern = re.compile(
        rf'^([ \t]*){re.escape(field)}[ \t]+"([^"]*)"[ \t]*$',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        if required:
            raise UpdateError(f"cask is missing the {field} stanza")
        return None
    if len(matches) > 1:
        raise UpdateError(f"cask contains more than one {field} stanza")
    return matches[0].group(2)


def set_cask_field(text: str, field: str, value: str) -> str:
    pattern = re.compile(
        rf'^([ \t]*){re.escape(field)}[ \t]+"[^"]*"[ \t]*$',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise UpdateError(f"cask must contain exactly one quoted {field} stanza")
    match = matches[0]
    return text[: match.start()] + f'{match.group(1)}{field} "{value}"' + text[match.end() :]


def fetch_releases(client: "GitHub", repo: str) -> list:
    releases: list = []
    for page in range(1, 11):
        url = f"{API_ROOT}/repos/{repo}/releases?per_page=100&page={page}"
        data = client.get_json(url)
        if not isinstance(data, list):
            raise ApiError(f"{repo}: GitHub releases response is not a list")
        releases.extend(data)
        if len(data) < 100:
            return releases
    raise ApiError(f"{repo}: release pagination limit reached")


def select_release(app: App, releases: object) -> Release:
    if not isinstance(releases, list):
        raise ApiError(f"{app.name}: GitHub releases response is not a list")

    candidates: list[tuple[tuple[int, int, int], str, dict]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        version_tuple = _version_tuple(tag)
        if version_tuple is None:
            continue
        candidates.append((version_tuple, tag, release))

    if not candidates:
        raise UpdateError(f"{app.name}: no stable semantic-version releases found")

    version_tuple, tag, release = max(
        candidates,
        key=lambda candidate: (
            candidate[0],
            candidate[2].get("published_at") or "",
        ),
    )
    version = ".".join(str(part) for part in version_tuple)
    expected_asset = app.asset_name(version)

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError(f"{app.name}: release {tag} has no asset list")
    asset = next(
        (candidate for candidate in assets if isinstance(candidate, dict) and candidate.get("name") == expected_asset),
        None,
    )
    if asset is None:
        available = sorted(
            candidate.get("name", "?") for candidate in assets if isinstance(candidate, dict)
        )
        raise UpdateError(
            f"{app.name}: release {tag} is missing expected asset {expected_asset!r}; "
            f"available assets: {', '.join(available) or '(none)'}"
        )
    if asset.get("state") != "uploaded":
        raise UpdateError(f"{app.name}: asset {expected_asset!r} is not uploaded")
    if not isinstance(asset.get("size"), int) or asset["size"] <= 0:
        raise UpdateError(f"{app.name}: asset {expected_asset!r} has an invalid size")

    return Release(
        version=version,
        version_tuple=version_tuple,
        tag=tag,
        assets=assets,
        asset=asset,
    )


def asset_url(app: App, asset: dict) -> str:
    url = asset.get("browser_download_url")
    expected_prefix = f"https://github.com/{app.repo}/releases/download/"
    if not isinstance(url, str) or not url.startswith(expected_prefix):
        raise UpdateError(f"{app.name}: unexpected download URL for {asset.get('name')!r}")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise UpdateError(f"{app.name}: download URL must use HTTPS on github.com")
    if urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) != asset.get("name"):
        raise UpdateError(f"{app.name}: download URL does not match asset name")
    return url


def parse_checksum_file(data: bytes, asset_name: str, source: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UpdateError(f"{source}: checksum file is not valid UTF-8") from error

    matches = list(CHECKSUM_LINE.finditer(text))
    if len(matches) != 1:
        raise UpdateError(f"{source}: checksum file must contain exactly one SHA-256 line")
    listed_name = matches[0].group(2)
    if listed_name is not None and listed_name != asset_name:
        raise UpdateError(
            f"{source}: checksum is for {listed_name!r}, expected {asset_name!r}"
        )
    return matches[0].group(1).lower()


def resolve_checksum(app: App, release: Release, client: "GitHub") -> str:
    name = release.asset["name"]

    digest = release.asset.get("digest")
    if digest is not None:
        return _normalize_sha256(digest, f"{app.name}: release digest for {name}")

    checksum_asset = next(
        (
            candidate
            for candidate in release.assets
            if isinstance(candidate, dict) and candidate.get("name") == f"{name}.sha256"
        ),
        None,
    )
    if checksum_asset is not None:
        source = f"{app.name}: {checksum_asset['name']}"
        data = client.get_bytes(asset_url(app, checksum_asset))
        companion_digest = checksum_asset.get("digest")
        if companion_digest is not None:
            expected = _normalize_sha256(companion_digest, source)
            if hashlib.sha256(data).hexdigest() != expected:
                raise UpdateError(f"{source}: checksum file does not match its published digest")
        return parse_checksum_file(data, name, source)

    print(f"  fetching {name} to calculate its SHA-256", flush=True)
    return client.download_sha256(asset_url(app, release.asset), expected_size=release.asset["size"])


def update_app(app: App, cask_path: Path, client: "GitHub", *, dry_run: bool = False) -> Change | None:
    try:
        text = cask_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise UpdateError(f"{app.name}: cask not found: {cask_path}") from error

    current_version = cask_field(text, "version")
    assert current_version is not None
    current_tuple = _version_tuple(current_version)
    if current_tuple is None:
        raise UpdateError(f"{app.name}: cask version {current_version!r} is not semver")
    display_name = cask_field(text, "name", required=False) or app.name

    releases = fetch_releases(client, app.repo)
    release = select_release(app, releases)

    if release.version_tuple == current_tuple:
        print(f"{app.name}: already at {current_version}", flush=True)
        return None
    if release.version_tuple < current_tuple:
        print(
            f"{app.name}: cask version {current_version} is newer than release "
            f"{release.version}; skipping",
            flush=True,
        )
        return None

    checksum = resolve_checksum(app, release, client)
    new_text = set_cask_field(text, "version", release.version)
    new_text = set_cask_field(new_text, "sha256", checksum)
    if new_text == text:
        print(f"{app.name}: cask already matches release {release.version}", flush=True)
        return None

    if not dry_run:
        cask_path.write_text(new_text, encoding="utf-8")

    action = "would update" if dry_run else "updated"
    print(
        f"{app.name}: {action} {current_version} -> {release.version} "
        f"(sha256 {checksum[:12]}...)",
        flush=True,
    )
    return Change(
        name=app.name,
        display_name=display_name,
        cask=cask_path,
        old_version=current_version,
        new_version=release.version,
        checksum=checksum,
    )


def summary(changes: list[Change]) -> tuple[str, str]:
    if not changes:
        return "", "No cask updates."

    title = "chore(homebrew): update " + ", ".join(
        f"{change.display_name} to {change.new_version}" for change in changes
    )
    rows = "\n".join(
        f"| `{change.name}` | {change.old_version} | {change.new_version} |"
        for change in changes
    )
    body = (
        "## Cask updates\n\n"
        "| Cask | Previous | New |\n"
        "| --- | --- | --- |\n"
        f"{rows}\n\n"
        "SHA-256 checksums were read from GitHub release metadata, a published "
        "checksum file, or calculated from the downloaded release artifact.\n\n"
        "This pull request was generated automatically. Review the upstream "
        "release notes before merging.\n"
    )
    return title, body


def write_summary_json(path: Path, changes: list[Change]) -> None:
    title, body = summary(changes)
    payload = {
        "changed": [
            {
                "name": change.name,
                "previous": change.old_version,
                "version": change.new_version,
                "sha256": change.checksum,
            }
            for change in changes
        ],
        "title": title,
        "body": body,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class GitHub:
    def __init__(self, token: str | None = None, timeout: float = 120) -> None:
        self.token = token
        self.timeout = timeout

    def _headers(self, accept: str, url: str) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        # Never forward the token to github.com release download redirects.
        if self.token and urllib.parse.urlsplit(url).hostname == "api.github.com":
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _open(self, url: str, accept: str):
        request = urllib.request.Request(url, headers=self._headers(accept, url))
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None
            if error.code == 429 or (error.code == 403 and remaining == "0"):
                reset = error.headers.get("X-RateLimit-Reset") if error.headers else None
                detail = ""
                if reset and reset.isdigit():
                    when = datetime.fromtimestamp(int(reset), tz=timezone.utc)
                    detail = f"; resets at {when:%Y-%m-%d %H:%M:%S UTC}"
                raise RateLimitError(f"GitHub API rate limit exceeded{detail}") from error
            raise ApiError(f"GitHub request failed: HTTP {error.code} for {url}") from error
        except urllib.error.URLError as error:
            raise ApiError(f"GitHub request failed: {error.reason}") from error

    def get_bytes(self, url: str) -> bytes:
        with self._open(url, "*/*") as response:
            return response.read()

    def get_json(self, url: str) -> object:
        with self._open(url, "application/vnd.github+json") as response:
            data = response.read()
        try:
            return json.loads(data)
        except json.JSONDecodeError as error:
            raise ApiError(f"GitHub returned invalid JSON for {url}") from error

    def download_sha256(self, url: str, expected_size: int | None = None) -> str:
        digest = hashlib.sha256()
        total = 0
        with self._open(url, "*/*") as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
        if expected_size is not None and total != expected_size:
            raise UpdateError(
                f"downloaded {total} bytes from {url}, expected {expected_size}"
            )
        return digest.hexdigest()


def main(argv: list[str] | None = None, client: GitHub | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update Homebrew casks from GitHub Releases.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="repository root")
    parser.add_argument("--config", default="scripts/apps.json", help="path to the app config")
    parser.add_argument("--only", action="append", default=[], metavar="NAME", help="update only this cask")
    parser.add_argument("--dry-run", action="store_true", help="print changes without writing")
    parser.add_argument("--summary-json", type=Path, help="write a machine-readable PR summary")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path

    try:
        apps = load_apps(config_path)
        if args.only:
            known = {app.name for app in apps}
            unknown = sorted(set(args.only) - known)
            if unknown:
                raise UpdateError(f"unknown cask(s): {', '.join(unknown)}")
            apps = [app for app in apps if app.name in set(args.only)]

        client = client or GitHub(token=os.environ.get("GITHUB_TOKEN"))
        changes = [
            change
            for app in apps
            if (change := update_app(app, root / app.cask, client, dry_run=args.dry_run))
        ]

        if args.summary_json:
            write_summary_json(args.summary_json, changes)

        if changes:
            print(f"\n{len(changes)} cask(s) {'would be ' if args.dry_run else ''}updated.")
            title, body = summary(changes)
            print(f"PR title: {title}")
            print(body)
            return 0

        print("\nAll casks are up to date.")
        return 0

    except RateLimitError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except ApiError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except UpdateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
