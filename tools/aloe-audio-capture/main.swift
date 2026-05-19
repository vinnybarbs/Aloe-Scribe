// aloe-audio-capture — Mic + system-audio capture for Aloe Scribe.
//
// System audio is tapped via ScreenCaptureKit (no BlackHole, no Multi-Output
// Device required). Mic audio is captured via AVAudioEngine, with the device
// chosen by name (the same name avfoundation/CoreAudio exposes).
//
// Both streams are downsampled to 16 kHz mono Int16, mixed sample-aligned
// inside this binary, and written to a single WAV file.
//
// Usage:
//   aloe-audio-capture --output recording.wav \
//                      [--mic "Jabra SPEAK 510 USB"] \
//                      [--sample-rate 16000] [--channels 1] \
//                      [--no-system]
//
// Stops when:
//   - stdin receives the byte 'q' or hits EOF
//   - the process receives SIGINT or SIGTERM
//
// The WAV header is finalized on clean shutdown only — kill -9 leaves the file
// in an unplayable state.

import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

// MARK: - Helpers

func eprint(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

// Saturating add for Int16 mixing.
@inline(__always)
func saturateMix(_ a: Int16, _ b: Int16) -> Int16 {
    let sum = Int32(a) + Int32(b)
    if sum > 32_767 { return 32_767 }
    if sum < -32_768 { return -32_768 }
    return Int16(sum)
}

// MARK: - CLI parsing

struct Args {
    let output: String
    let micDeviceName: String?      // nil = no mic capture
    let captureSystem: Bool          // --no-system disables system capture
    let sampleRate: Double
    let channels: AVAudioChannelCount
}

func parseArgs() -> Args {
    var output: String?
    var micDeviceName: String?
    var captureSystem = true
    var sampleRate: Double = 16_000
    var channels: AVAudioChannelCount = 1

    let argv = CommandLine.arguments
    var i = 1
    while i < argv.count {
        let arg = argv[i]
        switch arg {
        case "--output", "-o":
            i += 1
            guard i < argv.count else { eprint("--output requires a value"); exit(2) }
            output = argv[i]
        case "--mic", "-m":
            i += 1
            guard i < argv.count else { eprint("--mic requires a value"); exit(2) }
            micDeviceName = argv[i]
        case "--no-system":
            captureSystem = false
        case "--sample-rate", "-r":
            i += 1
            guard i < argv.count, let v = Double(argv[i]) else {
                eprint("--sample-rate requires a number"); exit(2)
            }
            sampleRate = v
        case "--channels", "-c":
            i += 1
            guard i < argv.count, let v = UInt32(argv[i]) else {
                eprint("--channels requires a number"); exit(2)
            }
            channels = AVAudioChannelCount(v)
        case "--help", "-h":
            print("""
            Usage: aloe-audio-capture --output <path> [--mic <name>] [--no-system]
                                      [--sample-rate 16000] [--channels 1]

            At least one source (system audio or --mic) must be active.
            Stops on 'q' on stdin, EOF on stdin, SIGINT, or SIGTERM.
            """)
            exit(0)
        default:
            eprint("Unknown argument: \(arg)")
            exit(2)
        }
        i += 1
    }

    guard let output = output else {
        eprint("Error: --output is required")
        exit(2)
    }
    if !captureSystem && micDeviceName == nil {
        eprint("Error: must capture at least one source (use --mic or drop --no-system)")
        exit(2)
    }
    return Args(output: output,
                micDeviceName: micDeviceName,
                captureSystem: captureSystem,
                sampleRate: sampleRate,
                channels: channels)
}

// MARK: - Mixer

final class Mixer: @unchecked Sendable {
    private let outputFormat: AVAudioFormat
    private let audioFile: AVAudioFile
    private let expectsSystem: Bool
    private let expectsMic: Bool

    // Both buffers carry Int16 samples at outputFormat.sampleRate, mono.
    private var systemFrames: [Int16] = []
    private var micFrames: [Int16] = []

    // Diagnostics
    private(set) var framesWritten: Int = 0

    // Serializes all buffer mutations + writes.
    private let queue = DispatchQueue(label: "aloe.mixer", qos: .userInteractive)
    private var stopped = false

    init(outputPath: String,
         sampleRate: Double,
         channels: AVAudioChannelCount,
         expectsSystem: Bool,
         expectsMic: Bool) throws {
        guard let fmt = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                       sampleRate: sampleRate,
                                       channels: channels,
                                       interleaved: true) else {
            throw NSError(domain: "aloe", code: 20,
                          userInfo: [NSLocalizedDescriptionKey: "Could not build output AVAudioFormat"])
        }
        self.outputFormat = fmt
        self.expectsSystem = expectsSystem
        self.expectsMic = expectsMic

        let url = URL(fileURLWithPath: outputPath)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: channels,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        self.audioFile = try AVAudioFile(forWriting: url,
                                         settings: settings,
                                         commonFormat: .pcmFormatInt16,
                                         interleaved: true)
    }

    var format: AVAudioFormat { outputFormat }

    /// Push system-audio samples (already converted to outputFormat).
    func feedSystem(_ samples: [Int16]) {
        queue.async { [weak self] in
            guard let self = self, !self.stopped else { return }
            self.systemFrames.append(contentsOf: samples)
            self.drainLocked()
        }
    }

    /// Push mic samples (already converted to outputFormat).
    func feedMic(_ samples: [Int16]) {
        queue.async { [weak self] in
            guard let self = self, !self.stopped else { return }
            self.micFrames.append(contentsOf: samples)
            self.drainLocked()
        }
    }

    /// Pops aligned chunks and writes them to the WAV. Caller must be on `queue`.
    private func drainLocked() {
        let n: Int
        if expectsSystem && expectsMic {
            n = min(systemFrames.count, micFrames.count)
        } else if expectsSystem {
            n = systemFrames.count
        } else {
            n = micFrames.count
        }
        if n == 0 { return }

        var mixed = [Int16](repeating: 0, count: n)
        if expectsSystem && expectsMic {
            for i in 0..<n {
                mixed[i] = saturateMix(systemFrames[i], micFrames[i])
            }
        } else if expectsSystem {
            for i in 0..<n { mixed[i] = systemFrames[i] }
        } else {
            for i in 0..<n { mixed[i] = micFrames[i] }
        }

        // Drop the consumed prefixes.
        if expectsSystem { systemFrames.removeFirst(min(n, systemFrames.count)) }
        if expectsMic    { micFrames.removeFirst(min(n, micFrames.count)) }

        writeFrames(mixed)
    }

    private func writeFrames(_ samples: [Int16]) {
        let frameCount = AVAudioFrameCount(samples.count)
        guard let buf = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: frameCount) else {
            eprint("Mixer: could not allocate PCM buffer")
            return
        }
        buf.frameLength = frameCount
        guard let dst = buf.int16ChannelData?[0] else {
            eprint("Mixer: int16ChannelData is nil")
            return
        }
        samples.withUnsafeBufferPointer { src in
            dst.update(from: src.baseAddress!, count: samples.count)
        }
        // Diagnostic: report peak amplitude per write (kept once per ~1 sec).
        let priorSec = framesWritten / 16000
        let nextSec = (framesWritten + samples.count) / 16000
        if framesWritten == 0 || priorSec != nextSec {
            var peak: Int32 = 0
            for s in samples {
                let a = Int32(s).magnitude
                if Int32(a) > peak { peak = Int32(a) }
            }
            eprint("Mixer: write \(samples.count) frames, peak=\(peak)")
        }
        do {
            try audioFile.write(from: buf)
            framesWritten += samples.count
        } catch {
            eprint("Mixer write error: \(error.localizedDescription)")
        }
    }

    /// Drains whatever remains, padding the short side with zeros, then closes the file.
    func finalize() {
        queue.sync {
            guard !stopped else { return }
            stopped = true
            // If both sources are expected, pad the short side to the length
            // of the long side and mix the rest. Don't leave audio behind.
            if expectsSystem && expectsMic {
                let target = max(systemFrames.count, micFrames.count)
                if systemFrames.count < target {
                    systemFrames.append(contentsOf: [Int16](repeating: 0, count: target - systemFrames.count))
                }
                if micFrames.count < target {
                    micFrames.append(contentsOf: [Int16](repeating: 0, count: target - micFrames.count))
                }
            }
            // One final drain (now both buffers are equal length).
            let n: Int
            if expectsSystem && expectsMic {
                n = systemFrames.count
            } else if expectsSystem {
                n = systemFrames.count
            } else {
                n = micFrames.count
            }
            if n > 0 {
                var mixed = [Int16](repeating: 0, count: n)
                if expectsSystem && expectsMic {
                    for i in 0..<n { mixed[i] = saturateMix(systemFrames[i], micFrames[i]) }
                } else if expectsSystem {
                    for i in 0..<n { mixed[i] = systemFrames[i] }
                } else {
                    for i in 0..<n { mixed[i] = micFrames[i] }
                }
                systemFrames.removeAll()
                micFrames.removeAll()
                writeFrames(mixed)
            }
        }
    }
}

