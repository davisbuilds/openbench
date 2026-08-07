import AppKit
import CoreGraphics
import Darwin
import Foundation

private let protocolVersion = 2
private let heartbeatIntervalSeconds = 0.5

private struct ProtocolReply: Codable {
    let protocolVersion: Int
    let kind: String
}

private struct SessionReply: Codable {
    let protocolVersion: Int
    let kind: String
    let status: String
    let screenUnlocked: Bool?
    let observedAt: String
    let observedAtMonotonicNs: UInt64
    let sequence: UInt64
}

private struct AppRecord: Codable {
    let bundleIdentifier: String
    let version: String?
    let running: Bool
    let path: String?
}

private struct AppsReply: Codable {
    let protocolVersion: Int
    let kind: String
    let apps: [AppRecord]
}

private struct FocusReply: Codable {
    let protocolVersion: Int
    let kind: String
    let sampleKind: String
    let observedAt: String
    let observedAtMonotonicNs: UInt64
    let sequence: UInt64
    let bundleIdentifier: String?
    let applicationName: String?
    let pid: Int32?
    let sessionStatus: String
    let screenUnlocked: Bool?
}

private struct SessionState {
    let status: String
    let screenUnlocked: Bool?
}

private var sequence: UInt64 = 0

private func nextSequence() -> UInt64 {
    sequence += 1
    return sequence
}

private func timestamp() -> String {
    ISO8601DateFormatter.string(
        from: Date(),
        timeZone: TimeZone(secondsFromGMT: 0)!,
        formatOptions: [.withInternetDateTime, .withFractionalSeconds]
    )
}

private func emit<T: Encodable>(_ value: T) {
    do {
        var data = try JSONEncoder().encode(value)
        data.append(0x0A)
        FileHandle.standardOutput.write(data)
    } catch {
        FileHandle.standardError.write(Data("JSON encode failed: \(error)\n".utf8))
        exit(70)
    }
}

private func sessionState() -> SessionState {
    guard let session = CGSessionCopyCurrentDictionary() as? [String: Any] else {
        return SessionState(
            status: "unknown",
            screenUnlocked: nil
        )
    }
    let key = "CGSSessionScreenIsLocked"
    if let raw = session[key] {
        guard let locked = raw as? Bool else {
            return SessionState(
                status: "unknown",
                screenUnlocked: nil
            )
        }
        return SessionState(
            status: "known",
            screenUnlocked: !locked
        )
    }
    return SessionState(
        status: "known",
        screenUnlocked: true
    )
}

private func sessionReply() -> SessionReply {
    let session = sessionState()
    return SessionReply(
        protocolVersion: protocolVersion,
        kind: "session",
        status: session.status,
        screenUnlocked: session.screenUnlocked,
        observedAt: timestamp(),
        observedAtMonotonicNs: DispatchTime.now().uptimeNanoseconds,
        sequence: nextSequence()
    )
}

private func appVersion(at bundleURL: URL?) -> String? {
    guard let bundleURL, let bundle = Bundle(url: bundleURL) else {
        return nil
    }
    return (bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString")
        ?? bundle.object(forInfoDictionaryKey: "CFBundleVersion")) as? String
}

private func appsReply(bundleIdentifiers: Set<String>) -> AppsReply {
    let apps = NSWorkspace.shared.runningApplications.compactMap { app -> AppRecord? in
        guard let bundleIdentifier = app.bundleIdentifier,
              bundleIdentifiers.contains(bundleIdentifier) else {
            return nil
        }
        return AppRecord(
            bundleIdentifier: bundleIdentifier,
            version: appVersion(at: app.bundleURL),
            running: true,
            path: app.bundleURL?.standardizedFileURL.path
        )
    }
    return AppsReply(
        protocolVersion: protocolVersion,
        kind: "apps",
        apps: apps
    )
}

private func focusReply(
    _ app: NSRunningApplication?,
    sampleKind: String
) -> FocusReply {
    let session = sessionState()
    return FocusReply(
        protocolVersion: protocolVersion,
        kind: "focus",
        sampleKind: sampleKind,
        observedAt: timestamp(),
        observedAtMonotonicNs: DispatchTime.now().uptimeNanoseconds,
        sequence: nextSequence(),
        bundleIdentifier: app?.bundleIdentifier,
        applicationName: app?.localizedName,
        pid: app?.processIdentifier,
        sessionStatus: session.status,
        screenUnlocked: session.screenUnlocked
    )
}

private func runFocusMonitor() -> Never {
    let center = NSWorkspace.shared.notificationCenter
    let observer = center.addObserver(
        forName: NSWorkspace.didActivateApplicationNotification,
        object: nil,
        queue: .main
    ) { notification in
        let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey]
            as? NSRunningApplication
        emit(focusReply(app, sampleKind: "activation"))
    }
    let heartbeat = Timer.scheduledTimer(
        withTimeInterval: heartbeatIntervalSeconds,
        repeats: true
    ) { _ in
        emit(focusReply(
            NSWorkspace.shared.frontmostApplication,
            sampleKind: "heartbeat"
        ))
    }
    signal(SIGTERM, SIG_IGN)
    let termination = DispatchSource.makeSignalSource(
        signal: SIGTERM,
        queue: .main
    )
    termination.setEventHandler {
        heartbeat.invalidate()
        center.removeObserver(observer)
        emit(focusReply(
            NSWorkspace.shared.frontmostApplication,
            sampleKind: "terminal"
        ))
        exit(0)
    }
    termination.resume()
    emit(focusReply(
        NSWorkspace.shared.frontmostApplication,
        sampleKind: "baseline"
    ))
    RunLoop.main.run()
    exit(0)
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard let command = arguments.first else {
    FileHandle.standardError.write(Data("missing command\n".utf8))
    exit(64)
}

switch command {
case "protocol":
    emit(ProtocolReply(protocolVersion: protocolVersion, kind: "protocol"))
case "session":
    emit(sessionReply())
case "apps":
    emit(appsReply(bundleIdentifiers: Set(arguments.dropFirst())))
case "focus":
    runFocusMonitor()
default:
    FileHandle.standardError.write(Data("unknown command: \(command)\n".utf8))
    exit(64)
}
