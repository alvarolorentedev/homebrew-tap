from __future__ import annotations

import email.message
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from update_casks import (  # noqa: E402
    ApiError,
    App,
    GitHub,
    RateLimitError,
    UpdateError,
    asset_url,
    fetch_releases,
    main,
    select_release,
    update_app,
)

GOOD_SHA = "a" * 64
NEW_SHA = "b" * 64


def asset(name: str, version: str = "0.3.2", *, digest: str | None = None, repo: str = "alvarolorentedev/mountee", size: int = 1234):
    data = {
        "name": name,
        "state": "uploaded",
        "size": size,
        "browser_download_url": f"https://github.com/{repo}/releases/download/v{version}/{name}",
    }
    if digest is not None:
        data["digest"] = digest
    return data


def release(version: str, assets: list[dict], *, draft: bool = False, prerelease: bool = False, tag: str | None = None, published: str = "2026-01-01T00:00:00Z", repo: str = "alvarolorentedev/mountee"):
    return {
        "tag_name": tag if tag is not None else f"v{version}",
        "draft": draft,
        "prerelease": prerelease,
        "published_at": published,
        "assets": assets,
    }


CASK = """cask "{name}" do
  version "{version}"
  sha256 "{sha}"

  url "https://github.com/{repo}/releases/download/v#{{version}}/{asset}",
      verified: "github.com/{repo}/"
  name "{display}"
  desc "Test application"
  homepage "https://example.invalid"

  depends_on arch: :arm64
  depends_on macos: :tahoe

  app "Example.app"
end
"""


class FakeClient:
    def __init__(self, releases=None, *, checksum_files=None, downloads=None, error=None):
        self.releases = releases or {}
        self.checksum_files = checksum_files or {}
        self.downloads = downloads or {}
        self.error = error
        self.json_urls: list[str] = []
        self.checksum_urls: list[str] = []
        self.download_urls: list[tuple[str, int | None]] = []

    def get_json(self, url: str):
        if self.error:
            raise self.error
        self.json_urls.append(url)
        repo = url.split("/repos/", 1)[1].split("/releases", 1)[0]
        if repo not in self.releases:
            raise ApiError(f"no fake releases for {repo}")
        return self.releases[repo]

    def get_bytes(self, url: str) -> bytes:
        self.checksum_urls.append(url)
        if url not in self.checksum_files:
            raise AssertionError(f"unexpected checksum download: {url}")
        return self.checksum_files[url]

    def download_sha256(self, url: str, expected_size: int | None = None) -> str:
        self.download_urls.append((url, expected_size))
        if url not in self.downloads:
            raise AssertionError(f"unexpected artifact download: {url}")
        return self.downloads[url]


class TapTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "Casks").mkdir()
        (self.root / "scripts").mkdir()
        self.config = {
            "apps": [
                {
                    "name": "mountee",
                    "repo": "alvarolorentedev/mountee",
                    "cask": "Casks/mountee.rb",
                    "asset": "Mountee.dmg",
                }
            ]
        }
        self.write_config()

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self):
        (self.root / "scripts" / "apps.json").write_text(json.dumps(self.config), encoding="utf-8")

    def write_cask(self, name="mountee", version="0.3.1", sha=GOOD_SHA, *, display="Mountee", repo="alvarolorentedev/mountee", asset="Mountee.dmg"):
        path = self.root / "Casks" / f"{name}.rb"
        text = CASK.format(
            name=name,
            version=version,
            sha=sha,
            repo=repo,
            asset=asset,
            display=display,
        )
        path.write_text(text, encoding="utf-8")
        return path

    def app(self, **overrides):
        data = {
            "name": "mountee",
            "repo": "alvarolorentedev/mountee",
            "cask": "Casks/mountee.rb",
            "asset": "Mountee.dmg",
        }
        data.update(overrides)
        return App(**data)


