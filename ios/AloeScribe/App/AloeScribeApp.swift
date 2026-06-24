// AloeScribeApp.swift — entry point.
//
// iPhone counterpart to the desktop Aloe Scribe. Mic-only by design: there is
// no system-audio capture on iOS (and no need for one — put a call on speaker
// and the other side is audible to the mic). Records locally, transcribes
// on-device with WhisperKit, and writes a Markdown transcript into a folder
// the user picks in the Files app.

import SwiftUI

@main
struct AloeScribeApp: App {
    @StateObject private var session = RecordingSession()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(session)
        }
    }
}
