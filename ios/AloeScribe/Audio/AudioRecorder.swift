// AudioRecorder.swift — mic capture to a 16 kHz mono WAV.
//
// WhisperKit expects 16 kHz mono PCM; recording straight to that format keeps
// the file small and avoids a resample step. We configure the AVAudioSession
// for `.playAndRecord` with `.defaultToSpeaker` so a) the mic is live and
// b) playback (and the speakerphone reminder) routes out the loud speaker.
//
// Limitation worth knowing: during an active *native* phone/FaceTime call on
// THIS device, iOS gives the mic to the call and third-party capture is
// blocked. This records fine for in-person conversations and for a call
// playing aloud on a *separate* device.

import Foundation
import AVFoundation

enum RecorderError: Error {
    case permissionDenied
    case sessionFailed(Error)
    case recorderFailed(Error)
}

final class AudioRecorder: NSObject {
    private var recorder: AVAudioRecorder?
    private(set) var outputURL: URL?

    override init() {
        super.init()
        // Resume recording after transient interruptions (Siri, alarms, a
        // notification chime). The screen simply locking is NOT an interruption
        // — the `audio` UIBackgroundMode keeps the recorder running through it.
        NotificationCenter.default.addObserver(
            self, selector: #selector(handleInterruption(_:)),
            name: AVAudioSession.interruptionNotification, object: nil)
    }

    deinit { NotificationCenter.default.removeObserver(self) }

    @objc private func handleInterruption(_ note: Notification) {
        guard let info = note.userInfo,
              let raw = info[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }

        switch type {
        case .began:
            // System paused us (the recorder stops capturing for the duration).
            break
        case .ended:
            // Reactivate the session and continue into the same file.
            let opts = (info[AVAudioSessionInterruptionOptionKey] as? UInt)
                .map(AVAudioSession.InterruptionOptions.init) ?? []
            if opts.contains(.shouldResume) {
                try? AVAudioSession.sharedInstance().setActive(true)
                recorder?.record()
            }
        @unknown default:
            break
        }
    }

    /// Requests mic permission. Throws `.permissionDenied` if refused.
    func requestPermission() async throws {
        let granted = await withCheckedContinuation { cont in
            AVAudioApplication.requestRecordPermission { cont.resume(returning: $0) }
        }
        guard granted else { throw RecorderError.permissionDenied }
    }

    /// Begins recording into a temporary WAV. Returns the file URL.
    @discardableResult
    func start() throws -> URL {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playAndRecord,
                                    mode: .default,
                                    options: [.defaultToSpeaker])
            try session.setActive(true)
        } catch {
            throw RecorderError.sessionFailed(error)
        }

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("aloe-\(Int(Date().timeIntervalSince1970)).wav")

        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatLinearPCM),
            AVSampleRateKey: 16_000,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsFloatKey: false,
        ]

        do {
            let rec = try AVAudioRecorder(url: url, settings: settings)
            rec.isMeteringEnabled = true
            guard rec.record() else {
                throw RecorderError.recorderFailed(
                    NSError(domain: "AloeScribe", code: -1,
                            userInfo: [NSLocalizedDescriptionKey: "AVAudioRecorder.record() returned false"]))
            }
            self.recorder = rec
            self.outputURL = url
            return url
        } catch let e as RecorderError {
            throw e
        } catch {
            throw RecorderError.recorderFailed(error)
        }
    }

    /// Stops recording and returns the finalized WAV URL.
    @discardableResult
    func stop() -> URL? {
        recorder?.stop()
        recorder = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        return outputURL
    }

    /// Current input level in 0...1, for a VU meter (mirrors Aloe's audio_meter).
    func meterLevel() -> Float {
        guard let rec = recorder else { return 0 }
        rec.updateMeters()
        let db = rec.averagePower(forChannel: 0)   // -160...0 dBFS
        let clamped = max(-60, db)
        return (clamped + 60) / 60                  // → 0...1
    }
}
