// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ComputerUseDriver",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "computer-use-driver", targets: ["ComputerUseDriver"])],
    targets: [.executableTarget(name: "ComputerUseDriver", path: "Sources")]
)
