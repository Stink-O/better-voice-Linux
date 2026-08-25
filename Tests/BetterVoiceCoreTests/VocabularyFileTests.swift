import XCTest
@testable import BetterVoiceCore

final class VocabularyFileTests: XCTestCase {
    private func temporaryURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("vocabulary-\(UUID().uuidString).json")
    }

    private func write(_ json: String, to url: URL) throws {
        try Data(json.utf8).write(to: url)
    }

    func testReadsTheTermsObject() throws {
        let url = temporaryURL()
        try write(#"{"terms": {"cube cuttle": "kubectl", "engine x": "nginx"}}"#, to: url)
        defer { try? FileManager.default.removeItem(at: url) }

        XCTAssertEqual(
            Dictionary(uniqueKeysWithValues: VocabularyFile.terms(at: url)),
            ["cube cuttle": "kubectl", "engine x": "nginx"]
        )
    }

    func testSortsLongerSourcesFirstSoPhrasesWinOverTheWordsInside() throws {
        let url = temporaryURL()
        try write(#"{"terms": {"engine": "engine", "engine x": "nginx"}}"#, to: url)
        defer { try? FileManager.default.removeItem(at: url) }

        XCTAssertEqual(VocabularyFile.terms(at: url).first?.0, "engine x")
    }

    func testIgnoresTheNotesKey() throws {
        let url = temporaryURL()
        try write(#"{"_readme": ["a note"], "terms": {"engine x": "nginx"}}"#, to: url)
        defer { try? FileManager.default.removeItem(at: url) }

        XCTAssertEqual(VocabularyFile.terms(at: url).count, 1)
    }

    func testDropsEntriesWithAnEmptySourceOrReplacement() throws {
        let url = temporaryURL()
        try write(#"{"terms": {"": "nginx", "engine x": "", "psequel": "psql"}}"#, to: url)
        defer { try? FileManager.default.removeItem(at: url) }

        XCTAssertEqual(VocabularyFile.terms(at: url).map(\.0), ["psequel"])
    }

    func testMalformedJsonYieldsNoTermsRatherThanThrowing() throws {
        let url = temporaryURL()
        try write("{ not json at all", to: url)
        defer { try? FileManager.default.removeItem(at: url) }

        XCTAssertTrue(VocabularyFile.terms(at: url).isEmpty)
    }

    func testAMissingFileYieldsNoTerms() {
        XCTAssertTrue(VocabularyFile.terms(at: temporaryURL()).isEmpty)
    }

    func testWrongValueTypesYieldNoTerms() throws {
        let url = temporaryURL()
        try write(#"{"terms": {"engine x": 7}}"#, to: url)
        defer { try? FileManager.default.removeItem(at: url) }

        XCTAssertTrue(VocabularyFile.terms(at: url).isEmpty)
    }

    func testWritesATemplateOnlyWhenTheFileIsAbsent() throws {
        let url = temporaryURL()
        defer { try? FileManager.default.removeItem(at: url) }

        try VocabularyFile.createTemplateIfMissing(at: url)
        try write(#"{"terms": {"mine": "kept"}}"#, to: url)
        try VocabularyFile.createTemplateIfMissing(at: url)

        XCTAssertEqual(VocabularyFile.terms(at: url).map(\.0), ["mine"])
    }

    func testTheTemplateIsValidAndStartsWithNoTerms() throws {
        let url = temporaryURL()
        defer { try? FileManager.default.removeItem(at: url) }

        try VocabularyFile.createTemplateIfMissing(at: url)

        let data = try Data(contentsOf: url)
        let root = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertNotNil(root?["_readme"], "the template must explain itself")
        XCTAssertTrue(VocabularyFile.terms(at: url).isEmpty, "no term ships enabled by default")
    }
}
