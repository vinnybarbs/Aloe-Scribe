// TranscriptWriter.swift — renders + writes the Markdown transcript.
//
// Output format and filename mirror the desktop app exactly:
//   ~/meetings/2026-04-07-1400-weekly-sync.md
//   # Title / Date / Time / --- / ## Full Transcript / `MM:SS` lines
// so transcripts from the phone and the Mac look identical.

import Foundation

enum TranscriptWriterError: Error {
    case noFolder
    case writeFailed(Error)
}

struct TranscriptWriter {

    /// Slugifies a title for the filename (matches Meeting.slug in meeting.py).
    static func slug(_ title: String) -> String {
        let lowered = title.lowercased()
        let stripped = lowered.unicodeScalars.map { scalar -> Character in
            if CharacterSet.alphanumerics.contains(scalar) { return Character(scalar) }
            if scalar == " " || scalar == "-" || scalar == "_" { return " " }
            return " "
        }
        let collapsed = String(stripped)
            .split(separator: " ", omittingEmptySubsequences: true)
            .joined(separator: "-")
        return collapsed.isEmpty ? "conversation" : collapsed
    }

    /// `123.0` seconds → "02:03"
    private static func mmss(_ seconds: Double) -> String {
        let total = Int(seconds.rounded(.down))
        return String(format: "%02d:%02d", total / 60, total % 60)
    }

    /// YAML frontmatter — the time-window identity of the meeting, for the
    /// downstream enrichment AI. Mirrors src/frontmatter.py exactly. No
    /// participants: the agent infers them from the matched calendar event.
    static func frontmatter(title: String, date: Date, segments: [TranscriptSegment]) -> String {
        let iso = ISO8601DateFormatter()
        iso.timeZone = .current
        iso.formatOptions = [.withInternetDateTime]   // e.g. 2026-06-17T14:00:00-05:00

        let durationS = segments.map(\.end).max() ?? 0
        let end = date.addingTimeInterval(durationS)
        let durationMin = max(0, Int((durationS / 60).rounded()))
        let safeTitle = title.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")

        return [
            "---",
            "title: \"\(safeTitle)\"",
            "date: \(iso.string(from: date))",
            "end: \(iso.string(from: end))",
            "duration_min: \(durationMin)",
            "source: aloe-scribe-ios",
            "---",
        ].joined(separator: "\n")
    }

    /// Builds the Markdown body.
    static func render(title: String, date: Date, segments: [TranscriptSegment]) -> String {
        let df = DateFormatter(); df.dateFormat = "MMMM d, yyyy"
        let tf = DateFormatter(); tf.dateFormat = "hh:mm a"
        let stamp = DateFormatter(); stamp.dateFormat = "yyyy-MM-dd HH:mm"

        var lines: [String] = []
        lines.append(frontmatter(title: title, date: date, segments: segments))
        lines.append("# \(title)")
        lines.append("**Date:** \(df.string(from: date))")
        lines.append("**Time:** \(tf.string(from: date))")
        lines.append("**Transcribed by:** Aloe Scribe (iOS)")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## Full Transcript")
        lines.append("")
        if segments.isEmpty {
            lines.append("_(no speech detected)_")
        } else {
            for seg in segments {
                lines.append("`\(mmss(seg.start))` \(seg.text)")
            }
        }
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("_Transcript generated \(stamp.string(from: Date())) · Aloe Scribe_")
        return lines.joined(separator: "\n") + "\n"
    }

    /// Filename: `{date}-{time}-{slug}.md` (matches filename_format).
    static func filename(title: String, date: Date) -> String {
        let df = DateFormatter(); df.dateFormat = "yyyy-MM-dd"
        let tf = DateFormatter(); tf.dateFormat = "HHmm"
        return "\(df.string(from: date))-\(tf.string(from: date))-\(slug(title)).md"
    }

    /// Writes the transcript into the user-picked folder. Returns the file URL.
    @discardableResult
    static func write(title: String, date: Date, segments: [TranscriptSegment]) throws -> URL {
        guard let folder = BookmarkStore.shared.resolve() else {
            throw TranscriptWriterError.noFolder
        }
        let scoped = folder.startAccessingSecurityScopedResource()
        defer { if scoped { folder.stopAccessingSecurityScopedResource() } }

        let body = render(title: title, date: date, segments: segments)
        let url = folder.appendingPathComponent(filename(title: title, date: date))
        do {
            try body.data(using: .utf8)!.write(to: url, options: .atomic)
            return url
        } catch {
            throw TranscriptWriterError.writeFailed(error)
        }
    }
}
