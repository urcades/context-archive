import Foundation
import Darwin

public struct Source: Decodable, Sendable, Identifiable {
    public var id: String { path }
    public let path: String
    public let adapter: String?
    public let browser: String?
    public let profile: String?
    public let enabled: Bool
    public let status: String
    public let message: String?
    public let last_success_at: String?
}
public struct Issue: Decodable, Sendable { public let status: String; public let path: String?; public let message: String }
public struct Capture: Decodable, Sendable { public let capture_id: Int; public let source_id: String; public let rows: Int; public let changed: Int; public let deleted: Int }
public struct Cycle: Decodable, Sendable {
    public let id: String
    public let origin: String
    public let status: String
    public let started_at: String
    public let ended_at: String?
    public let captures: [Capture]
    public let failures: [Issue]
}
public struct ArchiveStatus: Decodable, Sendable {
    public let schema_version: Int
    public let paused: Bool
    public let collecting: Bool
    public let archive_exists: Bool
    public let sources: [Source]
    public let issues: [Issue]
    public let latest_cycle: Cycle?
    public let last_success_at: String?
    public let permission_executable: String
    public let archive_path: String
    public let config_path: String
}
public struct CommandResult: Sendable { public let status: Int32; public let stdout: Data; public let stderr: Data }
public enum CommandError: Error, LocalizedError {
    case failed(Int32, String), timedOut
    public var errorDescription: String? { switch self { case .failed(let code, let message): return "Command failed (\(code)): \(message)"; case .timedOut: return "Command timed out." } }
}
/// Each pipe has its own reader so a full stderr pipe cannot deadlock stdout.
public struct ProcessRunner: Sendable {
    public init() {}
    public func run(executable: URL, arguments: [String], environment: [String: String]? = nil, timeout: TimeInterval = 60, workingDirectory: URL? = nil) async throws -> CommandResult {
        let job = RunningProcess()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global().async {
                    do { continuation.resume(returning: try job.run(executable, arguments, environment, timeout, workingDirectory)) }
                    catch { continuation.resume(throwing: error) }
                }
            }
        } onCancel: { job.cancel() }
    }
}
private final class RunningProcess: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false
    func cancel() {
        lock.lock(); cancelled = true; let p = process; lock.unlock()
        if let p, p.isRunning { p.terminate(); let pid = p.processIdentifier; DispatchQueue.global().asyncAfter(deadline: .now() + 2) { if p.isRunning { kill(pid, SIGKILL) } } }
    }
    func run(_ executable: URL, _ arguments: [String], _ environment: [String: String]?, _ timeout: TimeInterval, _ workingDirectory: URL?) throws -> CommandResult {
        let p = Process(); p.executableURL = executable; p.arguments = arguments; p.environment = environment; p.currentDirectoryURL = workingDirectory
        let out = Pipe(), err = Pipe(); p.standardOutput = out; p.standardError = err
        lock.lock(); if cancelled { lock.unlock(); throw CancellationError() }; process = p
        do { try p.run() } catch { lock.unlock(); throw error }; lock.unlock()
        // Nonblocking reads alternate pipes; retain at most 2 MiB from each.
        // A descendant retaining a descriptor cannot hold completion indefinitely.
        let handles = [out.fileHandleForReading, err.fileHandleForReading]
        for handle in handles { _ = fcntl(handle.fileDescriptor, F_SETFL, O_NONBLOCK) }
        var outputs = [Data(), Data()], ended = [false, false]
        let deadline = Date().addingTimeInterval(timeout)
        var timedOut = false
        var exitDeadline: Date?
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            for index in 0..<2 where !ended[index] {
                let count = read(handles[index].fileDescriptor, &chunk, chunk.count)
                if count > 0 {
                    let remaining = max(0, 2 * 1024 * 1024 - outputs[index].count)
                    outputs[index].append(contentsOf: chunk.prefix(min(count, remaining)))
                } else if count == 0 { ended[index] = true }
                else if errno != EAGAIN && errno != EINTR { ended[index] = true }
            }
            if !p.isRunning {
                if exitDeadline == nil { exitDeadline = Date().addingTimeInterval(2) }
                if ended.allSatisfy({ $0 }) || Date() >= exitDeadline! { break }
            } else if Date() >= deadline && !timedOut {
                timedOut = true; cancel()
            }
            Thread.sleep(forTimeInterval: 0.005)
        }
        for handle in handles { try? handle.close() }
        p.waitUntilExit()
        if timedOut { throw CommandError.timedOut }
        lock.lock(); let wasCancelled = cancelled; lock.unlock()
        if wasCancelled { throw CancellationError() }
        return CommandResult(status: p.terminationStatus, stdout: outputs[0], stderr: outputs[1])
    }
}
public struct Engine: Sendable {
    public let resources: URL
    public init(resources: URL = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources")) { self.resources = resources }
    public var python: URL { resources.appendingPathComponent("python/bin/python3.12") }
    public var environment: [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PYTHONDONTWRITEBYTECODE"] = "1"; env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONSAFEPATH"] = "1"
        env["PYTHONPATH"] = resources.appendingPathComponent("engine").path
        env.removeValue(forKey: "PYTHONHOME")
        return env
    }
    public static var root: URL { URL(fileURLWithPath: ProcessInfo.processInfo.environment["CONTEXT_ARCHIVE_HOME"] ?? NSHomeDirectory() + "/Library/Application Support/ContextArchive") }
    public func call(_ arguments: [String], timeout: TimeInterval = 60) async throws -> Data {
        let result = try await ProcessRunner().run(executable: python, arguments: ["-m", "context_archive.cli"] + arguments, environment: environment, timeout: timeout, workingDirectory: resources.appendingPathComponent("engine"))
        guard result.status == 0 else { throw CommandError.failed(result.status, String(decoding: result.stderr.isEmpty ? result.stdout : result.stderr, as: UTF8.self)) }
        return result.stdout
    }
    public static func resourcesForExecutable() -> URL {
        // launchd may supply a relative BundleProgram or label as argv[0].
        // Ask dyld for the loaded image path rather than trusting invocation text.
        var capacity: UInt32 = 0
        _ = _NSGetExecutablePath(nil, &capacity)
        var buffer = [CChar](repeating: 0, count: Int(capacity))
        let result = buffer.withUnsafeMutableBufferPointer { _NSGetExecutablePath($0.baseAddress, &capacity) }
        precondition(result == 0, "Unable to resolve the running executable")
        let path = buffer.withUnsafeBufferPointer { String(cString: $0.baseAddress!) }
        return resources(relativeToExecutable: URL(fileURLWithPath: path))
    }
    public static func resources(relativeToExecutable executable: URL) -> URL {
        executable.resolvingSymlinksInPath().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Resources")
    }
}
public protocol BackgroundService: Sendable { func register() async throws; func unregister() async throws; func requiresApproval() async -> Bool }
public protocol MigrationEngine: Sendable { func prepare() async throws; func rollback() async throws; func verifyBackground(since: Date) async throws }
public struct ServiceCoordinator: Sendable {
    public let service: any BackgroundService
    public let migration: any MigrationEngine
    public init(service: any BackgroundService, migration: any MigrationEngine) { self.service = service; self.migration = migration }
    public func enable(requiresMigration: Bool) async throws {
        if requiresMigration { try await adopt() } else {
            let started = Date()
            do { try await service.register(); if !(await service.requiresApproval()) { try await migration.verifyBackground(since: started) } }
            catch { if !(await service.requiresApproval()) { try? await service.unregister() }; throw error }
        }
    }
    public func adopt() async throws {
        try await migration.prepare()
        let started = Date()
        do { try await service.register(); if !(await service.requiresApproval()) { try await migration.verifyBackground(since: started) } }
        catch { if !(await service.requiresApproval()) { try? await service.unregister(); try await migration.rollback() }; throw error }
    }
}
extension Engine: MigrationEngine {
    public func prepare() async throws { _ = try await call(["native-prepare-migration"], timeout: 180) }
    public func rollback() async throws { _ = try await call(["native-rollback"]) }
    public func verifyBackground(since: Date) async throws {
        let deadline = Date().addingTimeInterval(180)
        while Date() < deadline {
            try Task.checkCancellation()
            let status = try JSONDecoder().decode(ArchiveStatus.self, from: await call(["control-status"], timeout: 15))
            if try status.activationVerified(since: since) { return }
            try await Task.sleep(for: .seconds(2))
        }
        throw CommandError.failed(1, "Background collection did not produce a new completed receipt within three minutes. Inspect native-agent.log before retrying.")
    }
}

