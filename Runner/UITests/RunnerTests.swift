import XCTest

/// Entry point: a UI test that never finishes. It serves the runner's HTTP API
/// until the host calls POST /shutdown.
final class RunnerTests: XCTestCase {
    private var router: Router?

    func testRunServer() throws {
        continueAfterFailure = true
        let env = ProcessInfo.processInfo.environment
        let port = UInt16(env["GHOSTHAND_PORT"] ?? "") ?? 8100
        let token = env["GHOSTHAND_TOKEN"] ?? ""
        // The simulator shares the Mac's network, so refuse other machines there.
        // On an iPhone the host connects through the CoreDevice tunnel instead.
        #if targetEnvironment(simulator)
        let loopbackOnly = true
        #else
        let loopbackOnly = false
        #endif

        let router = Router()
        self.router = router
        let server = try HTTPServer(port: port, token: token, loopbackOnly: loopbackOnly, handler: router.handle)
        server.start()
        NSLog("GHOSTHAND_RUNNER_READY port=%d", Int(port))

        // Spinning the main run loop lets the server hop onto the main queue,
        // where XCUITest APIs must be called.
        while !router.shouldStop {
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.5))
        }
        server.stop()
    }

    // XCUITest reports failed interactions as test issues rather than thrown
    // errors. Capture them so the router can return them as HTTP errors, and
    // don't record them: a failed tap must not end the session.
    override func record(_ issue: XCTIssue) {
        if let router {
            // The full description dumps the whole element tree; the first line says what failed.
            let summary = issue.compactDescription.split(separator: "\n").first.map(String.init)
            router.recordIssue(summary ?? issue.compactDescription)
        } else {
            super.record(issue)
        }
    }
}