class UpdateTests(TapTestCase):
    def test_no_new_release_makes_no_change(self):
        cask = self.write_cask(version="0.3.2")
        before = cask.read_text()
        client = FakeClient(
            {"alvarolorentedev/mountee": [release("0.3.2", [asset("Mountee.dmg", digest=f"sha256:{NEW_SHA}")])]}
        )

        self.assertIsNone(update_app(self.app(), cask, client))

        self.assertEqual(cask.read_text(), before)
        self.assertEqual(client.download_urls, [])
        self.assertEqual(client.checksum_urls, [])

    def test_single_app_update_uses_published_digest(self):
        cask = self.write_cask(version="0.3.1")
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.3.2", [asset("Mountee.dmg", digest=f"sha256:{NEW_SHA}")])
                ]
            }
        )

        change = update_app(self.app(), cask, client)

        self.assertIsNotNone(change)
        self.assertEqual(change.old_version, "0.3.1")
        self.assertEqual(change.new_version, "0.3.2")
        text = cask.read_text()
        self.assertIn('version "0.3.2"', text)
        self.assertIn(f'sha256 "{NEW_SHA}"', text)
        self.assertIn("desc \"Test application\"", text)
        self.assertEqual(client.download_urls, [])

    def test_multiple_app_updates_from_main(self):
        self.config["apps"].append(
            {
                "name": "barklarm",
                "repo": "alvarolorentedev/barklarm-app",
                "cask": "Casks/barklarm.rb",
                "asset": "barklarm-{version}-arm64.dmg",
            }
        )
        self.write_config()
        mountee_cask = self.write_cask("mountee", version="0.3.1", display="Mountee")
        barklarm_cask = self.write_cask(
            "barklarm",
            version="2.4.7",
            display="Barklarm",
            repo="alvarolorentedev/barklarm-app",
            asset="barklarm-2.4.7-arm64.dmg",
        )
        summary_path = self.root / "summary.json"
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.3.2", [asset("Mountee.dmg", digest=f"sha256:{NEW_SHA}")])
                ],
                "alvarolorentedev/barklarm-app": [
                    release(
                        "2.4.8",
                        [asset("barklarm-2.4.8-arm64.dmg", version="2.4.8", digest=f"sha256:{'c' * 64}", repo="alvarolorentedev/barklarm-app")],
                        repo="alvarolorentedev/barklarm-app",
                    )
                ],
            }
        )

        with redirect_stdout(io.StringIO()):
            result = main(
                ["--root", str(self.root), "--summary-json", str(summary_path)],
                client=client,
            )

        self.assertEqual(result, 0)
        self.assertIn('version "0.3.2"', mountee_cask.read_text())
        self.assertIn('version "2.4.8"', barklarm_cask.read_text())
        payload = json.loads(summary_path.read_text())
        self.assertEqual([item["name"] for item in payload["changed"]], ["mountee", "barklarm"])
        self.assertEqual(
            payload["title"],
            "chore(homebrew): update Mountee to 0.3.2, Barklarm to 2.4.8",
        )

    def test_draft_release_is_ignored(self):
        cask = self.write_cask(version="0.3.1")
        digest = f"sha256:{NEW_SHA}"
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.4.0", [asset("Mountee.dmg", "0.4.0", digest=digest)], draft=True),
                    release("0.3.2", [asset("Mountee.dmg", digest=digest)]),
                ]
            }
        )

        change = update_app(self.app(), cask, client)

        self.assertEqual(change.new_version, "0.3.2")

    def test_prerelease_is_ignored(self):
        cask = self.write_cask(version="0.3.1")
        digest = f"sha256:{NEW_SHA}"
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.4.0", [asset("Mountee.dmg", "0.4.0", digest=digest)], prerelease=True),
                    release("0.3.2", [asset("Mountee.dmg", digest=digest)]),
                ]
            }
        )

        change = update_app(self.app(), cask, client)

        self.assertEqual(change.new_version, "0.3.2")

    def test_non_version_tags_are_ignored(self):
        cask = self.write_cask(version="0.3.1")
        digest = f"sha256:{NEW_SHA}"
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("9.9.9", [asset("Mountee.dmg", "9.9.9", digest=digest)], tag="updates"),
                    release("9.9.9", [asset("Mountee.dmg", "9.9.9", digest=digest)], tag="latest"),
                    release("0.3.2", [asset("Mountee.dmg", digest=digest)]),
                ]
            }
        )

        change = update_app(self.app(), cask, client)

        self.assertEqual(change.new_version, "0.3.2")

    def test_missing_macos_artifact_is_an_error(self):
        cask = self.write_cask(version="0.3.1")
        before = cask.read_text()
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.4.0", [asset("appcast.xml", "0.4.0", digest=f"sha256:{NEW_SHA}")])
                ]
            }
        )

        with self.assertRaises(UpdateError):
            update_app(self.app(), cask, client)

        self.assertEqual(cask.read_text(), before)

    def test_asset_filename_change_is_not_silently_accepted(self):
        cask = self.write_cask(version="0.3.1")
        before = cask.read_text()
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.4.0", [asset("Mountee-0.4.0.dmg", "0.4.0", digest=f"sha256:{NEW_SHA}")])
                ]
            }
        )

        with self.assertRaises(UpdateError) as caught:
            update_app(self.app(), cask, client)

        self.assertIn("missing expected asset", str(caught.exception))
        self.assertEqual(cask.read_text(), before)

    def test_missing_checksum_metadata_downloads_artifact(self):
        cask = self.write_cask(version="0.3.1")
        artifact = asset("Mountee.dmg")
        client = FakeClient(
            {"alvarolorentedev/mountee": [release("0.3.2", [artifact])]},
            downloads={artifact["browser_download_url"]: NEW_SHA},
        )

        change = update_app(self.app(), cask, client)

        self.assertEqual(change.checksum, NEW_SHA)
        self.assertEqual(client.download_urls, [(artifact["browser_download_url"], 1234)])
        self.assertIn(f'sha256 "{NEW_SHA}"', cask.read_text())

    def test_published_checksum_file_is_used_without_downloading_artifact(self):
        cask = self.write_cask(version="0.3.1")
        artifact = asset("Mountee.dmg")
        checksum_file = asset("Mountee.dmg.sha256")
        client = FakeClient(
            {"alvarolorentedev/mountee": [release("0.3.2", [artifact, checksum_file])]},
            checksum_files={checksum_file["browser_download_url"]: f"{NEW_SHA}  Mountee.dmg\n".encode()},
        )

        change = update_app(self.app(), cask, client)

        self.assertEqual(change.checksum, NEW_SHA)
        self.assertEqual(client.download_urls, [])

    def test_checksum_file_with_wrong_filename_is_rejected(self):
        cask = self.write_cask(version="0.3.1")
        artifact = asset("Mountee.dmg")
        checksum_file = asset("Mountee.dmg.sha256")
        client = FakeClient(
            {"alvarolorentedev/mountee": [release("0.3.2", [artifact, checksum_file])]},
            checksum_files={checksum_file["browser_download_url"]: f"{NEW_SHA}  other.dmg\n".encode()},
        )

        with self.assertRaises(UpdateError):
            update_app(self.app(), cask, client)

    def test_invalid_published_digest_is_rejected(self):
        cask = self.write_cask(version="0.3.1")
        before = cask.read_text()
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.3.2", [asset("Mountee.dmg", digest="sha256:not-a-checksum")])
                ]
            }
        )

        with self.assertRaises(UpdateError):
            update_app(self.app(), cask, client)

        self.assertEqual(cask.read_text(), before)

    def test_invalid_checksum_file_is_rejected(self):
        cask = self.write_cask(version="0.3.1")
        before = cask.read_text()
        artifact = asset("Mountee.dmg")
        checksum_file = asset("Mountee.dmg.sha256")
        client = FakeClient(
            {"alvarolorentedev/mountee": [release("0.3.2", [artifact, checksum_file])]},
            checksum_files={checksum_file["browser_download_url"]: b"not a checksum\n"},
        )

        with self.assertRaises(UpdateError):
            update_app(self.app(), cask, client)

        self.assertEqual(cask.read_text(), before)

    def test_checksum_file_is_verified_against_its_own_digest(self):
        cask = self.write_cask(version="0.3.1")
        artifact = asset("Mountee.dmg")
        content = f"{NEW_SHA}  Mountee.dmg\n".encode()
        checksum_file = asset("Mountee.dmg.sha256", digest=f"sha256:{'d' * 64}")
        client = FakeClient(
            {"alvarolorentedev/mountee": [release("0.3.2", [artifact, checksum_file])]},
            checksum_files={checksum_file["browser_download_url"]: content},
        )

        with self.assertRaises(UpdateError):
            update_app(self.app(), cask, client)

    def test_newest_semver_wins_regardless_of_api_order(self):
        cask = self.write_cask(version="0.3.0")
        digest = f"sha256:{NEW_SHA}"
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.3.0", [asset("Mountee.dmg", "0.3.0", digest=digest)]),
                    release("0.3.1", [asset("Mountee.dmg", "0.3.1", digest=digest)]),
                    release("0.3.2", [asset("Mountee.dmg", digest=digest)]),
                ]
            }
        )

        change = update_app(self.app(), cask, client)

        self.assertEqual(change.new_version, "0.3.2")

    def test_dry_run_does_not_write(self):
        cask = self.write_cask(version="0.3.1")
        before = cask.read_text()
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.3.2", [asset("Mountee.dmg", digest=f"sha256:{NEW_SHA}")])
                ]
            }
        )

        change = update_app(self.app(), cask, client, dry_run=True)

        self.assertIsNotNone(change)
        self.assertEqual(cask.read_text(), before)

    def test_cask_newer_than_releases_is_not_downgraded(self):
        cask = self.write_cask(version="0.9.0")
        before = cask.read_text()
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.3.2", [asset("Mountee.dmg", digest=f"sha256:{NEW_SHA}")])
                ]
            }
        )

        self.assertIsNone(update_app(self.app(), cask, client))

        self.assertEqual(cask.read_text(), before)


