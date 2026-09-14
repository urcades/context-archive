import Foundation
import Darwin
import ContextArchiveCore
let engine = Engine(resources: Engine.resourcesForExecutable())
// Replace this process, preserving stdin/stdout exactly for MCP framing and signals.
let args = [engine.python.path, "-m", "context_archive.cli"] + CommandLine.arguments.dropFirst()
for (key, value) in engine.environment { setenv(key, value, 1) }
unsetenv("PYTHONHOME")
guard chdir(engine.resources.appendingPathComponent("engine").path) == 0 else { exit(127) }
let pointers = args.map { strdup($0) } + [nil]
pointers.withUnsafeBufferPointer { _ = execv(engine.python.path, $0.baseAddress!) }
fputs("Unable to launch bundled Python: \(String(cString: strerror(errno)))\n", stderr)
exit(127)
