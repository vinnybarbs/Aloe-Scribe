// RecordingSession.swift — orchestrates the record → transcribe → save flow.
//
// The iOS analog of main.py's orchestration: owns the recorder + transcriber,
// drives the state machine, enforces the max-duration cap, and surfaces status
// to the SwiftUI views.

import Foundation
import SwiftUI

@MainActor
final class RecordingSession: ObservableObject {

    enum State: Equatable {
        case idle
        case recording
        case transcribing
        case saved(URL)
        case error(String)
    }

    @Published var state: State = .idle
    @Published var title: String = Settings.shared.defaultTitle
    @Published var elapsed: TimeInterval = 0
    @Published var level: Float = 0

    private let recorder = AudioRecorder()
    private let transcriber = TranscriptionService()
    private var timer: Timer?
    private var startedAt: Date?

    var statusText: String {
        switch state {
        case .idle: return "Ready"
        case .recording: return "Recording…"
        case .transcribing: return transcriber.progress.isEmpty ? "Transcribing…" : transcriber.progress
        case .saved: return "Saved"
        case .error(let m): return m
        }
    }

    var isRecording: Bool { if case .recording = state { return true }; return false }
    var isBusy: Bool {
        switch state { case .recording, .transcribing: return true; default: return false }
    }

    func start() {
        Task {
            do {
                try await recorder.requestPermission()
                try recorder.start()
                startedAt = Date()
                elapsed = 0
                state = .recording
                startTimer()
            } catch RecorderError.permissionDenied {
                state = .error("Microphone access denied. Enable it in Settings.")
            } catch {
                state = .error("Couldn't start recording: \(error.localizedDescription)")
            }
        }
    }

    func stop() {
        guard isRecording else { return }
        stopTimer()
        guard let url = recorder.stop() else {
            state = .error("Recording produced no audio file.")
            return
        }
        let capturedTitle = title.trimmingCharacters(in: .whitespaces).isEmpty
            ? Settings.shared.defaultTitle : title
        let date = startedAt ?? Date()

        Task {
            state = .transcribing
            do {
                let segments = try await transcriber.transcribe(url: url, model: Settings.shared.model)
                let fileURL = try TranscriptWriter.write(title: capturedTitle, date: date, segments: segments)
                try? FileManager.default.removeItem(at: url)   // drop the temp WAV
                state = .saved(fileURL)
            } catch TranscriptWriterError.noFolder {
                state = .error("Pick a folder to save transcripts first.")
            } catch {
                state = .error("Transcription failed: \(error.localizedDescription)")
            }
        }
    }

    // MARK: - Timer / metering / auto-stop

    private func startTimer() {
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
    }

    private func tick() {
        guard let startedAt else { return }
        elapsed = Date().timeIntervalSince(startedAt)
        level = recorder.meterLevel()
        let cap = TimeInterval(Settings.shared.maxDurationMinutes * 60)
        if elapsed >= cap { stop() }   // hard cap, mirrors max_duration_minutes
    }

    private func stopTimer() {
        timer?.invalidate(); timer = nil; level = 0
    }
}