// MARK: - System audio capture (ScreenCaptureKit)

final class SystemCapture: NSObject, SCStreamDelegate, SCStreamOutput, @unchecked Sendable {

    private let mixer: Mixer
    private var stream: SCStream?
    private var converter: AVAudioConverter?
    private var sourceFormat: AVAudioFormat?
    private let scratchQueue = DispatchQueue(label: "aloe.system.scratch",
                                             qos: .userInteractive)
    private(set) var buffersReceived: Int = 0

    init(mixer: Mixer) {
        self.mixer = mixer
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            throw NSError(domain: "aloe", code: 11,
                          userInfo: [NSLocalizedDescriptionKey: "No display available"])
        }
        let filter = SCContentFilter(display: display,
                                     excludingApplications: [],
                                     exceptingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = 48_000
        config.channelCount = 2
        config.excludesCurrentProcessAudio = true
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 6

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: scratchQueue)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: scratchQueue)
        try await stream.startCapture()
        self.stream = stream
        eprint("System capture started")
    }

    func stop() async {
        if let stream = stream {
            do { try await stream.stopCapture() }
            catch { eprint("system stopCapture error: \(error.localizedDescription)") }
            self.stream = nil
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        eprint("System stream stopped with error: \(error.localizedDescription)")
        Foundation.exit(1)
    }

    func stream(_ stream: SCStream,
                didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        buffersReceived += 1
        do {
            try sampleBuffer.withAudioBufferList { audioBufferList, _ in
                guard let formatDesc = sampleBuffer.formatDescription,
                      let asbdPtr = formatDesc.audioStreamBasicDescription else { return }

                if sourceFormat == nil {
                    var asbd = asbdPtr
                    sourceFormat = AVAudioFormat(streamDescription: &asbd)
                    if let src = sourceFormat {
                        converter = AVAudioConverter(from: src, to: mixer.format)
                        eprint("System source format: \(src.sampleRate) Hz, \(src.channelCount) ch")
                    }
                }
                guard let src = sourceFormat, let converter = converter else { return }
                guard let input = AVAudioPCMBuffer(pcmFormat: src,
                                                   bufferListNoCopy: audioBufferList.unsafePointer)
                else { return }

                let inFrames = input.frameLength
                if inFrames == 0 { return }

                let ratio = mixer.format.sampleRate / src.sampleRate
                let outCap = AVAudioFrameCount(ceil(Double(inFrames) * ratio)) + 1024
                guard let outBuf = AVAudioPCMBuffer(pcmFormat: mixer.format,
                                                    frameCapacity: outCap) else { return }

                var err: NSError?
                var consumed = false
                let status = converter.convert(to: outBuf, error: &err) { _, outStatus in
                    if consumed { outStatus.pointee = .noDataNow; return nil }
                    consumed = true
                    outStatus.pointee = .haveData
                    return input
                }
                if status == .error {
                    eprint("System converter error: \(err?.localizedDescription ?? "?")")
                    return
                }
                if outBuf.frameLength == 0 { return }
                guard let ch = outBuf.int16ChannelData?[0] else { return }
                let n = Int(outBuf.frameLength)
                let samples = Array(UnsafeBufferPointer(start: ch, count: n))
                mixer.feedSystem(samples)
            }
        } catch {
            eprint("system withAudioBufferList error: \(error.localizedDescription)")
        }
    }
}