class IdempotencyAndErrorTests(TapTestCase):
    def test_idempotent_second_run_makes_no_changes(self):
        cask = self.write_cask(version="0.3.1")
        client = FakeClient(
            {
                "alvarolorentedev/mountee": [
                    release("0.3.2", [asset("Mountee.dmg", digest=f"sha256:{NEW_SHA}")])
                ]
            }
        )

        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root)], client=client), 0)
        after_first = cask.read_text()

        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root)], client=client), 0)

        self.assertEqual(cask.read_text(), after_first)
        self.assertEqual(client.download_urls, [])
        self.assertEqual(len(client.json_urls), 2)

    def test_api_error_exits_two(self):
        self.write_cask()
        client = FakeClient(error=ApiError("GitHub request failed: HTTP 500"))

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(["--root", str(self.root)], client=client)

        self.assertEqual(result, 2)
        self.assertIn("HTTP 500", stderr.getvalue())

    def test_rate_limit_exits_two(self):
        self.write_cask()
        client = FakeClient(error=RateLimitError("GitHub API rate limit exceeded"))

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(["--root", str(self.root)], client=client)

        self.assertEqual(result, 2)
        self.assertIn("rate limit", stderr.getvalue())

    def test_unknown_only_name_exits_one(self):
        self.write_cask()
        with redirect_stderr(io.StringIO()):
            result = main(["--root", str(self.root), "--only", "nope"], client=FakeClient())

        self.assertEqual(result, 1)


