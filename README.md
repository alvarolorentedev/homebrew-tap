# Homebrew Tap

Homebrew casks for macOS applications by [Álvaro Lorente](https://github.com/alvarolorentedev).
Each cask downloads the official GitHub Releases artifact, verifies its SHA-256
checksum, and installs the application bundle with Homebrew.

## Install

```bash
brew tap alvarolorentedev/tap

brew install --cask alvarolorentedev/tap/mountee
brew install --cask alvarolorentedev/tap/opencode-cloudlink
brew install --cask alvarolorentedev/tap/barklarm
```

### Homebrew 7 or later

If Homebrew refuses the tap as untrusted, trust it once before installing:

```bash
brew trust --tap alvarolorentedev/tap
```

## Applications

| Cask | Application | Requirements | Upstream |
| --- | --- | --- | --- |
| `mountee` | Mountee | macOS 26+, Apple Silicon | [Project](https://github.com/alvarolorentedev/mountee) · [Website](https://mountee.app/) |
| `opencode-cloudlink` | OpenCode CloudLink | macOS 15+, Intel or Apple Silicon | [Releases](https://github.com/alvarolorentedev/opencode-cloud-link-releases) · [Website](https://opencodecloud.link/) |
| `barklarm` | Barklarm | macOS 11+, Apple Silicon | [Project](https://github.com/alvarolorentedev/barklarm-app) · [Website](https://www.barklarm.com/) |

### Mountee

Mountee uses a macOS file system extension. After installing, launch Mountee
and enable the extension in **System Settings → General → Login Items &
Extensions → File System Extensions**. Homebrew cannot enable it for you.

Mountee mounts NTFS volumes read-only by default. Choosing read/write is always
a manual action, and volumes left dirty by Windows are kept read-only. If you
previously installed the old `MacNTFS.app`, quit and remove it manually before
using Mountee.

### OpenCode CloudLink

The DMG is universal and contains `OpenCode CloudLink.app`. The application has
its own signed updater, so the cask declares `auto_updates true`.

### Barklarm

Barklarm is published as an Apple Silicon DMG and installs `Barklarm.app`.

## Upgrade

```bash
brew update
brew upgrade --cask
```

To upgrade one application:

```bash
brew upgrade --cask mountee
```

When the application has its own updater (OpenCode CloudLink), Homebrew may
leave it to update itself. Use `brew reinstall --cask <cask>` to return to the
version declared in this tap.

## Uninstall

```bash
brew uninstall --cask mountee
brew uninstall --cask opencode-cloudlink
brew uninstall --cask barklarm
```

Remove the tap itself with:

```bash
brew untap alvarolorentedev/tap
```

Uninstalling an application through Homebrew does not remove user-created data,
mounted volumes, or files the application wrote elsewhere. The casks do not use
destructive `zap` or `uninstall` operations.

## How updates work

The casks are updated automatically from GitHub Releases:

1. [`scripts/update_casks.py`](scripts/update_casks.py) queries each upstream
   repository's releases.
2. Drafts, prereleases, and non-version tags are ignored. The highest stable
   semantic version is selected rather than assuming the most recent API entry
   or the `latest` release is correct.
3. The exact expected macOS artifact must exist. If a release renames or drops
   it, the updater fails instead of falling back to a different asset.
4. The checksum comes from GitHub's release asset digest when available, then
   from a published checksum file, and only otherwise from a local SHA-256 of
   the downloaded artifact. `sha256 :no_check` is never used.
5. Only the cask's `version` and `sha256` lines are rewritten. The updater is
   idempotent: when nothing new has been released, it makes no changes and does
   not download artifacts.

The [update workflow](.github/workflows/update-casks.yml) runs every six hours
and on demand. It runs the updater tests, validates the casks with
`brew audit --cask --strict --online` and `brew style`, then opens or updates a
pull request. It never merges automatically. Because pull requests created with
`GITHUB_TOKEN` do not trigger ordinary `pull_request` workflows, validation runs
inside the update workflow before the pull request is proposed.

For maintainers, the repository must allow GitHub Actions to create pull
requests under **Settings → Actions → General → Workflow permissions**.

## Validate locally

```bash
python3 -m unittest discover -s scripts/tests -v

for cask in Casks/*.rb; do ruby -c "$cask"; done
```

Homebrew disables auditing by file path, so register the repository as a tap
before auditing namespaced casks:

```bash
# If the tap is already installed, run brew untap alvarolorentedev/tap first.
tap_dir="$(brew --repository)/Library/Taps/alvarolorentedev/homebrew-tap"
ln -s "$PWD" "$tap_dir"
brew trust --tap alvarolorentedev/tap

brew audit --cask --strict --online \
  alvarolorentedev/tap/mountee \
  alvarolorentedev/tap/opencode-cloudlink \
  alvarolorentedev/tap/barklarm
brew style Casks/*.rb
```

## Troubleshooting

- **`brew install --cask` says the cask is unavailable** — run
  `brew tap alvarolorentedev/tap` first, or `brew update` if the tap is already
  present.
- **Homebrew refuses the tap as untrusted** — on Homebrew 7 and later, run
  `brew trust --tap alvarolorentedev/tap`. Older Homebrew versions do not
  require this step.
- **Homebrew reports an outdated version** — run `brew update` and then
  `brew upgrade --cask <cask>`.
- **Mountee cannot mount a drive** — make sure the file system extension is
  enabled and the app has been launched once. Check Disk Utility for a dirty
  NTFS volume; Mountee keeps such volumes read-only by design.
- **macOS blocks an application on first launch** — allow it in
  **System Settings → Privacy & Security**. Do not disable Gatekeeper.
- **An install is broken after an upstream update** — report it in the upstream
  project's issue tracker. This tap only packages official release artifacts.

Homebrew manages installation, upgrades, and removal of the application bundle.
It does not replace an application's own permissions, login items, or file
system extension activation requirements.

## Adding an application

Open an issue or pull request with:

1. The upstream GitHub repository and release artifact (exact filename).
2. The `.app` bundle name inside the DMG.
3. Supported CPU architectures and minimum macOS version.
4. The application homepage and a one-line description.

An accepted application needs a stable GitHub Release with a versioned `.dmg`
and a verifiable checksum. Add a cask under `Casks/`, an entry in
[`scripts/apps.json`](scripts/apps.json), and run the updater tests.

## Contributing

Pull requests are welcome. Keep changes small and consistent with the existing
casks. Before opening a pull request:

```bash
python3 -m unittest discover -s scripts/tests -v
brew audit --cask --strict --online <namespaced-cask>
brew style Casks/*.rb
```

The updater must remain deterministic, must never fabricate checksums, and must
fail safely when an upstream release does not match the expected artifact.