// MARK: - Mic capture (ffmpeg subprocess)
//
// We deliberately use ffmpeg instead of AVAudioEngine here because plain CLI
// binaries on recent macOS have an AVAudioEngine quirk where the input tap
// silently never fires, even with Microphone permission granted and the device
// override succeeding. ffmpeg's avfoundation input driver works reliably from
// a CLI context and matches what the existing aloe-scribe recorder/meter use.

final class MicCapture: @unchecked Sendable {
    private let mixer: Mixer
    private let deviceName: String  // e.g. "Jabra SPEAK 510 USB"
    private var process: Process?
    private let readQueue = DispatchQueue(label: "aloe.mic.read", qos: .userInteractive)
    private var stopFlag = false
    private(set) var buffersReceived: Int = 0

    init(mixer: Mixer, deviceName: String) {
        self.mixer = mixer
        self.deviceName = deviceName
    }

    static func findFFmpeg() -> String? {
        for path in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"] {
            if FileManager.default.isExecutableFile(atPath: path) { return path }
        }
        return nil
    }

    /// Run ffmpeg -list_devices and find the avfoundation index whose name
    /// contains `target` (case-insensitive).
    static func resolveAVFoundationIndex(name target: String, ffmpegPath: String) -> Int? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: ffmpegPath)
        p.arguments = ["-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
        let errPipe = Pipe()
        p.standardError = errPipe
        p.standardOutput = Pipe()
        do { try p.run() } catch { return nil }
        let data = errPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard let text = String(data: data, encoding: .utf8) else { return nil }

        var inAudio = false
        let targetLower = target.lowercased()
        for line in text.split(separator: "\n") {
            let s = String(line)
            if s.contains("AVFoundation audio devices:") {
                inAudio = true
                continue
            }
            if !inAudio { continue }
            // Lines look like: [AVFoundation indev @ ...] [N] Device Name
            // Extract the "[N] Name" portion after the prefix.
            guard let lbr = s.range(of: "] [", options: .literal) else { continue }
            let rest = s[lbr.upperBound...]
            guard let rbr = rest.range(of: "]", options: .literal) else { continue }
            let idxStr = rest[..<rbr.lowerBound]
            let name = rest[rbr.upperBound...].trimmingCharacters(in: .whitespaces)
            if name.lowercased().contains(targetLower), let idx = Int(idxStr) {
                return idx
            }
        }
        return nil
    }

    func start() throws {
        eprint("MicCapture.start() entered")
        guard let ffmpeg = MicCapture.findFFmpeg() else {
            throw NSError(domain: "aloe", code: 40, userInfo: [NSLocalizedDescriptionKey:
                "ffmpeg not found. Install with: brew install ffmpeg"])
        }
        guard let idx = MicCapture.resolveAVFoundationIndex(name: deviceName, ffmpegPath: ffmpeg) else {
            throw NSError(domain: "aloe", code: 41, userInfo: [NSLocalizedDescriptionKey:
                "Mic device '\(deviceName)' not found via avfoundation"])
        }
        eprint("Mic device: \(deviceName) → avfoundation index :\(idx)")

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: ffmpeg)
        proc.arguments = [
            "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation",
            "-i", ":\(idx)",
            "-ar", "\(Int(mixer.format.sampleRate))",
            "-ac", "1",
            "-f", "s16le",
            "-",
        ]
        let stdoutPipe = Pipe()
        proc.standardOutput = stdoutPipe
        proc.standardError = FileHandle.standardError  // pass through ffmpeg errors
        try proc.run()
        self.process = proc
        eprint("Mic ffmpeg started (PID \(proc.processIdentifier))")

        let fd = stdoutPipe.fileHandleForReading.fileDescriptor
        readQueue.async { [weak self] in
            self?.readLoop(fd: fd)
        }
    }

    private func readLoop(fd: Int32) {
        // ~50 ms at 16 kHz mono int16 = 800 frames = 1600 bytes per chunk.
        let chunkBytes = 1600
        var buffer = [UInt8](repeating: 0, count: chunkBytes)
        while !stopFlag {
            let n = buffer.withUnsafeMutableBufferPointer { ptr -> Int in
                Darwin.read(fd, ptr.baseAddress, ptr.count)
            }
            if n <= 0 { break }
            let frames = n / 2
            if frames == 0 { continue }
            var samples = [Int16](repeating: 0, count: frames)
            samples.withUnsafeMutableBufferPointer { sptr in
                buffer.withUnsafeBufferPointer { bptr in
                    _ = memcpy(sptr.baseAddress, bptr.baseAddress, frames * 2)
                }
            }
            buffersReceived += 1
            if buffersReceived == 1 {
                eprint("MicCapture: first chunk received (\(frames) frames)")
            }
            mixer.feedMic(samples)
        }
        eprint("Mic read loop exited")
    }

    func stop() {
        stopFlag = true
        if let proc = process {
            proc.terminate()
            proc.waitUntilExit()
            self.process = nil
        }
    }
}

