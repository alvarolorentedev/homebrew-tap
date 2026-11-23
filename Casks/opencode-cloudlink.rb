cask "opencode-cloudlink" do
  version "0.1.35"
  sha256 "fee00ac27a2410c0b588f9c1a552a74ad6c4c52d5edd73a7d8cb1d2a405ec48b"

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
