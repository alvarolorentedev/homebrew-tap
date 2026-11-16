cask "barklarm" do
  version "3.0.0"
  sha256 "4f839f665fc273854ae802c3c7a99147df703166e7634dc441ae9a997872acf2"

  url "https://github.com/alvarolorentedev/barklarm-app/releases/download/v#{version}/Barklarm_#{version}_aarch64.dmg"
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

  app "Barklarm.app"

  uninstall quit: "dev.barklarm.app"
end