// MARK: - Main

let args = parseArgs()
let mixer: Mixer
do {
    mixer = try Mixer(outputPath: args.output,
                      sampleRate: args.sampleRate,
                      channels: args.channels,
                      expectsSystem: args.captureSystem,
                      expectsMic: args.micDeviceName != nil)
} catch {
    eprint("Failed to open output \(args.output): \(error.localizedDescription)")
    exit(1)
}

let systemCapture: SystemCapture? = args.captureSystem ? SystemCapture(mixer: mixer) : nil
let micCapture: MicCapture? = (args.micDeviceName.map { name in
    MicCapture(mixer: mixer, deviceName: name)
})

final class AtomicFlag: @unchecked Sendable {
    private var flag = false
    private let lock = NSLock()
    func set() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if flag { return false }
        flag = true
        return true
    }
}
let stopOnce = AtomicFlag()

func requestStop(reason: String) {
    guard stopOnce.set() else { return }
    eprint("Stopping (\(reason))…")
    Task {
        if let s = systemCapture { await s.stop() }
        micCapture?.stop()
        mixer.finalize()
        eprint("Capture stopped — system buffers: \(systemCapture?.buffersReceived ?? 0), mic buffers: \(micCapture?.buffersReceived ?? 0), frames written: \(mixer.framesWritten)")
        Foundation.exit(0)
    }
}

