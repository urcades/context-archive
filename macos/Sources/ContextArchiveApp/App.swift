import SwiftUI
import ServiceManagement
import ContextArchiveCore
import Darwin

struct NativeService: BackgroundService {
    static var legacyExists: Bool { FileManager.default.fileExists(atPath: NSHomeDirectory() + "/Library/LaunchAgents/local.context-archive.plist") }
    static func enable(adopt: Bool, engine: Engine) async throws {
        // Re-enabling an existing registration owns no new activation and must not
        // time out waiting for its next scheduled cycle, then unregister it.
        guard SMAppService.agent(plistName: "io.github.urcades.ContextArchive.agent.plist").status != .enabled else { return }
        let check = try await ProcessRunner().run(executable: URL(fileURLWithPath: "/bin/launchctl"), arguments: ["print", "gui/\(getuid())/local.context-archive"])
        try await ServiceCoordinator(service: NativeService(), migration: engine).enable(requiresMigration: adopt || legacyExists || check.status == 0)
    }
    func register() async throws { try SMAppService.agent(plistName: "io.github.urcades.ContextArchive.agent.plist").register() }
    func unregister() async throws { try await SMAppService.agent(plistName: "io.github.urcades.ContextArchive.agent.plist").unregister() }
    func requiresApproval() async -> Bool { SMAppService.agent(plistName: "io.github.urcades.ContextArchive.agent.plist").status == .requiresApproval }
}
@MainActor @Observable final class Model {
    var status: ArchiveStatus?
    var message = ""
    var busy = false
    var serviceStatus = "Not registered"
    var serviceEnabled = false
    var approvalRequired = false
    var health: String { status?.health(serviceEnabled: serviceEnabled, requiresApproval: approvalRequired).rawValue ?? "Setup Required" }
    let engine = Engine()
    func refresh() async {
        let service = SMAppService.agent(plistName: "io.github.urcades.ContextArchive.agent.plist")
        serviceEnabled = service.status == .enabled
        approvalRequired = service.status == .requiresApproval
        switch service.status { case .enabled: serviceStatus = "Registered"; case .requiresApproval: serviceStatus = "Approval required in Login Items"; case .notFound: serviceStatus = "Service not found"; default: serviceStatus = "Not registered" }
        do { status = try JSONDecoder().decode(ArchiveStatus.self, from: await engine.call(["control-status"])) } catch { message = error.localizedDescription } }
    func perform(_ operation: @escaping @Sendable () async throws -> Void) { guard !busy else { return }; busy = true; Task { do { try await operation(); message = "Done" } catch { message = error.localizedDescription }; busy = false; await refresh() } }
    func update(_ params: [String: Any]) { guard let data = try? JSONSerialization.data(withJSONObject: params), let json = String(data: data, encoding: .utf8) else { return }; let e = engine; perform { _ = try await e.call(["control-update", "--params", json]) } }
    func runNow() { perform { let result = try await ProcessRunner().run(executable: URL(fileURLWithPath: "/bin/launchctl"), arguments: ["kickstart", "gui/\(getuid())/io.github.urcades.ContextArchive.agent"]); if result.status != 0 { throw CommandError.failed(result.status, String(decoding: result.stderr, as: UTF8.self)) } } }
    func register(adopt: Bool) { let e = engine; perform { try await NativeService.enable(adopt: adopt, engine: e); if await NativeService().requiresApproval() { await MainActor.run { SMAppService.openSystemSettingsLoginItems() } } } }
    func copyMCP() { let cli = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/context-archive").path; let archive = status?.archive_path ?? Engine.root.appendingPathComponent("data/archive.sqlite3").path; NSPasteboard.general.clearContents(); NSPasteboard.general.setString("codex mcp add context-archive -- '\(cli.replacingOccurrences(of: "'", with: "'\\''"))' --archive '\(archive.replacingOccurrences(of: "'", with: "'\\''"))' mcp", forType: .string) }
    func setupMCP() { let picker = NSOpenPanel(); picker.title = "Choose your Codex CLI executable"; picker.canChooseDirectories = false; guard picker.runModal() == .OK, let executable = picker.url else { return }; let cli = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/context-archive").path; let archive = status?.archive_path ?? Engine.root.appendingPathComponent("data/archive.sqlite3").path; perform { let result = try await ProcessRunner().run(executable: executable, arguments: ["mcp", "add", "context-archive", "--", cli, "--archive", archive, "mcp"]); if result.status != 0 { throw CommandError.failed(result.status, String(decoding: result.stderr, as: UTF8.self)) } } }
}
@main struct ContextArchiveApp: App {
    @State private var model = Model()
    var body: some Scene {
        MenuBarExtra("Context Archive", systemImage: "archivebox") {
            Text(model.health)
            if let last = model.status?.last_success_at { Text("Last success: \(last)") }
            if let cycle = model.status?.latest_cycle { Text("Last cycle: \(cycle.origin) · \(cycle.status)"); Text(cycle.ended_at ?? cycle.started_at) }
            Button("Run Now") { model.runNow() }.disabled(model.busy)
            Button(model.status?.paused == true ? "Resume Capture" : "Pause Capture") { model.update(["paused": !(model.status?.paused ?? false)]) }
            SettingsLink { Text("Settings…") }
            Button("Open Logs") { NSWorkspace.shared.open(Engine.root.appendingPathComponent("logs")) }
            Divider()
            Button("Quit Context Archive") { NSApplication.shared.terminate(nil) }
            Text("Background collection continues after quitting.")
            Color.clear.frame(width: 0, height: 0).task { while !Task.isCancelled { await model.refresh(); try? await Task.sleep(for: .seconds(5)) } }
        }
        Settings { SettingsView(model: model) }
    }
    init() {
        let flags = CommandLine.arguments
        if flags.contains("--adopt") || flags.contains("--register") || flags.contains("--unregister") {
            Task { do { if flags.contains("--unregister") { try await NativeService().unregister() } else { try await NativeService.enable(adopt: flags.contains("--adopt"), engine: Engine()) }; exit(0) } catch { FileHandle.standardError.write(Data("\(error)\n".utf8)); exit(1) } }
        }
    }
}
struct SettingsView: View {
    @Bindable var model: Model
    var body: some View {
        ScrollView { Form {
            Text("Context Archive").font(.title)
            LabeledContent("Capture", value: model.health)
            LabeledContent("Background service", value: model.serviceStatus)
            Text("Capture stays on this Mac. Enable individual browser profiles below; pausing keeps the archive and stops future capture.")
            ForEach(model.status?.sources ?? []) { source in
                Toggle(isOn: Binding(get: { source.enabled }, set: { model.update(["source_path": source.path, "enabled": $0]) })) { VStack(alignment: .leading) { Text([source.browser ?? source.adapter ?? "Unavailable source", source.profile].compactMap { $0 }.joined(separator: " — ")); Text(source.path).font(.caption).textSelection(.enabled); Text(source.message ?? source.status).font(.caption); if let last = source.last_success_at { Text("Last success: \(last)").font(.caption) } } }
            }
            if let cycle = model.status?.latest_cycle { Text("Last cycle: \(cycle.origin) · \(cycle.status) · \(cycle.captures.count) captures"); Text(cycle.ended_at ?? cycle.started_at).font(.caption); ForEach(Array(cycle.failures.enumerated()), id: \.offset) { _, issue in Text(issue.message).foregroundStyle(.orange) } }
            ForEach(Array((model.status?.issues ?? []).enumerated()), id: \.offset) { _, issue in Text(issue.message).foregroundStyle(.orange) }
            Text("Full Disk Access may be needed for protected browser data. Add the executable shown below in Privacy & Security → Full Disk Access.").font(.caption)
            Text(model.status?.permission_executable ?? model.engine.python.path).font(.caption).textSelection(.enabled)
            Button("Open Full Disk Access Settings") { NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!) }
            if let status = model.status { LabeledContent("Archive", value: status.archive_path).textSelection(.enabled); LabeledContent("Configuration", value: status.config_path).textSelection(.enabled); HStack { Button("Open Archive Folder") { NSWorkspace.shared.open(URL(fileURLWithPath: status.archive_path).deletingLastPathComponent()) }; Button("Open Configuration") { NSWorkspace.shared.open(URL(fileURLWithPath: status.config_path)) } } }
            HStack { Button("Enable Background Collection") { model.register(adopt: false) }.disabled(model.serviceEnabled); Button("Disable Background Collection") { model.perform { try await NativeService().unregister() } } }
            Text("Use the migration action for an existing collector: it saves the previous setup before switching services.").font(.caption)
            HStack { Button("Use Existing Context Archive") { model.register(adopt: true) }.disabled(model.serviceEnabled); Button("Restore Previous Collector") { let e = model.engine; model.perform { try await NativeService().unregister(); try await e.rollback() } } }
            HStack { Button("Copy Codex MCP Setup Command") { model.copyMCP() }; Button("Set Up MCP with Codex CLI…") { model.setupMCP() } }
            Text("MCP setup uses the Codex executable you select to register this app’s bundled command.").font(.caption)
            Text(model.message).textSelection(.enabled)
        }.padding() }.frame(width: 680, height: 720).disabled(model.busy)
        .task { while !Task.isCancelled { await model.refresh(); try? await Task.sleep(for: .seconds(10)) } }
    }
}
