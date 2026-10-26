import UIKit
import XCTest

enum RunnerError: Error, CustomStringConvertible {
    case badRequest(String)
    case actionFailed(String)

    var description: String {
        switch self {
        case .badRequest(let message), .actionFailed(let message): return message
        }
    }
}

/// Always called on the main thread.
final class Router {
    static let springboardID = "com.apple.springboard"
    static let spotlightID = "com.apple.Spotlight"

    private(set) var shouldStop = false
    /// The app the host last launched or activated; the tree and gestures target it.
    private var currentBundleID: String?
    private var pendingIssues: [String] = []
    /// Installed apps, sent by the host, used to find the foreground app.
    private var knownApps: [String] = []

    func recordIssue(_ message: String) {
        pendingIssues.append(message)
    }

    func handle(_ request: HTTPRequest) -> HTTPResponse {
        pendingIssues.removeAll()
        do {
            let response = try route(request)
            if !pendingIssues.isEmpty {
                return .error(pendingIssues.joined(separator: "\n"), status: 500)
            }
            return response
        } catch let error as RunnerError {
            if case .badRequest = error { return .error(error.description, status: 400) }
            return .error(error.description, status: 500)
        } catch {
            return .error("\(error)", status: 500)
        }
    }

    private func route(_ request: HTTPRequest) throws -> HTTPResponse {
        switch (request.method, request.path) {
        case ("GET", "/health"):
            return .json(["ok": true, "currentApp": activeAppID(), "locked": isLocked()])

        case ("GET", "/screen"):
            let screen = UIScreen.main
            return .json([
                "width": screen.bounds.width,
                "height": screen.bounds.height,
                "scale": screen.scale,
                "orientation": orientationName(XCUIDevice.shared.orientation),
            ])

        case ("GET", "/screenshot"):
            let shot = XCUIScreen.main.screenshot()
            guard request.query["format"] == "jpeg" else {
                return HTTPResponse(status: 200, contentType: "image/png", body: shot.pngRepresentation)
            }
            // One pixel per point, compressed: about 20x less to send than the full PNG,
            // which matters over Wi-Fi.
            return HTTPResponse(status: 200, contentType: "image/jpeg", body: pointSizedJPEG(shot.image))

        case ("GET", "/tree"):
            let appID = request.query["bundleId"] ?? activeAppID()
            let app = XCUIApplication(bundleIdentifier: appID)
            let maxDepth = Int(request.query["maxDepth"] ?? "") ?? 60
            var tree = serialize(try app.snapshot(), depth: 0, maxDepth: maxDepth)
            tree["bundleId"] = appID
            tree["locked"] = isLocked()
            // System alerts (permissions, etc.) belong to SpringBoard but sit on top of the app.
            let springboard = XCUIApplication(bundleIdentifier: Self.springboardID)
            if appID != Self.springboardID, springboard.alerts.count > 0 {
                let alerts = try springboard.alerts.allElementsBoundByIndex.map {
                    serialize(try $0.snapshot(), depth: 0, maxDepth: maxDepth)
                }
                tree["children"] = alerts + (tree["children"] as? [[String: Any]] ?? [])
            }
            return .json(tree)

        case ("POST", "/tap"):
            let body = try request.json()
            let point = coordinate(x: try number(body, "x"), y: try number(body, "y"))
            if let duration = body["duration"] as? Double, duration > 0 {
                point.press(forDuration: duration)
            } else {
                point.tap()
            }
            return .json(["ok": true])

        case ("POST", "/swipe"):
            let body = try request.json()
            let fromX = try number(body, "fromX"), fromY = try number(body, "fromY")
            let toX = try number(body, "toX"), toY = try number(body, "toY")
            let duration = max(body["duration"] as? Double ?? 0.3, 0.05)
            let distance = hypot(toX - fromX, toY - fromY)
            coordinate(x: fromX, y: fromY).press(
                forDuration: 0.05,
                thenDragTo: coordinate(x: toX, y: toY),
                withVelocity: XCUIGestureVelocity(distance / duration),
                thenHoldForDuration: 0
            )
            return .json(["ok": true])

        case ("POST", "/type"):
            let body = try request.json()
            guard let text = body["text"] as? String else { throw RunnerError.badRequest("missing 'text'") }
            guard let target = focusedApp() else {
                throw RunnerError.actionFailed("no text field has keyboard focus; tap one first")
            }
            target.typeText(text)
            return .json(["ok": true])

        case ("POST", "/press"):
            let body = try request.json()
            switch body["button"] as? String {
            case "home":
                XCUIDevice.shared.press(.home)
                currentBundleID = nil
            #if !targetEnvironment(simulator)
            case "volumeUp": XCUIDevice.shared.press(.volumeUp)
            case "volumeDown": XCUIDevice.shared.press(.volumeDown)
            #endif
            default:
                throw RunnerError.badRequest("unknown button; expected home, volumeUp or volumeDown (device only)")
            }
            return .json(["ok": true])

        case ("POST", "/launch"):
            let body = try request.json()
            let bundleID = try string(body, "bundleId")
            let app = XCUIApplication(bundleIdentifier: bundleID)
            if body["relaunch"] as? Bool == true {
                app.launch()
            } else {
                app.activate()
            }
            currentBundleID = bundleID
            return .json(["ok": true, "state": stateName(app.state)])

        case ("POST", "/terminate"):
            let body = try request.json()
            let bundleID = try string(body, "bundleId")
            XCUIApplication(bundleIdentifier: bundleID).terminate()
            if currentBundleID == bundleID { currentBundleID = nil }
            return .json(["ok": true])

        case ("POST", "/app-state"):
            // XCUITest has no public "which app is in front" call, so the host
            // passes candidates (e.g. installed apps) and we report each state.
            let body = try request.json()
            guard let ids = body["bundleIds"] as? [String] else { throw RunnerError.badRequest("missing 'bundleIds'") }
            var states: [String: String] = [:]
            for id in ids { states[id] = stateName(XCUIApplication(bundleIdentifier: id).state) }
            return .json(["states": states])

        case ("POST", "/open-url"):
            let body = try request.json()
            guard let url = URL(string: try string(body, "url")) else { throw RunnerError.badRequest("invalid 'url'") }
            XCUIDevice.shared.system.open(url)
            return .json(["ok": true])

        case ("POST", "/current-app"):
            // Lets the host point the runner at an app it opened another way (simctl, URL…).
            let body = try request.json()
            currentBundleID = body["bundleId"] as? String
            return .json(["ok": true])

        case ("POST", "/known-apps"):
            let body = try request.json()
            guard let ids = body["bundleIds"] as? [String] else { throw RunnerError.badRequest("missing 'bundleIds'") }
            let system: Set<String> = [Self.springboardID, Self.spotlightID]
            knownApps = ids.filter { !system.contains($0) }
            return .json(["ok": true, "count": knownApps.count])

        case ("POST", "/shutdown"):
            shouldStop = true
            return .json(["ok": true])

        default:
            return .error("no route for \(request.method) \(request.path)", status: 404)
        }
    }

