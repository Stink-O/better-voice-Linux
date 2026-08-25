import Foundation

/// A replacement map the user edits by hand, so a misheard term can be corrected
/// without rebuilding the app.
///
/// Rebuilding is a poor way to fix one word here: it changes the code signature,
/// and macOS then drops the Accessibility and Screen Recording grants while the
/// toggles still read as enabled.
public enum VocabularyFile {
    static let fileName = "vocabulary.json"
    private static let termsKey = "terms"
    private static let notesKey = "_readme"

    /// `~/Library/Application Support/BetterVoice/vocabulary.json`
    public static func defaultURL() -> URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appendingPathComponent("BetterVoice/\(fileName)")
    }

    /// Reads the map, longest source first so a phrase wins over a word inside it.
    ///
    /// Every failure yields no terms rather than throwing. A file the user typed
    /// into by hand will be malformed sooner or later, and losing a recording over
    /// a stray comma would be worse than ignoring the map for one transcript.
    public static func terms(at url: URL) -> [(String, String)] {
        guard let data = try? Data(contentsOf: url),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let table = root[termsKey] as? [String: String]
        else { return [] }
        return table
            .filter { !$0.key.isEmpty && !$0.value.isEmpty }
            .sorted { lhs, rhs in
                lhs.key.count == rhs.key.count ? lhs.key < rhs.key : lhs.key.count > rhs.key.count
            }
            .map { ($0.key, $0.value) }
    }

    /// Writes the starting file, and never overwrites one the user already edited.
    public static func createTemplateIfMissing(at url: URL) throws {
        guard !FileManager.default.fileExists(atPath: url.path) else { return }
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try Data(template.utf8).write(to: url)
    }

    /// Ships with no terms on purpose. Every entry rewrites free-form speech, so
    /// the first one should be a decision the user made, not one inherited.
    private static let template = """
    {
      "_readme": [
        "Corrections for terms BetterVoice mishears. The key is what comes out,",
        "the value is what you meant. Phrases of several words are allowed.",
        "Saving takes effect on your next recording, no restart needed.",
        "",
        "Example: \\"cube cuttle\\": \\"kubectl\\"",
        "",
        "One rule worth respecting: never use an ordinary word as a key.",
        "Writing \\"read me\\": \\"README\\" would rewrite every sentence that",
        "contains read me. Matching is whole-word and case-insensitive, and it",
        "skips filenames, domains and paths.",
        "",
        "This file is ignored while Developer vocabulary is off."
      ],
      "terms": {
      }
    }

    """
}
