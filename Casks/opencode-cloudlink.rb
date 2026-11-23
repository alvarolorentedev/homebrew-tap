cask "opencode-cloudlink" do
  version "0.1.35"
  sha256 "124cf6fc20681760f12d5bd186e28a9f606e94853e51810d2d24020cf179c0b8"

  url "https://github.com/alvarolorentedev/opencode-cloud-link-releases/releases/download/v#{version}/opencode-cloudlink_darwin_universal.dmg"
  name "OpenCode CloudLink"
  desc "Desktop application for OpenCode cloud connectivity"
  homepage "https://opencodecloud.link/"

  livecheck do
    url :url
    strategy :github_latest
    regex(/^v?(\d+(?:\.\d+)+)$/i)
  end

  auto_updates true
  depends_on macos: :sequoia

  app "OpenCode CloudLink.app"

  uninstall quit: "link.opencodecloud.desktop"
end
