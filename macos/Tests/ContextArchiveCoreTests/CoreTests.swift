import XCTest
@testable import ContextArchiveCore

actor Recorder { var events: [String] = []; func add(_ value: String) { events.append(value) } }
struct StubService: BackgroundService {
    let recorder: Recorder; let fail: Bool; let approval: Bool
    func register() async throws { await recorder.add("register"); if fail { throw CommandError.failed(1, "registration failed") } }
    func unregister() async throws { await recorder.add("unregister") }
    func requiresApproval() async -> Bool { approval }
}
struct StubMigration: MigrationEngine {
    let recorder: Recorder
    var verificationFails = false
    func verifyBackground(since: Date) async throws { await recorder.add("verify"); if verificationFails { throw CommandError.failed(1, "no receipt") } }
    func prepare() async throws { await recorder.add("prepare") }
    func rollback() async throws { await recorder.add("rollback") }
}
final class CoreTests: XCTestCase, @unchecked Sendable {
    func testEnableRequiresMigration() async throws {
        let recorder = Recorder()
        try await ServiceCoordinator(service: StubService(recorder: recorder, fail: false, approval: false), migration: StubMigration(recorder: recorder)).enable(requiresMigration: true)
        let events = await recorder.events; XCTAssertEqual(events, ["prepare", "register", "verify"])
    }
    func testStartupFailureRollsBack() async {
        let recorder = Recorder()
        do { try await ServiceCoordinator(service: StubService(recorder: recorder, fail: false, approval: false), migration: StubMigration(recorder: recorder, verificationFails: true)).adopt(); XCTFail("expected startup failure") } catch {}
        let events = await recorder.events; XCTAssertEqual(events, ["prepare", "register", "verify", "unregister", "rollback"])
    }
    func testSuccessfulPendingApprovalSkipsVerification() async throws {
        let recorder = Recorder()
        try await ServiceCoordinator(service: StubService(recorder: recorder, fail: false, approval: true), migration: StubMigration(recorder: recorder)).adopt()
        let events = await recorder.events; XCTAssertEqual(events, ["prepare", "register"])
    }
    func testFailureRollsBackPreparedMigration() async {
        let recorder = Recorder(); let coordinator = ServiceCoordinator(service: StubService(recorder: recorder, fail: true, approval: false), migration: StubMigration(recorder: recorder))
        do { try await coordinator.adopt(); XCTFail("expected failure") } catch {}
        let events = await recorder.events; XCTAssertEqual(events, ["prepare", "register", "unregister", "rollback"])
    }
    func testApprovalPendingKeepsMigration() async {
        let recorder = Recorder(); let coordinator = ServiceCoordinator(service: StubService(recorder: recorder, fail: true, approval: true), migration: StubMigration(recorder: recorder))
        do { try await coordinator.adopt(); XCTFail("expected pending") } catch {}
        let events = await recorder.events; XCTAssertEqual(events, ["prepare", "register"])
    }
    func testProcessExitAndPipeDrain() async throws {
        let result = try await ProcessRunner().run(executable: URL(fileURLWithPath: "/bin/sh"), arguments: ["-c", "printf hello; printf problem >&2; exit 7"])
        XCTAssertEqual(result.status, 7); XCTAssertEqual(String(decoding: result.stdout, as: UTF8.self), "hello"); XCTAssertEqual(String(decoding: result.stderr, as: UTF8.self), "problem")
    }
    func testProcessTimeout() async throws {
        do { _ = try await ProcessRunner().run(executable: URL(fileURLWithPath: "/bin/sleep"), arguments: ["5"], timeout: 0.05); XCTFail("expected timeout") } catch CommandError.timedOut {} catch { XCTFail("\(error)") }
    }
    func testCancellationTerminatesProcess() async throws {
        let task = Task { try await ProcessRunner().run(executable: URL(fileURLWithPath: "/bin/sleep"), arguments: ["10"]) }
        try await Task.sleep(for: .milliseconds(100)); task.cancel()
        do { _ = try await task.value; XCTFail("expected cancellation") } catch is CancellationError {} catch { XCTFail("\(error)") }
    }
    func testLargeIndependentPipes() async throws {
        let result = try await ProcessRunner().run(executable: URL(fileURLWithPath: "/bin/sh"), arguments: ["-c", "dd if=/dev/zero bs=65536 count=4 >&2 2>/dev/null; printf done"])
        XCTAssertEqual(result.stderr.count, 262144)
        XCTAssertEqual(String(decoding: result.stdout, as: UTF8.self), "done")
    }
    func testOutputIsBounded() async throws {
        let result = try await ProcessRunner().run(executable: URL(fileURLWithPath: "/bin/dd"), arguments: ["if=/dev/zero", "bs=1048576", "count=4"])
        XCTAssertEqual(result.stdout.count, 2 * 1024 * 1024)
    }
    func testHealthTransitions() throws {
        let now = Date(timeIntervalSince1970: 1789344000)
        func status(paused: Bool = false, collecting: Bool = false, cycle: String = "completed", origin: String = "background", ended: String = "2026-09-14T00:00:00Z", sourceState: String = "ok", hasSources: Bool = true) throws -> ArchiveStatus {
            let json = """
            {"schema_version":1,"paused":\(paused),"collecting":\(collecting),"archive_exists":true,"sources":\(hasSources ? "[{\"path\":\"/p\",\"adapter\":null,\"enabled\":true,\"status\":\"\(sourceState)\"}]" : "[]"),"issues":[],"permission_executable":"/python","archive_path":"/a","config_path":"/c","latest_cycle":{"id":"cycle","origin":"\(origin)","status":"\(cycle)","started_at":"2026-09-14T00:00:00Z","ended_at":"\(ended)","captures":[{"capture_id":1,"source_id":"uuid","rows":1,"changed":1,"deleted":0}],"failures":[]}}
            """
            return try JSONDecoder().decode(ArchiveStatus.self, from: Data(json.utf8))
        }
        XCTAssertEqual(try status().health(serviceEnabled: true, requiresApproval: false, now: now), .healthy)
        XCTAssertEqual(try status(paused: true).health(serviceEnabled: true, requiresApproval: false, now: now), .paused)
        XCTAssertEqual(try status(collecting: true).health(serviceEnabled: true, requiresApproval: false, now: now), .collecting)
        XCTAssertEqual(try status(cycle: "partial").health(serviceEnabled: true, requiresApproval: false, now: now), .partial)
        XCTAssertEqual(try status(cycle: "failed").health(serviceEnabled: true, requiresApproval: false, now: now), .partial)
        XCTAssertEqual(try status(cycle: "interrupted").health(serviceEnabled: true, requiresApproval: false, now: now), .partial)
        XCTAssertEqual(try status().health(serviceEnabled: false, requiresApproval: false, now: now), .setupRequired)
        XCTAssertEqual(try status().health(serviceEnabled: true, requiresApproval: true, now: now), .setupRequired)
        XCTAssertEqual(try status(origin: "cli").health(serviceEnabled: true, requiresApproval: false, now: now), .setupRequired)
        XCTAssertEqual(try status(ended: "2026-09-13T23:45:00Z").health(serviceEnabled: true, requiresApproval: false, now: now), .partial)
        XCTAssertEqual(try status(ended: "2026-09-13T23:45:00Z", hasSources: false).health(serviceEnabled: true, requiresApproval: false, now: now), .partial)
        XCTAssertEqual(try status(sourceState: "stale").health(serviceEnabled: true, requiresApproval: false, now: now), .partial)
    }
    func testActivationRequiresNewBackgroundEvidence() throws {
        let since = Date(timeIntervalSince1970: 1789344000)
        func status(_ state: String = "completed", origin: String = "background", started: String = "2026-09-14T00:00:01Z", permissionDenied: Bool = false, captured: Bool = true) throws -> ArchiveStatus {
            let receipt: [String: Any] = ["id": "new", "origin": origin, "status": state, "started_at": started, "ended_at": "2026-09-14T00:00:02Z", "captures": captured ? [["capture_id": 1, "source_id": "id", "rows": 1, "changed": 1, "deleted": 0]] : [], "failures": permissionDenied ? [["status": "permission_denied", "message": "Denied"]] : []]
            let data = try JSONSerialization.data(withJSONObject: ["schema_version": 1, "paused": false, "collecting": false, "archive_exists": true, "sources": [], "issues": [], "permission_executable": "/app/python", "archive_path": "/archive", "config_path": "/config", "latest_cycle": receipt])
            return try JSONDecoder().decode(ArchiveStatus.self, from: data)
        }
        XCTAssertTrue(try status().activationVerified(since: since))
        XCTAssertTrue(try status("partial").activationVerified(since: since))
        XCTAssertTrue(try status("paused", captured: false).activationVerified(since: since))
        XCTAssertFalse(try status(origin: "manual").activationVerified(since: since))
        XCTAssertFalse(try status(started: "2026-09-13T23:59:59Z").activationVerified(since: since))
        XCTAssertThrowsError(try status("failed", captured: false).activationVerified(since: since))
        XCTAssertThrowsError(try status("partial", captured: false).activationVerified(since: since))
        XCTAssertThrowsError(try status("partial", permissionDenied: true).activationVerified(since: since))
    }
    func testResourcePathUsesLoadedExecutable() {
        let path = Engine.resourcesForExecutable()
        XCTAssertTrue(path.path.hasPrefix("/"))
        XCTAssertEqual(path.lastPathComponent, "Resources")
        XCTAssertEqual(Engine.resources(relativeToExecutable: URL(fileURLWithPath: "/Applications/Context Archive.app/Contents/MacOS/ContextArchiveAgent")).path, "/Applications/Context Archive.app/Contents/Resources")
    }
    func testStatusDecoding() throws {
        let json = #"{"schema_version":1,"paused":false,"collecting":false,"archive_exists":true,"sources":[{"path":"/profile","adapter":null,"enabled":true,"status":"ok"}],"issues":[],"permission_executable":"/python","archive_path":"/archive","config_path":"/config","latest_cycle":null}"#
        let status = try JSONDecoder().decode(ArchiveStatus.self, from: Data(json.utf8)); XCTAssertEqual(status.sources.first?.path, "/profile"); XCTAssertFalse(status.paused)
    }
}
