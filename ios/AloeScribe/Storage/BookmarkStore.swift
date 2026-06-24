// BookmarkStore.swift — persists the user-picked output folder.
//
// iOS apps can't write to an arbitrary path. The user picks a folder once via
// UIDocumentPicker; we store a security-scoped bookmark so we can resolve and
// write into that folder on every later launch. This is the iOS equivalent of
// Aloe's "Save transcripts to" folder setting.

import Foundation

final class BookmarkStore: ObservableObject {
    static let shared = BookmarkStore()

    private let key = "outputFolderBookmark"
    private let defaults = UserDefaults.standard

    /// Display name of the chosen folder, or nil if none picked yet.
    @Published private(set) var folderName: String?

    private init() {
        self.folderName = resolve()?.lastPathComponent
    }

    /// Stores a bookmark for `url` (the folder returned by the document picker).
    func save(url: URL) {
        let needsScope = url.startAccessingSecurityScopedResource()
        defer { if needsScope { url.stopAccessingSecurityScopedResource() } }
        do {
            let data = try url.bookmarkData(options: .minimalBookmark,
                                            includingResourceValuesForKeys: nil,
                                            relativeTo: nil)
            defaults.set(data, forKey: key)
            folderName = url.lastPathComponent
        } catch {
            NSLog("BookmarkStore: failed to bookmark folder: \(error)")
        }
    }

    /// Resolves the saved bookmark back into a usable folder URL.
    func resolve() -> URL? {
        guard let data = defaults.data(forKey: key) else { return nil }
        var stale = false
        guard let url = try? URL(resolvingBookmarkData: data,
                                 options: [],
                                 relativeTo: nil,
                                 bookmarkDataIsStale: &stale) else { return nil }
        if stale { save(url: url) }  // re-bookmark if the OS invalidated it
        return url
    }

    var hasFolder: Bool { defaults.data(forKey: key) != nil }
}