// Signal handlers
let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigintSource.setEventHandler { requestStop(reason: "SIGINT") }
sigintSource.resume()
signal(SIGINT, SIG_IGN)

let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigtermSource.setEventHandler { requestStop(reason: "SIGTERM") }
sigtermSource.resume()
signal(SIGTERM, SIG_IGN)

// Stdin watcher
let stdinSource = DispatchSource.makeReadSource(fileDescriptor: 0,
                                                queue: .global(qos: .utility))
stdinSource.setEventHandler {
    var buf = [UInt8](repeating: 0, count: 256)
    let n = buf.withUnsafeMutableBufferPointer { ptr -> Int in
        Darwin.read(0, ptr.baseAddress, ptr.count)
    }
    if n <= 0 {
        requestStop(reason: n == 0 ? "stdin EOF" : "stdin read error")
        stdinSource.cancel()
        return
    }
    for i in 0..<n where buf[i] == UInt8(ascii: "q") {
        requestStop(reason: "'q' on stdin")
        stdinSource.cancel()
        return
    }
}
stdinSource.resume()

Task {
    do {
        if let s = systemCapture { try await s.start() }
        if let m = micCapture {
            try m.start()
        }
        eprint("All sources started → \(args.output)")
    } catch {
        let ns = error as NSError
        eprint("Start failed: \(ns.localizedDescription)")
        if ns.domain.contains("SCStreamErrorDomain") || ns.localizedDescription.contains("permission") {
            eprint("Grant access in System Settings → Privacy & Security → Screen Recording, then retry.")
        }
        Foundation.exit(1)
    }
}

RunLoop.main.run()
