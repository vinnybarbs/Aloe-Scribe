// aloe-audio-capture — Mic + system-audio capture for Aloe Scribe.
//
// System audio is tapped via ScreenCaptureKit (no BlackHole, no Multi-Output
// Device required). Mic audio is captured via AVAudioEngine, with the device
// chosen by name (the same name avfoundation/CoreAudio exposes).
//
// Both streams are downsampled to 16 kHz mono Int16 and sample-aligned inside
// this binary. Default output is a single mixed mono WAV; with --split (and
// both sources active) the output is instead a stereo WAV with the mic on
// channel 0 and system audio on channel 1, so downstream speaker attribution
// can tell the local side from the remote side.
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
import CoreAudio

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

// MARK: - Passive input listing (CoreAudio, no AVFoundation discovery)

func listCoreAudioInputNames() -> [String] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size
    ) == noErr else { return [] }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    var ids = [AudioObjectID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &ids
    ) == noErr else { return [] }

    var names: [String] = []
    for id in ids {
        // Input-capable devices only: check for input-scope streams.
        var streamsAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreams,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain)
        var streamsSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &streamsAddr, 0, nil, &streamsSize) == noErr,
              streamsSize > 0 else { continue }

        // Skip Continuity iPhone/iPad mics by TRANSPORT TYPE — custom phone
        // names ("VMoney Microphone") defeat any name-based filter, and a
        // meeting recorder should never touch a phone mic.
        var transportAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyTransportType,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var transport: UInt32 = 0
        var transportSize = UInt32(MemoryLayout<UInt32>.size)
        if AudioObjectGetPropertyData(id, &transportAddr, 0, nil, &transportSize, &transport) == noErr {
            if transport == kAudioDeviceTransportTypeContinuityCaptureWired
                || transport == kAudioDeviceTransportTypeContinuityCaptureWireless {
                continue
            }
        }

        var nameAddr = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var cfName: Unmanaged<CFString>?
        var nameSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(id, &nameAddr, 0, nil, &nameSize, &cfName) == noErr,
              let name = cfName?.takeRetainedValue() as String? else { continue }
        names.append(name)
    }
    return names
}

// MARK: - CLI parsing

struct Args {
    let output: String?              // nil only in --meter mode (PCM → stdout)
    let micDeviceName: String?       // nil = no mic capture
    let captureSystem: Bool          // --no-system disables system capture
    let sampleRate: Double
    let channels: AVAudioChannelCount
    let meterMode: Bool              // --meter: stream SCK PCM to stdout, no WAV
    let splitChannels: Bool          // --split: stereo WAV, ch0 = mic, ch1 = system
}

