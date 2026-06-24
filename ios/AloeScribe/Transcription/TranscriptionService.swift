// TranscriptionService.swift — on-device STT via WhisperKit.
//
// Mirrors transcriber_parakeet.py / transcriber.py on the desktop: takes a
// recorded audio file, runs a local model, and returns timestamped segments
// that TranscriptWriter renders into Markdown.
//
// WhisperKit downloads the selected model on first use and caches it on device;
// after that it runs fully offline on the Neural Engine / GPU.

import Foundation
import WhisperKit

struct TranscriptSegment {
    let start: Double   // seconds
    let end: Double
    let text: String
}

@MainActor
final class TranscriptionService: ObservableObject {
    @Published var progress: String = ""

    private var pipe: WhisperKit?
    private var loadedModel: String?

    /// Loads the pipeline for `model` if not already loaded.
    private func ensureLoaded(_ model: WhisperModel) async throws {
        if loadedModel == model.rawValue, pipe != nil { return }
        progress = "Loading \(model.label)…"
        let config = WhisperKitConfig(model: model.rawValue)
        let kit = try await WhisperKit(config)
        self.pipe = kit
        self.loadedModel = model.rawValue
    }

    /// Transcribes the WAV at `url`, returning timestamped segments.
    func transcribe(url: URL, model: WhisperModel) async throws -> [TranscriptSegment] {
        try await ensureLoaded(model)
        guard let pipe else { throw NSError(domain: "AloeScribe", code: -2) }

        progress = "Transcribing…"
        let results = try await pipe.transcribe(audioPath: url.path)

        // Flatten every result's segments into a single timeline.
        var out: [TranscriptSegment] = []
        for result in results {
            for seg in result.segments {
                let text = seg.text
                    .replacingOccurrences(of: "<|endoftext|>", with: "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else { continue }
                out.append(TranscriptSegment(start: Double(seg.start),
                                             end: Double(seg.end),
                                             text: text))
            }
        }
        progress = ""
        return out
    }
}
