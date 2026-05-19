// aloe-audio-capture — System audio capture for Aloe Scribe via ScreenCaptureKit.
//
// Captures the desktop's system audio output (no BlackHole, no Multi-Output
// Device required) and writes a 16-bit PCM WAV file. Phase-1 scope: system
// audio only. Mic mixing is added in Phase 2.
//
// Usage:
//   aloe-audio-capture --output recording.wav [--sample-rate 16000] [--channels 1]
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

// MARK: - CLI parsing

struct Args {
    let output: String
    let sampleRate: Double
    let channels: AVAudioChannelCount
}

func parseArgs() -> Args {
    var output: String?
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
        case "--sample-rate", "-r":
            i += 1
            guard i < argv.count, let v = Double(argv[i]) else {
                eprint("--sample-rate requires a number")
                exit(2)
            }
            sampleRate = v
        case "--channels", "-c":
            i += 1
            guard i < argv.count, let v = UInt32(argv[i]) else {
                eprint("--channels requires a number")
                exit(2)
            }
            channels = AVAudioChannelCount(v)
        case "--help", "-h":
            print("""
            Usage: aloe-audio-capture --output <path> [--sample-rate 16000] [--channels 1]

            Captures macOS system audio via ScreenCaptureKit and writes a WAV file.
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
    return Args(output: output, sampleRate: sampleRate, channels: channels)
}

// MARK: - Capture

final class Capture: NSObject, SCStreamDelegate, SCStreamOutput, @unchecked Sendable {

    private let outputPath: String
    private let outputSampleRate: Double
    private let outputChannels: AVAudioChannelCount

    private var stream: SCStream?
    private var audioFile: AVAudioFile?
    private var converter: AVAudioConverter?
    private var sourceFormat: AVAudioFormat?
    private var outputFormat: AVAudioFormat?

    // Serialize all file writes & format setup so we can stop cleanly.
    private let writerQueue = DispatchQueue(label: "aloe.audio.writer", qos: .userInteractive)

    // Diagnostics
    private var audioBuffersReceived: Int = 0
    private var framesWritten: AVAudioFramePosition = 0

    init(args: Args) {
        self.outputPath = args.output
        self.outputSampleRate = args.sampleRate
        self.outputChannels = args.channels
    }

    func start() async throws {
        // Output format: 16-bit signed PCM, interleaved, target sample rate.
        guard let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                         sampleRate: outputSampleRate,
                                         channels: outputChannels,
                                         interleaved: true) else {
            throw NSError(domain: "aloe", code: 10,
                          userInfo: [NSLocalizedDescriptionKey: "Could not build output AVAudioFormat"])
        }
        self.outputFormat = outFmt

        // Open the destination WAV. AVAudioFile writes a valid header on init
        // and finalizes the size fields when the instance is deinit-ed.
        let url = URL(fileURLWithPath: outputPath)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: outputSampleRate,
            AVNumberOfChannelsKey: outputChannels,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        self.audioFile = try AVAudioFile(forWriting: url,
                                         settings: settings,
                                         commonFormat: .pcmFormatInt16,
                                         interleaved: true)

        // Discover a display to attach to. SCStream requires *some* content
        // filter even for audio-only capture.
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
        config.sampleRate = 48_000           // SCK's native source rate
        config.channelCount = 2               // stereo from the system mix
        config.excludesCurrentProcessAudio = true
        // We must request *some* screen output for SCStream to start cleanly.
        // Keep it tiny and slow to minimize CPU.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)  // 1 fps
        config.queueDepth = 6

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: writerQueue)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: writerQueue)
        try await stream.startCapture()
        self.stream = stream
        eprint("Capture started → \(outputPath)")
    }

    func stop() async {
        if let stream = stream {
            do {
                try await stream.stopCapture()
            } catch {
                eprint("stopCapture error: \(error.localizedDescription)")
            }
            self.stream = nil
        }
        // Drop the AVAudioFile reference so its deinit finalizes the WAV
        // header. We do this on the writer queue so it can't race a pending
        // sample-buffer callback that's still mid-write.
        writerQueue.sync {
            self.audioFile = nil
        }
        eprint("Capture stopped — audio buffers received: \(audioBuffersReceived), frames written: \(framesWritten)")
    }

    // MARK: SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        eprint("Stream stopped with error: \(error.localizedDescription)")
        // Try to finalize the WAV before bailing.
        writerQueue.sync {
            self.audioFile = nil
        }
        Foundation.exit(1)
    }

    // MARK: SCStreamOutput

    func stream(_ stream: SCStream,
                didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        // Discard screen frames — we only requested them to keep SCStream happy.
        guard type == .audio else { return }
        guard sampleBuffer.isValid else { return }
        guard let outputFormat = outputFormat else { return }

        audioBuffersReceived += 1

        do {
            try sampleBuffer.withAudioBufferList { audioBufferList, _ in
                guard let formatDesc = sampleBuffer.formatDescription,
                      let asbdPtr = formatDesc.audioStreamBasicDescription else { return }

                // Build the source AVAudioFormat from the first buffer we see.
                if sourceFormat == nil {
                    var asbd = asbdPtr  // mutable copy for the initializer
                    sourceFormat = AVAudioFormat(streamDescription: &asbd)
                    if let src = sourceFormat {
                        converter = AVAudioConverter(from: src, to: outputFormat)
                        if converter == nil {
                            eprint("AVAudioConverter init failed: \(src) → \(outputFormat)")
                        } else {
                            eprint("Source format: \(src.sampleRate) Hz, \(src.channelCount) ch")
                        }
                    }
                }

                guard let src = sourceFormat, let converter = converter else { return }

                guard let inputBuffer = AVAudioPCMBuffer(pcmFormat: src,
                                                         bufferListNoCopy: audioBufferList.unsafePointer)
                else { return }

                let inFrames = inputBuffer.frameLength
                if inFrames == 0 { return }

                // Output capacity: ceil(inFrames * outRate / srcRate) plus a little slack.
                let ratio = outputFormat.sampleRate / src.sampleRate
                let outFrameCapacity = AVAudioFrameCount(ceil(Double(inFrames) * ratio)) + 1024

                guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: outputFormat,
                                                          frameCapacity: outFrameCapacity)
                else { return }

                var error: NSError?
                var consumed = false
                // Per-call lifecycle: feed the buffer once, then say "no data
                // now" — NOT .endOfStream. .endOfStream permanently terminates
                // the converter, so subsequent buffers would produce zero
                // output frames forever.
                let status = converter.convert(to: outputBuffer, error: &error) { _, outStatus in
                    if consumed {
                        outStatus.pointee = .noDataNow
                        return nil
                    }
                    consumed = true
                    outStatus.pointee = .haveData
                    return inputBuffer
                }

                if status == .error {
                    eprint("Converter error: \(error?.localizedDescription ?? "unknown")")
                    return
                }
                if outputBuffer.frameLength == 0 { return }

                guard let audioFile = audioFile else { return }
                do {
                    try audioFile.write(from: outputBuffer)
                    framesWritten += AVAudioFramePosition(outputBuffer.frameLength)
                } catch {
                    eprint("Write error: \(error.localizedDescription)")
                }
            }
        } catch {
            eprint("withAudioBufferList error: \(error.localizedDescription)")
        }
    }
}

// MARK: - Main

let args = parseArgs()
let capture = Capture(args: args)

// We use a single shared stop path: gather sources of stop into one place so
// we don't race startCapture / stopCapture.
let stopOnce = AtomicFlag()

func requestStop(reason: String) {
    guard stopOnce.set() else { return }
    eprint("Stopping (\(reason))…")
    Task {
        await capture.stop()
        Foundation.exit(0)
    }
}

final class AtomicFlag: @unchecked Sendable {
    private var flag = false
    private let lock = NSLock()
    /// Returns true the first time, false thereafter.
    func set() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if flag { return false }
        flag = true
        return true
    }
}

// Signal handlers — set before async start so even a fast Ctrl-C is honored.
let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigintSource.setEventHandler { requestStop(reason: "SIGINT") }
sigintSource.resume()
signal(SIGINT, SIG_IGN)  // dispatch source needs default signal ignored

let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigtermSource.setEventHandler { requestStop(reason: "SIGTERM") }
sigtermSource.resume()
signal(SIGTERM, SIG_IGN)

// Stdin watcher — 'q' on stdin or actual EOF stops the capture. Uses a
// DispatchSource so the handler only fires when fd 0 is genuinely readable
// (i.e. has data buffered) or has hit EOF — `FileHandle.availableData` can
// return empty Data on a still-open pipe and falsely report EOF.
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
        try await capture.start()
    } catch {
        let ns = error as NSError
        eprint("Start failed: \(ns.localizedDescription)")
        // SCK throws with code -3801 / -3802 when the user hasn't granted
        // Screen Recording permission. Surface a clear hint for either.
        if ns.domain.contains("SCStreamErrorDomain") || ns.localizedDescription.contains("permission") {
            eprint("Grant access in System Settings → Privacy & Security → Screen Recording, then retry.")
        }
        Foundation.exit(1)
    }
}

RunLoop.main.run()