func parseArgs() -> Args {
    var output: String?
    var micDeviceName: String?
    var captureSystem = true
    var sampleRate: Double = 16_000
    var channels: AVAudioChannelCount = 1
    var meterMode = false
    var splitChannels = false

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
        case "--split":
            splitChannels = true
        case "--meter":
            meterMode = true
        case "--list-inputs":
            // Passive CoreAudio listing: prints input device names WITHOUT
            // instantiating AVFoundation discovery, which wakes Continuity
            // iPhone mics (the phone chimes in the user's pocket just from
            // being enumerated).
            for name in listCoreAudioInputNames() {
                print(name)
            }
            exit(0)
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
                                      [--split] [--sample-rate 16000] [--channels 1]
                   aloe-audio-capture --meter [--sample-rate 16000]

            Record mode (default): mix system audio (SCK) + optional mic into a WAV.
            Split mode (--split):   requires both sources; writes a stereo WAV with
                                    the mic on channel 0 and system audio on
                                    channel 1 (sample-aligned, not mixed) so
                                    downstream tools can attribute speakers.
            Meter mode (--meter):   stream raw s16le mono PCM of system audio to
                                    stdout for a live UI level bar.

            Stops on 'q' on stdin, EOF on stdin, SIGINT, or SIGTERM.
            """)
            exit(0)
        default:
            eprint("Unknown argument: \(arg)")
            exit(2)
        }
        i += 1
    }

    if meterMode {
        return Args(output: nil,
                    micDeviceName: nil,
                    captureSystem: true,
                    sampleRate: sampleRate,
                    channels: 1,
                    meterMode: true,
                    splitChannels: false)
    }
    guard let output = output else {
        eprint("Error: --output is required (or pass --meter for level-only mode)")
        exit(2)
    }
    if !captureSystem && micDeviceName == nil {
        eprint("Error: must capture at least one source (use --mic or drop --no-system)")
        exit(2)
    }
    if splitChannels && (!captureSystem || micDeviceName == nil) {
        eprint("--split needs both mic and system audio; falling back to single-source mono")
        splitChannels = false
    }
    return Args(output: output,
                micDeviceName: micDeviceName,
                captureSystem: captureSystem,
                sampleRate: sampleRate,
                channels: channels,
                meterMode: false,
                splitChannels: splitChannels)
}

// MARK: - WAV writer
//
// We write the WAV by hand instead of going through AVAudioFile because
// AVAudioFile inserts JUNK and FLLR padding chunks between `fmt ` and `data`
// for 4 KB alignment. ffmpeg/ffprobe handle them, whisper.cpp's WAV reader
// does not and fails with "failed to read the frames of the audio data".

final class WavWriter {
    private let handle: FileHandle
    private let sampleRate: UInt32
    private let channels: UInt16
    private let bitsPerSample: UInt16 = 16
    private(set) var dataBytesWritten: UInt32 = 0
    private var finalized = false

    init(url: URL, sampleRate: Double, channels: AVAudioChannelCount) throws {
        self.sampleRate = UInt32(sampleRate)
        self.channels = UInt16(channels)
        FileManager.default.createFile(atPath: url.path, contents: nil)
        self.handle = try FileHandle(forWritingTo: url)
        try writeHeader(dataSize: 0)
    }

    func write(_ samples: [Int16]) throws {
        let bytes = samples.withUnsafeBytes { Data($0) }
        try handle.write(contentsOf: bytes)
        dataBytesWritten &+= UInt32(bytes.count)
    }

    func finalize() throws {
        if finalized { return }
        finalized = true
        try handle.seek(toOffset: 0)
        try writeHeader(dataSize: dataBytesWritten)
        try handle.close()
    }

    private func writeHeader(dataSize: UInt32) throws {
        let byteRate = sampleRate * UInt32(channels) * UInt32(bitsPerSample / 8)
        let blockAlign = channels * (bitsPerSample / 8)
        let riffSize = 36 &+ dataSize

        var h = Data()
        h.append(contentsOf: Array("RIFF".utf8))
        h.append(le(riffSize))
        h.append(contentsOf: Array("WAVE".utf8))
        h.append(contentsOf: Array("fmt ".utf8))
        h.append(le(UInt32(16)))           // PCM fmt chunk size
        h.append(le(UInt16(1)))            // format = 1 (PCM)
        h.append(le(channels))
        h.append(le(sampleRate))
        h.append(le(byteRate))
        h.append(le(blockAlign))
        h.append(le(bitsPerSample))
        h.append(contentsOf: Array("data".utf8))
        h.append(le(dataSize))
        try handle.write(contentsOf: h)
    }

    private func le<T: FixedWidthInteger>(_ v: T) -> Data {
        var x = v.littleEndian
        return Data(bytes: &x, count: MemoryLayout<T>.size)
    }
}

// MARK: - Mixer

final class Mixer: @unchecked Sendable {
    private let outputFormat: AVAudioFormat
    private let wavWriter: WavWriter
    private let expectsSystem: Bool
    private let expectsMic: Bool
    private let split: Bool          // stereo out: ch0 = mic, ch1 = system

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
         expectsMic: Bool,
         split: Bool = false) throws {
        // Sources always feed mono streams; `split` only changes how they are
        // written (interleaved stereo instead of summed mono).
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
        self.split = split && expectsSystem && expectsMic

        let url = URL(fileURLWithPath: outputPath)
        self.wavWriter = try WavWriter(url: url,
                                       sampleRate: sampleRate,
                                       channels: self.split ? 2 : channels)
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

        var mixed: [Int16]
        if split {
            // Interleave: frame i = [mic, system].
            mixed = [Int16](repeating: 0, count: n * 2)
            for i in 0..<n {
                mixed[i * 2] = micFrames[i]
                mixed[i * 2 + 1] = systemFrames[i]
            }
        } else if expectsSystem && expectsMic {
            mixed = [Int16](repeating: 0, count: n)
            for i in 0..<n {
                mixed[i] = saturateMix(systemFrames[i], micFrames[i])
            }
        } else if expectsSystem {
            mixed = Array(systemFrames[0..<n])
        } else {
            mixed = Array(micFrames[0..<n])
        }

        // Drop the consumed prefixes.
        if expectsSystem { systemFrames.removeFirst(min(n, systemFrames.count)) }
        if expectsMic    { micFrames.removeFirst(min(n, micFrames.count)) }

        writeFrames(mixed)
    }

    private func writeFrames(_ samples: [Int16]) {
        let frames = samples.count / (split ? 2 : 1)
        // Diagnostic: report peak amplitude per write (kept once per ~1 sec).
        let priorSec = framesWritten / 16000
        let nextSec = (framesWritten + frames) / 16000
        if framesWritten == 0 || priorSec != nextSec {
            var peak: Int32 = 0
            for s in samples {
                let a = Int32(s).magnitude
                if Int32(a) > peak { peak = Int32(a) }
            }
            eprint("Mixer: write \(samples.count) frames, peak=\(peak)")
        }
        do {
            try wavWriter.write(samples)
            framesWritten += frames
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
                var mixed: [Int16]
                if split {
                    mixed = [Int16](repeating: 0, count: n * 2)
                    for i in 0..<n {
                        mixed[i * 2] = micFrames[i]
                        mixed[i * 2 + 1] = systemFrames[i]
                    }
                } else if expectsSystem && expectsMic {
                    mixed = [Int16](repeating: 0, count: n)
                    for i in 0..<n { mixed[i] = saturateMix(systemFrames[i], micFrames[i]) }
                } else if expectsSystem {
                    mixed = Array(systemFrames[0..<n])
                } else {
                    mixed = Array(micFrames[0..<n])
                }
                systemFrames.removeAll()
                micFrames.removeAll()
                writeFrames(mixed)
            }
            // Patch the WAV header with the actual data size & close the file.
            do {
                try wavWriter.finalize()
            } catch {
                eprint("Mixer finalize error: \(error.localizedDescription)")
            }
        }
    }
}

// MARK: - System audio capture (ScreenCaptureKit)

final class SystemCapture: NSObject, SCStreamDelegate, SCStreamOutput, @unchecked Sendable {

    typealias SampleSink = ([Int16]) -> Void

    private let targetFormat: AVAudioFormat
    private let sink: SampleSink
    private var stream: SCStream?
    private var converter: AVAudioConverter?
    private var sourceFormat: AVAudioFormat?
    private let scratchQueue = DispatchQueue(label: "aloe.system.scratch",
                                             qos: .userInteractive)
    private(set) var buffersReceived: Int = 0

    init(targetFormat: AVAudioFormat, sink: @escaping SampleSink) {
        self.targetFormat = targetFormat
        self.sink = sink
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
                        converter = AVAudioConverter(from: src, to: targetFormat)
                        eprint("System source format: \(src.sampleRate) Hz, \(src.channelCount) ch")
                    }
                }
                guard let src = sourceFormat, let converter = converter else { return }
                guard let input = AVAudioPCMBuffer(pcmFormat: src,
                                                   bufferListNoCopy: audioBufferList.unsafePointer)
                else { return }

                let inFrames = input.frameLength
                if inFrames == 0 { return }

                let ratio = targetFormat.sampleRate / src.sampleRate
                let outCap = AVAudioFrameCount(ceil(Double(inFrames) * ratio)) + 1024
                guard let outBuf = AVAudioPCMBuffer(pcmFormat: targetFormat,
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
                sink(samples)
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
    private var engine: AVAudioEngine?
    private var vpioPeak: Int16 = 0

    init(mixer: Mixer, deviceName: String) {
        self.mixer = mixer
        self.deviceName = deviceName
    }

    /// True when the default input is the machine's BUILT-IN mic. Voice
    /// processing is only safe there: Bluetooth and USB inputs (AirPods,
    /// Jabra) delivered all-zero audio through VPIO in the field, and those
    /// devices carry their own hardware echo cancellation anyway.
    static func defaultInputIsBuiltIn() -> Bool {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var devID = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                         &addr, 0, nil, &size, &devID) == noErr,
              devID != kAudioObjectUnknown else { return false }
        var tAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyTransportType,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var transport = UInt32(0)
        var tSize = UInt32(MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(devID, &tAddr, 0, nil, &tSize, &transport) == noErr
        else { return false }
        return transport == kAudioDeviceTransportTypeBuiltIn
    }

    /// AudioDeviceID for an input device by exact name (case-insensitive),
    /// skipping Continuity devices — passive CoreAudio, no discovery chime.
    static func findInputDeviceID(name: String) -> AudioDeviceID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size
        ) == noErr else { return nil }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &ids
        ) == noErr else { return nil }
        for id in ids {
            var streamsAddr = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyStreams,
                mScope: kAudioObjectPropertyScopeInput,
                mElement: kAudioObjectPropertyElementMain)
            var streamsSize: UInt32 = 0
            guard AudioObjectGetPropertyDataSize(id, &streamsAddr, 0, nil, &streamsSize) == noErr,
                  streamsSize > 0 else { continue }
            var transportAddr = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyTransportType,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            var transport: UInt32 = 0
            var tSize = UInt32(MemoryLayout<UInt32>.size)
            if AudioObjectGetPropertyData(id, &transportAddr, 0, nil, &tSize, &transport) == noErr,
               transport == kAudioDeviceTransportTypeContinuityCaptureWired
                || transport == kAudioDeviceTransportTypeContinuityCaptureWireless {
                continue
            }
            var nameAddr = AudioObjectPropertyAddress(
                mSelector: kAudioObjectPropertyName,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            var cfName: Unmanaged<CFString>?
            var nameSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
            guard AudioObjectGetPropertyData(id, &nameAddr, 0, nil, &nameSize, &cfName) == noErr,
                  let n = cfName?.takeRetainedValue() as String? else { continue }
            if n.lowercased() == name.lowercased() { return id }
        }
        return nil
    }

    static func defaultInputDeviceName() -> String? {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var devID = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                         &addr, 0, nil, &size, &devID) == noErr,
              devID != kAudioObjectUnknown else { return nil }
        var nameAddr = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var name: Unmanaged<CFString>?
        var nsize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(devID, &nameAddr, 0, nil, &nsize, &name) == noErr,
              let cf = name?.takeRetainedValue() else { return nil }
        return cf as String
    }

    /// Echo-cancelled capture through Apple voice processing. Only used when
    /// the requested mic IS the default input device: voice processing binds
    /// to the default input/output pair, and overriding devices under it is
    /// where the known macOS breakage lives. Verified on this machine: it
    /// cancels audio played by OTHER processes (a Teams call through the
    /// speakers no longer lands on the mic channel).
    private func startVPIO() throws {
        try startEngine(voiceProcessing: true)
    }

    /// Native AVAudioEngine capture WITHOUT voice processing — the primary
    /// mic path. ffmpeg's avfoundation input was measured time-compressing
    /// the mic stream by ~11% (12.3 s between events that were really
    /// 13.8 s apart), which corrupted every mic-channel timestamp.
    private func startRawEngine() throws {
        try startEngine(voiceProcessing: false)
    }

    private func startEngine(voiceProcessing: Bool) throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        if voiceProcessing {
            try input.setVoiceProcessingEnabled(true)
        } else if let devID = MicCapture.findInputDeviceID(name: deviceName) {
            // Bind the requested (non-default) device. Failure falls back
            // to the default input rather than aborting the recording.
            do { try input.auAudioUnit.setDeviceID(devID) }
            catch { eprint("Mic device bind failed (\(error)) — using default input") }
        }
        if #available(macOS 14.0, *), voiceProcessing {
            // Never duck other apps' audio: the meeting itself plays there.
            input.voiceProcessingOtherAudioDuckingConfiguration =
                AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                    enableAdvancedDucking: false, duckingLevel: .min)
        }
        let inFmt = input.outputFormat(forBus: 0)
        guard let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                          sampleRate: mixer.format.sampleRate,
                                          channels: 1,
                                          interleaved: true),
              let converter = AVAudioConverter(from: inFmt, to: outFmt) else {
            throw NSError(domain: "aloe", code: 41, userInfo: [
                NSLocalizedDescriptionKey: "Could not build VPIO converter"])
        }
        let ratio = outFmt.sampleRate / inFmt.sampleRate
        input.installTap(onBus: 0, bufferSize: 4096, format: inFmt) { [weak self] buf, _ in
            guard let self = self, !self.stopFlag else { return }
            let cap = AVAudioFrameCount(Double(buf.frameLength) * ratio) + 16
            guard let out = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return }
            var fed = false
            var err: NSError?
            converter.convert(to: out, error: &err) { _, status in
                if fed { status.pointee = .noDataNow; return nil }
                fed = true
                status.pointee = .haveData
                return buf
            }
            let n = Int(out.frameLength)
            guard n > 0, let ch = out.int16ChannelData?[0] else { return }
            let samples = Array(UnsafeBufferPointer(start: ch, count: n))
            for s in samples where abs(Int32(s)) > Int32(self.vpioPeak) {
                self.vpioPeak = Int16(min(Int32(Int16.max), abs(Int32(s))))
            }
            self.buffersReceived += 1
            if self.buffersReceived == 1 {
                eprint("MicCapture(VPIO): first buffer received (\(n) frames)")
            }
            self.mixer.feedMic(samples)
        }
        engine.prepare()
        try engine.start()
        self.engine = engine
        eprint(voiceProcessing
               ? "Mic: echo-cancelled capture via Apple voice processing "
                 + "(tap \(Int(inFmt.sampleRate)) Hz, \(inFmt.channelCount) ch)"
               : "Mic: native capture (tap \(Int(inFmt.sampleRate)) Hz, "
                 + "\(inFmt.channelCount) ch)")
    }

    private func stopVPIO() {
        if let e = engine {
            e.inputNode.removeTap(onBus: 0)
            e.stop()
            engine = nil
        }
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
        // Prefer Apple voice processing (echo cancellation) when the chosen
        // mic is the system default input. ALOE_NO_AEC=1 forces raw capture.
        // Voice processing is OPT-IN (ALOE_AEC=1). Engaging it while a
        // conferencing app is live on the same mic put two voice-processing
        // chains on one device: ours delivered pure zeros and the user's
        // call audio was disturbed. Until we can detect an active call,
        // passive native capture is the default.
        if ProcessInfo.processInfo.environment["ALOE_AEC"] == "1",
           ProcessInfo.processInfo.environment["ALOE_NO_AEC"] == nil,
           MicCapture.defaultInputIsBuiltIn(),
           let defName = MicCapture.defaultInputDeviceName(),
           defName.lowercased() == deviceName.lowercased() {
            do {
                try startVPIO()
                // If voice processing yields no audio (quirky device, odd
                // aggregate), fall back to raw ffmpeg capture automatically.
                // Silence is NOT proof of failure here: perfect echo
                // cancellation plus noise suppression legitimately outputs
                // all-zero audio while the user listens without talking.
                // Judge over a long window with periodic logging, and only
                // abandon VPIO when no buffers arrive at all (a real stall)
                // or half a minute passes with literal digital silence.
                func check(_ elapsed: Int) {
                    DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 5.0) { [weak self] in
                        guard let self = self, !self.stopFlag else { return }
                        if self.buffersReceived == 0 {
                            eprint("VPIO: no buffers after \(elapsed + 5)s — falling back to raw capture")
                            self.stopVPIO()
                            try? self.startRawEngine()
                            return
                        }
                        if self.vpioPeak > 0 {
                            eprint("VPIO healthy: first nonzero audio within \(elapsed + 5)s (peak \(self.vpioPeak))")
                            return
                        }
                        if elapsed + 5 >= 30 {
                            eprint("VPIO: 30s of digital silence — falling back to raw capture")
                            self.stopVPIO()
                            try? self.startRawEngine()
                            return
                        }
                        eprint("VPIO: silent so far at \(elapsed + 5)s (buffers \(self.buffersReceived)) — could be perfect AEC while the user listens, waiting")
                        check(elapsed + 5)
                    }
                }
                check(0)
                return
            } catch {
                eprint("VPIO unavailable (\(error.localizedDescription)) — raw capture")
            }
        } else {
            eprint("Mic: native capture (voice processing is opt-in via ALOE_AEC=1)")
        }
        do {
            try startRawEngine()
            // ffmpeg remains the last resort if the engine yields nothing.
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 3.0) { [weak self] in
                guard let self = self, !self.stopFlag,
                      self.buffersReceived == 0 else { return }
                eprint("Native capture produced no audio in 3s — ffmpeg fallback")
                self.stopVPIO()
                try? self.startFFmpeg()
            }
        } catch {
            eprint("Native capture unavailable (\(error.localizedDescription)) — ffmpeg fallback")
            try startFFmpeg()
        }
    }

    private func startFFmpeg() throws {
        guard let ffmpeg = MicCapture.findFFmpeg() else {
            throw NSError(domain: "aloe", code: 40, userInfo: [NSLocalizedDescriptionKey:
                "ffmpeg not found. Install with: brew install ffmpeg"])
        }
        // Open by NAME directly. Pre-resolving an index required an
        // avfoundation enumeration, which wakes Continuity iPhone mics (the
        // phone chimes just from being listed). Name matching skips that;
        // index resolution remains as the failure fallback below.
        eprint("Mic device: \(deviceName) (opening by name)")

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: ffmpeg)
        proc.arguments = [
            "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation",
            "-i", ":\(deviceName)",
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

        // Fallback: if name matching failed (ffmpeg exits immediately),
        // resolve the avfoundation index the old way and relaunch. Only this
        // failure path pays the enumeration cost.
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 2.0) { [weak self] in
            guard let self = self, let p = self.process,
                  !p.isRunning, !self.stopFlag else { return }
            eprint("Mic open by name failed — resolving index (fallback)")
            guard let idx = MicCapture.resolveAVFoundationIndex(
                name: self.deviceName, ffmpegPath: ffmpeg
            ) else {
                eprint("Mic device '\(self.deviceName)' not found via avfoundation")
                return
            }
            let retry = Process()
            retry.executableURL = URL(fileURLWithPath: ffmpeg)
            retry.arguments = [
                "-hide_banner", "-loglevel", "error",
                "-f", "avfoundation",
                "-i", ":\(idx)",
                "-ar", "\(Int(self.mixer.format.sampleRate))",
                "-ac", "1",
                "-f", "s16le",
                "-",
            ]
            let retryPipe = Pipe()
            retry.standardOutput = retryPipe
            retry.standardError = FileHandle.standardError
            do { try retry.run() } catch {
                eprint("Mic fallback launch failed: \(error.localizedDescription)")
                return
            }
            self.process = retry
            eprint("Mic ffmpeg restarted via index :\(idx) (PID \(retry.processIdentifier))")
            let retryFD = retryPipe.fileHandleForReading.fileDescriptor
            self.readQueue.async { self.readLoop(fd: retryFD) }
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
        stopVPIO()
        if let proc = process {
            proc.terminate()
            proc.waitUntilExit()
            self.process = nil
        }
    }
}

// MARK: - Main

let args = parseArgs()

// Build the target audio format once; SystemCapture / MicCapture all
// downsample to this.
guard let targetFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                        sampleRate: args.sampleRate,
                                        channels: args.channels,
                                        interleaved: true) else {
    eprint("Failed to build target AVAudioFormat")
    exit(1)
}

// Mixer + Mic exist only in record mode. Meter mode is system-audio-only
// and writes raw PCM straight to stdout for the UI to read.
let mixer: Mixer?
let systemCapture: SystemCapture?
let micCapture: MicCapture?

if args.meterMode {
    mixer = nil
    micCapture = nil
    // SCK → stdout. We use Darwin.write directly so partial writes are
    // handled and the FileHandle isn't fighting macOS over stdout buffering.
    let stdoutFD: Int32 = 1
    systemCapture = SystemCapture(targetFormat: targetFormat) { samples in
        samples.withUnsafeBytes { rawBuf in
            var remaining = rawBuf.count
            var ptr = rawBuf.baseAddress!
            while remaining > 0 {
                let n = Darwin.write(stdoutFD, ptr, remaining)
                if n <= 0 { return }   // pipe closed; consumer went away
                remaining -= n
                ptr = ptr.advanced(by: n)
            }
        }
    }
    eprint("Meter mode — SCK PCM to stdout at \(Int(args.sampleRate)) Hz mono int16")
} else {
    guard let outPath = args.output else {
        eprint("Internal error: record mode without --output")
        exit(2)
    }
    do {
        let m = try Mixer(outputPath: outPath,
                          sampleRate: args.sampleRate,
                          channels: args.channels,
                          expectsSystem: args.captureSystem,
                          expectsMic: args.micDeviceName != nil,
                          split: args.splitChannels)
        mixer = m
        systemCapture = args.captureSystem
            ? SystemCapture(targetFormat: targetFormat, sink: m.feedSystem)
            : nil
        micCapture = args.micDeviceName.map { MicCapture(mixer: m, deviceName: $0) }
    } catch {
        eprint("Failed to open output \(args.output ?? "?"): \(error.localizedDescription)")
        exit(1)
    }
}

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
        mixer?.finalize()
        let sysN = systemCapture?.buffersReceived ?? 0
        let micN = micCapture?.buffersReceived ?? 0
        let written = mixer?.framesWritten ?? 0
        eprint("Capture stopped — system buffers: \(sysN), mic buffers: \(micN), frames written: \(written)")
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
