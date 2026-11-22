// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Rnnr",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "RnnrApp", targets: ["RnnrApp"])],
    targets: [
        .target(name: "RnnrCore"),
        .executableTarget(name: "RnnrApp", dependencies: ["RnnrCore"]),
        .executableTarget(
            name: "RnnrCoreChecks",
            dependencies: ["RnnrCore"],
            path: "Tests/RnnrCoreTests"
        ),
    ]
)
