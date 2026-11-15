cask "barklarm" do
  version "2.4.8"
  sha256 "7c33c90420d5fbb705fb336edf736eb9a9a9957feb02ee648d9e26f9ec24a82a"

  url "https://github.com/alvarolorentedev/barklarm-app/releases/download/v#{version}/barklarm-#{version}-arm64.dmg"
  name "Barklarm"
  desc "Build and observability monitoring application"
  homepage "https://www.barklarm.com/"

  livecheck do
    url :url
    strategy :github_latest
    regex(/^v?(\d+(?:\.\d+)+)$/i)
  end

  depends_on arch: :arm64
  depends_on :macos

  app "barklarm.app"

  uninstall quit: "com.electron.barklarm"
end
