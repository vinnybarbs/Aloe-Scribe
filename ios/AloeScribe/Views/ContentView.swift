// ContentView.swift — the single-screen UI.
//
// Mirrors the desktop idle/recording screens: a title field, the big
// Start/Stop button, a level meter, the speakerphone reminder, a model
// picker, and the "Save transcripts to" folder chooser.

import SwiftUI

struct ContentView: View {
    @EnvironmentObject var session: RecordingSession
    @StateObject private var bookmarks = BookmarkStore.shared
    @StateObject private var settings = Settings.shared

    @State private var showFolderPicker = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 24) {
                    speakerReminder
                    titleField
                    recordButton
                    levelMeter
                    statusLine
                    Divider().padding(.vertical, 4)
                    folderSection
                    modelSection
                }
                .padding()
            }
            .navigationTitle("Aloe Scribe")
            .sheet(isPresented: $showFolderPicker) {
                FolderPicker { url in bookmarks.save(url: url) }
            }
        }
    }

    // MARK: - Pieces

    private var speakerReminder: some View {
        HStack(spacing: 10) {
            Image(systemName: "speaker.wave.3.fill")
            Text("On a call? Switch to **speaker** so the other side is audible to the mic.")
                .font(.footnote)
            Spacer()
        }
        .padding()
        .background(Color.orange.opacity(0.15))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var titleField: some View {
        TextField("Conversation title", text: $session.title)
            .textFieldStyle(.roundedBorder)
            .disabled(session.isBusy)
    }

    private var recordButton: some View {
        Button(action: { session.isRecording ? session.stop() : session.start() }) {
            VStack(spacing: 8) {
                Image(systemName: session.isRecording ? "stop.fill" : "mic.fill")
                    .font(.system(size: 44))
                Text(session.isRecording ? "Stop & Transcribe" : "Start Recording")
                    .font(.headline)
            }
            .frame(maxWidth: .infinity)
            .frame(height: 140)
            .background(session.isRecording ? Color.red.opacity(0.85) : Color.green.opacity(0.85))
            .foregroundStyle(.white)
            .clipShape(RoundedRectangle(cornerRadius: 20))
        }
        .disabled(session.state == .transcribing || (!bookmarks.hasFolder && !session.isRecording))
    }

    private var levelMeter: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 4).fill(Color.gray.opacity(0.2))
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color.green)
                    .frame(width: geo.size.width * CGFloat(session.level))
            }
        }
        .frame(height: 8)
        .opacity(session.isRecording ? 1 : 0.3)
    }

    private var statusLine: some View {
        HStack {
            if session.isRecording {
                Text(timeString(session.elapsed)).monospacedDigit()
            }
            Text(session.statusText).foregroundStyle(.secondary)
            Spacer()
            if case .saved(let url) = session.state {
                ShareLink(item: url) { Image(systemName: "square.and.arrow.up") }
            }
        }
        .font(.subheadline)
    }

    private var folderSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Save transcripts to").font(.subheadline.bold())
            HStack {
                Image(systemName: "folder")
                Text(bookmarks.folderName ?? "No folder chosen")
                    .foregroundStyle(bookmarks.folderName == nil ? .secondary : .primary)
                Spacer()
                Button("Choose…") { showFolderPicker = true }
                    .buttonStyle(.bordered)
            }
        }
    }

    private var modelSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Transcription model").font(.subheadline.bold())
            Picker("Model", selection: $settings.model) {
                ForEach(WhisperModel.allCases) { m in
                    Text(m.label).tag(m)
                }
            }
            .pickerStyle(.menu)
            .disabled(session.isBusy)
        }
    }

    private func timeString(_ t: TimeInterval) -> String {
        String(format: "%02d:%02d", Int(t) / 60, Int(t) % 60)
    }
}
