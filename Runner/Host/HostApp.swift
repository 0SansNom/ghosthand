import SwiftUI

// Empty host app: XCUITest bundles need one, but all the work happens in the
// UI test process (see UITests/RunnerTests.swift).
@main
struct HostApp: App {
    var body: some Scene {
        WindowGroup {
            Text("Ghosthand Runner")
        }
    }
}
