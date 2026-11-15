cask "mountee" do
  version "0.3.2"
  sha256 "bd5f7c5e234d38fa625aa2f0c948d75bcdf881f399ccd7a08703d40d7a5305ba"

  url "https://github.com/alvarolorentedev/mountee/releases/download/v#{version}/Mountee.dmg"
  name "Mountee"
  desc "NTFS read/write drive mounting utility"
  homepage "https://mountee.app/"

  livecheck do
    url :url
    strategy :github_latest
    regex(/^v?(\d+(?:\.\d+)+)$/i)
  end

  depends_on arch: :arm64
  depends_on macos: :tahoe

  app "Mountee.app"

  uninstall quit: "app.mountee"

  caveats <<~EOS
    Mountee requires macOS 26 or later on Apple Silicon.

    After installing, launch Mountee and enable its file system extension in
    System Settings -> General -> Login Items & Extensions -> File System
    Extensions. Homebrew cannot enable the extension for you.

    If you previously installed MacNTFS.app, quit and remove it before using
    Mountee. Mountee mounts NTFS volumes read-only by default; choosing
    read/write is always a manual action.
  EOS
end