    /// Which app is in front. XCUITest has no public call for this, so we check
    /// Spotlight (an overlay), then the app we last targeted, then every known app.
    private func activeAppID() -> String {
        if isForeground(Self.spotlightID) { return Self.spotlightID }
        if let id = currentBundleID, isForeground(id) { return id }
        // Apps opened by tapping an icon, a link or a notification.
        if let id = knownApps.first(where: { $0 != currentBundleID && isForeground($0) }) {
            currentBundleID = id
            return id
        }
        currentBundleID = nil
        return Self.springboardID
    }

    /// The lock screen shows a padlock with this identifier, whatever the language.
    private func isLocked() -> Bool {
        #if targetEnvironment(simulator)
        return false
        #else
        let springboard = XCUIApplication(bundleIdentifier: Self.springboardID)
        return springboard.descendants(matching: .any)["proud-lock"].exists
        #endif
    }

    private func isForeground(_ bundleID: String) -> Bool {
        XCUIApplication(bundleIdentifier: bundleID).state == .runningForeground
    }

    private func activeApp() -> XCUIApplication {
        XCUIApplication(bundleIdentifier: activeAppID())
    }

    /// The app owning keyboard focus. System UI such as Spotlight runs in its own
    /// process, so the current app isn't always the one being typed into.
    private func focusedApp() -> XCUIApplication? {
        let candidates = [activeApp(), XCUIApplication(bundleIdentifier: Self.spotlightID),
                          XCUIApplication(bundleIdentifier: Self.springboardID)]
        let focused = NSPredicate(format: "hasKeyboardFocus == true")
        return candidates.first { app in
            app.state == .runningForeground && app.descendants(matching: .any).matching(focused).firstMatch.exists
        }
    }

    /// Screen coordinates in points, from the top-left corner.
    private func coordinate(x: Double, y: Double) -> XCUICoordinate {
        activeApp()
            .coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
            .withOffset(CGVector(dx: x, dy: y))
    }

    private func pointSizedJPEG(_ image: UIImage) -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let size = UIScreen.main.bounds.size
        let resized = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            image.draw(in: CGRect(origin: .zero, size: size))
        }
        return resized.jpegData(compressionQuality: 0.8) ?? Data()
    }

    private func number(_ body: [String: Any], _ key: String) throws -> Double {
        guard let value = body[key] as? Double else { throw RunnerError.badRequest("missing number '\(key)'") }
        return value
    }

    private func string(_ body: [String: Any], _ key: String) throws -> String {
        guard let value = body[key] as? String, !value.isEmpty else { throw RunnerError.badRequest("missing '\(key)'") }
        return value
    }
}
