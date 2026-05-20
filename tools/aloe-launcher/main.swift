// aloe-launcher — Native Mach-O CFBundleExecutable for Aloe Scribe.app.
//
// We were using a bash shell script as the bundle's main executable, which
// macOS Tahoe (26.x) appears to treat as "not a real app binary" and uses to
// silently filter NSStatusItem additions made by the child Python process.
// Replacing the bash launcher with a 30-line Swift program that runs as a
// proper Mach-O binary signed with the bundle's identity gives macOS a
// recognizable executable to attribute the menu bar item against.
//
// Behavior is identical to the old aloe-scribe shell script: set PYTHONHOME
// + PYTHONPATH so the embedded interpreter finds the homebrew framework's
// stdlib and the project venv's site-packages, chdir into the project dir,
// then execve to the bundled Python with src/main.py as the script.

import Foundation
import Darwin

// Paths are baked at build time by scripts/build-app.sh.
let PYTHON_HOME = "__PYTHON_HOME__"
let PYTHONPATH_VALUE = "__PYTHONPATH__"
let PROJECT_DIR = "__PROJECT_DIR__"
let MAIN_PY = "\(PROJECT_DIR)/src/main.py"

// The Python interpreter lives in Contents/Resources/python/ — NOT
// Contents/MacOS/. macOS only treats Contents/MacOS/ binaries as the
// bundle's main executable; any other binary in there gets registered
// as its own separate app in TCC/Privacy, which led to a phantom
// "aloe-python" entry in Screen Recording and tripped the menu bar
// status item filter.
let argv0 = CommandLine.arguments[0]
let bundleContents = URL(fileURLWithPath: argv0)
    .deletingLastPathComponent()   // Contents/MacOS/
    .deletingLastPathComponent()   // Contents/
let pythonExec = bundleContents
    .appendingPathComponent("Resources")
    .appendingPathComponent("python")
    .appendingPathComponent("aloe-python")
    .path

setenv("PYTHONHOME", PYTHON_HOME, 1)
setenv("PYTHONPATH", PYTHONPATH_VALUE, 1)
setenv("ALOE_SCRIBE_PROJECT_DIR", PROJECT_DIR, 1)

chdir(PROJECT_DIR)

// Pass through any args this launcher was given, in case macOS launches us
// with file arguments (e.g. CFBundleDocumentTypes wiring later).
let extraArgs = Array(CommandLine.arguments.dropFirst())
let argList = [pythonExec, MAIN_PY] + extraArgs
let cArgv: [UnsafeMutablePointer<CChar>?] = argList.map { strdup($0) } + [nil]

execv(pythonExec, cArgv)
// execv only returns on failure.
perror("aloe-launcher: execv failed")
exit(1)