class SelectionTests(unittest.TestCase):
    def test_select_release_returns_expected_asset(self):
        app = App("mountee", "alvarolorentedev/mountee", "Casks/mountee.rb", "Mountee.dmg")
        selected = select_release(
            app,
            [release("0.3.2", [asset("Mountee.dmg", digest=f"sha256:{NEW_SHA}")])],
        )

        self.assertEqual(selected.version, "0.3.2")
        self.assertEqual(selected.asset["name"], "Mountee.dmg")

    def test_select_release_rejects_release_with_bad_version(self):
        app = App("mountee", "alvarolorentedev/mountee", "Casks/mountee.rb", "Mountee.dmg")
        with self.assertRaises(UpdateError):
            select_release(app, [release("0.3.2", [], tag="nightly")])

    def test_fetch_releases_paginates(self):
        class PagedClient:
            def __init__(self, pages):
                self.pages = pages

            def get_json(self, url):
                page = int(url.rsplit("page=", 1)[1])
                return self.pages.get(page, [])

        pages = {
            1: [{"tag_name": f"v0.0.{index}"} for index in range(100)],
            2: [{"tag_name": "v1.0.0"}],
        }

        releases = fetch_releases(PagedClient(pages), "owner/repo")

        self.assertEqual(len(releases), 101)
        self.assertEqual(releases[-1]["tag_name"], "v1.0.0")

    def test_asset_url_must_match_asset_name(self):
        app = App("mountee", "alvarolorentedev/mountee", "Casks/mountee.rb", "Mountee.dmg")
        with self.assertRaises(UpdateError):
            asset_url(
                app,
                {
                    "name": "Mountee.dmg",
                    "browser_download_url": "https://github.com/alvarolorentedev/mountee/releases/download/v1/Other.dmg",
                },
            )


class HttpTests(unittest.TestCase):
    def test_token_is_only_sent_to_api_host(self):
        client = GitHub(token="secret")
        self.assertIn("Authorization", client._headers("*/*", "https://api.github.com/repos/x/y"))
        self.assertNotIn(
            "Authorization",
            client._headers("*/*", "https://github.com/x/y/releases/download/v1/file.dmg"),
        )

    def test_http_403_rate_limit_is_detected(self):
        headers = email.message.Message()
        headers["X-RateLimit-Remaining"] = "0"
        headers["X-RateLimit-Reset"] = "1700000000"
        error = HTTPError("https://api.github.com/x", 403, "Forbidden", headers, None)

        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RateLimitError):
                GitHub().get_json("https://api.github.com/x")

    def test_http_error_is_reported_as_api_error(self):
        headers = email.message.Message()
        error = HTTPError("https://api.github.com/x", 500, "Server Error", headers, None)

        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ApiError):
                GitHub().get_json("https://api.github.com/x")

    def test_download_size_mismatch_is_an_error(self):
        client = GitHub()
        response = io.BytesIO(b"abc")
        response.__enter__ = lambda self=response: self
        response.__exit__ = lambda *args: False

        with patch.object(client, "_open", return_value=response):
            with self.assertRaises(UpdateError):
                client.download_sha256("https://github.com/x/y/file.dmg", expected_size=99)


if __name__ == "__main__":
    unittest.main()
