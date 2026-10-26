import Foundation
import Network

struct HTTPRequest {
    let method: String
    let path: String
    let query: [String: String]
    let headers: [String: String]
    let body: Data

    func json() throws -> [String: Any] {
        if body.isEmpty { return [:] }
        guard let object = try JSONSerialization.jsonObject(with: body) as? [String: Any] else {
            throw RunnerError.badRequest("body must be a JSON object")
        }
        return object
    }

    /// Returns a complete request, or nil if `buffer` doesn't hold one yet.
    static func parse(_ buffer: Data) -> HTTPRequest? {
        let separator = Data("\r\n\r\n".utf8)
        guard let headerEnd = buffer.range(of: separator),
              let head = String(data: buffer[..<headerEnd.lowerBound], encoding: .utf8)
        else { return nil }

        let lines = head.components(separatedBy: "\r\n")
        let parts = lines[0].split(separator: " ")
        guard parts.count >= 2 else { return nil }

        var contentLength = 0
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            let pair = line.split(separator: ":", maxSplits: 1)
            guard pair.count == 2 else { continue }
            let name = pair[0].lowercased()
            let value = pair[1].trimmingCharacters(in: .whitespaces)
            headers[name] = value
            if name == "content-length" {
                contentLength = Int(value) ?? 0
            }
        }
        let bodyStart = headerEnd.upperBound
        guard buffer.count - bodyStart >= contentLength else { return nil }

        let components = URLComponents(string: String(parts[1]))
        var query: [String: String] = [:]
        for item in components?.queryItems ?? [] {
            query[item.name] = item.value ?? ""
        }
        return HTTPRequest(
            method: String(parts[0]),
            path: components?.path ?? String(parts[1]),
            query: query,
            headers: headers,
            body: buffer.subdata(in: bodyStart..<(bodyStart + contentLength))
        )
    }
}

struct HTTPResponse {
    var status: Int
    var contentType: String
    var body: Data

    static func json(_ object: Any, status: Int = 200) -> HTTPResponse {
        let data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
        return HTTPResponse(status: status, contentType: "application/json", body: data)
    }

    static func error(_ message: String, status: Int) -> HTTPResponse {
        json(["error": message], status: status)
    }

    func serialized() -> Data {
        let reason = [200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                      500: "Internal Server Error"][status] ?? "Error"
        var data = Data("HTTP/1.1 \(status) \(reason)\r\nContent-Type: \(contentType)\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8)
        data.append(body)
        return data
    }
}

/// Minimal HTTP/1.1 server: one request per connection, handled on the main queue.
/// Every request must carry the bearer token the host generated. On the simulator the
/// listener also refuses other machines; a real iPhone stays reachable through the
/// CoreDevice tunnel, which is why the token is what closes that path.
final class HTTPServer {
    private let listener: NWListener
    private let queue = DispatchQueue(label: "ghosthand.runner.http")
    private let token: String
    private let loopbackOnly: Bool
    private let handler: (HTTPRequest) -> HTTPResponse

    init(port: UInt16, token: String, loopbackOnly: Bool, handler: @escaping (HTTPRequest) -> HTTPResponse) throws {
        if token.isEmpty { throw RunnerError.badRequest("missing runner token") }
        let parameters = NWParameters.tcp
        parameters.allowLocalEndpointReuse = true
        guard let nwPort = NWEndpoint.Port(rawValue: port) else {
            throw RunnerError.badRequest("invalid port \(port)")
        }
        listener = try NWListener(using: parameters, on: nwPort)
        self.token = token
        self.loopbackOnly = loopbackOnly
        self.handler = handler
    }

    func start() {
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { return }
            // `acceptLocalOnly` also rejects the Mac's own loopback connections to the simulator.
            if self.loopbackOnly && !Self.isLoopback(connection.endpoint) {
                connection.cancel()
                return
            }
            connection.start(queue: self.queue)
            self.receive(on: connection, buffer: Data())
        }
        listener.start(queue: queue)
    }

    func stop() {
        listener.cancel()
    }

    private static func isLoopback(_ endpoint: NWEndpoint) -> Bool {
        guard case .hostPort(let host, _) = endpoint else { return false }
        switch host {
        case .ipv4(let address): return address.isLoopback
        case .ipv6(let address): return address.isLoopback || address.asIPv4?.isLoopback == true
        default: return false
        }
    }

    private func receive(on connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            var buffer = buffer
            if let data { buffer.append(data) }

            if let request = HTTPRequest.parse(buffer) {
                var response = HTTPResponse.error("no response", status: 500)
                if request.headers["authorization"] != "Bearer \(self.token)" {
                    response = HTTPResponse.error("unauthorized", status: 401)
                } else {
                    DispatchQueue.main.sync { response = self.handler(request) }
                }
                connection.send(content: response.serialized(), completion: .contentProcessed { _ in
                    connection.cancel()
                })
            } else if isComplete || error != nil {
                connection.cancel()
            } else {
                self.receive(on: connection, buffer: buffer)
            }
        }
    }
}