public enum CollectionHealth: String, Sendable { case paused = "Paused", collecting = "Collecting", healthy = "Healthy", partial = "Partial", setupRequired = "Setup Required" }
public extension ArchiveStatus {
    func health(serviceEnabled: Bool, requiresApproval: Bool, now: Date = Date()) -> CollectionHealth {
        if paused { return .paused }
        if collecting { return .collecting }
        if !serviceEnabled || requiresApproval { return .setupRequired }
        // Three missed five-minute opportunities are actionable even when discovery
        // returns no sources. An old successful receipt must not imply health.
        func stale(_ timestamp: String) -> Bool {
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            var parsed = formatter.date(from: timestamp)
            if parsed == nil { formatter.formatOptions = [.withInternetDateTime]; parsed = formatter.date(from: timestamp) }
            guard let parsed else { return true }
            return now.timeIntervalSince(parsed) >= 15 * 60
        }
        if let cycle = latest_cycle, cycle.status == "completed", stale(cycle.ended_at ?? cycle.started_at) { return .partial }
        if sources.contains(where: { $0.enabled && ($0.status == "stale" || $0.last_success_at.map(stale) == true) }) { return .partial }
        if !sources.contains(where: { $0.enabled }) { return .setupRequired }
        if latest_cycle?.status == "failed" || latest_cycle?.status == "partial" || latest_cycle?.status == "interrupted" || !issues.isEmpty { return .partial }
        if latest_cycle?.origin == "background" && latest_cycle?.status == "completed" { return .healthy }
        return .setupRequired
    }
}

public extension ArchiveStatus {
    /// A preexisting archive or old successful capture cannot prove this activation.
    func activationVerified(since: Date) throws -> Bool {
        guard let cycle = latest_cycle, cycle.origin == "background", let ended = cycle.ended_at else { return false }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var started = formatter.date(from: cycle.started_at)
        if started == nil { formatter.formatOptions = [.withInternetDateTime]; started = formatter.date(from: cycle.started_at) }
        guard let started, started >= since, !ended.isEmpty else { return false }
        if cycle.failures.contains(where: { $0.status == "permission_denied" }) {
            throw CommandError.failed(1, "Background collection lacks Full Disk Access. Grant access to \(permission_executable) in Privacy & Security, then retry migration.")
        }
        if cycle.status == "paused" { return true } // Confirms scheduling, not protected-data access.
        if cycle.status == "completed" { return true }
        if cycle.status == "partial" && !cycle.captures.isEmpty { return true }
        throw CommandError.failed(1, "Background activation failed: \(cycle.failures.map(\.message).joined(separator: "; "))")
    }
}
