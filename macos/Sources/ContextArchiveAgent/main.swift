import Foundation
import Darwin
import ContextArchiveCore
let root = Engine.root
let logs = root.appendingPathComponent("logs")
do {
    try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    chmod(root.path, 0o700); chmod(logs.path, 0o700)
    let log = logs.appendingPathComponent("native-agent.log")
    let fd = open(log.path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { exit(1) }
    fchmod(fd, 0o600); dup2(fd, STDOUT_FILENO); dup2(fd, STDERR_FILENO); close(fd)
    let engine = Engine(resources: Engine.resourcesForExecutable())
    let output = try await engine.call(["control-cycle", "--origin", "background"], timeout: 900)
    FileHandle.standardOutput.write(output)
} catch {
    FileHandle.standardError.write(Data("\(Date()): \(error.localizedDescription)\n".utf8)); exit(1)
}
