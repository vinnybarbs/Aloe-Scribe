// Settings.swift — persisted user settings, the iOS analog of config.toml.
//
// Stored in UserDefaults. The output *folder* is not a plain path (iOS apps
// can't freely write outside their sandbox) — instead we persist a
// security-scoped bookmark to the user-picked folder. See BookmarkStore.

import Foundation
import Combine

/// WhisperKit model variants, smallest → most accurate. English-only `.en`
/// models mirror Aloe's Parakeet default (technical English, no multilingual).
enum WhisperModel: String, CaseIterable, Identifiable {
    case tinyEN = "openai_whisper-tiny.en"
    case baseEN = "openai_whisper-base.en"
    case smallEN = "openai_whisper-small.en"
    case largeV3Turbo = "openai_whisper-large-v3-v20240930_turbo"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .tinyEN: return "Tiny (English) · fastest, ~75 MB"
        case .baseEN: return "Base (English) · fast, ~145 MB"
        case .smallEN: return "Small (English) · balanced, ~480 MB"
        case .largeV3Turbo: return "Large v3 Turbo · best, ~1.5 GB"
        }
    }
}

final class Settings: ObservableObject {
    static let shared = Settings()

    private let defaults = UserDefaults.standard

    @Published var model: WhisperModel {
        didSet { defaults.set(model.rawValue, forKey: "model") }
    }

    /// Hard cap on recording length — auto-stops + transcribes after this many
    /// minutes (mirrors Aloe's `max_duration_minutes`).
    @Published var maxDurationMinutes: Int {
        didSet { defaults.set(maxDurationMinutes, forKey: "maxDurationMinutes") }
    }

    /// Default title used when the user doesn't name a session.
    @Published var defaultTitle: String {
        didSet { defaults.set(defaultTitle, forKey: "defaultTitle") }
    }

    private init() {
        let raw = defaults.string(forKey: "model") ?? WhisperModel.baseEN.rawValue
        self.model = WhisperModel(rawValue: raw) ?? .baseEN
        let cap = defaults.integer(forKey: "maxDurationMinutes")
        self.maxDurationMinutes = cap == 0 ? 120 : cap
        self.defaultTitle = defaults.string(forKey: "defaultTitle") ?? "Conversation"
    }
}
