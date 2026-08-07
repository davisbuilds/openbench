import AppKit
import CoreGraphics
import Foundation

private let protocolVersion = 1

private struct ProtocolReply: Codable {
    let protocolVersion: Int
    let kind: String
}

private struct SessionReply: Codable {
    let protocolVersion: Int
    let kind: String
    let status: String
    let screenUnlocked: Bool?
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
    let bundleIdentifier: String?
    let applicationName: String?
    let pid: Int32?
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

private func sessionReply() -> SessionReply {
    guard let session = CGSessionCopyCurrentDictionary() as? [String: Any] else {
        return SessionReply(
            protocolVersion: protocolVersion,
            kind: "session",
            status: "unknown",
            screenUnlocked: nil
        )
    }
    let key = "CGSSessionScreenIsLocked"
    if let raw = session[key] {
        guard let locked = raw as? Bool else {
            return SessionReply(
                protocolVersion: protocolVersion,
                kind: "session",
                status: "unknown",
                screenUnlocked: nil
            )
        }
        return SessionReply(
            protocolVersion: protocolVersion,
            kind: "session",
            status: "known",
            screenUnlocked: !locked
        )
    }
    return SessionReply(
        protocolVersion: protocolVersion,
        kind: "session",
        status: "known",
        screenUnlocked: true
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

private func focusReply(_ app: NSRunningApplication?) -> FocusReply {
    FocusReply(
        protocolVersion: protocolVersion,
        kind: "focus",
        bundleIdentifier: app?.bundleIdentifier,
        applicationName: app?.localizedName,
        pid: app?.processIdentifier
    )
}

private func runFocusMonitor() -> Never {
    let center = NSWorkspace.shared.notificationCenter
    _ = center.addObserver(
        forName: NSWorkspace.didActivateApplicationNotification,
        object: nil,
        queue: .main
    ) { notification in
        let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey]
            as? NSRunningApplication
        emit(focusReply(app))
    }
    emit(focusReply(NSWorkspace.shared.frontmostApplication))
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
