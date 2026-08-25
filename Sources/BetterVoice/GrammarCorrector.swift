import CryptoKit
import Foundation
import OnnxRuntimeBindings
import Tokenizers

private enum GrammarCorrectionError: LocalizedError {
    case invalidModelAsset(String)
    case missingOutput(String)
    case invalidTensor(String)

    var errorDescription: String? {
        switch self {
        case .invalidModelAsset(let name): return "The grammar model asset \(name) is invalid."
        case .missingOutput(let name): return "The grammar model did not return \(name)."
        case .invalidTensor(let name): return "The grammar model returned an invalid \(name) tensor."
        }
    }
}

private enum GrammarModelAssets {
    static let revision = "d5f27b81d5316bd689977d722d3ed513bbb9122c"
    static let baseURL = "https://huggingface.co/rabden/t5-tiny-gec-hone/resolve/\(revision)/"
    static let modelDirectoryName = "t5-tiny-gec-hone"

    struct Asset {
        let path: String
        let sha256: String
    }

    static let assets = [
        Asset(
            path: "onnx/encoder_model_quantized.onnx",
            sha256: "baa41f33481ca2db5d2b458d3bd50a879f0b6b054fd101fa1e68bb3c41b724a4"
        ),
        Asset(
            path: "onnx/decoder_model_merged_quantized.onnx",
            sha256: "dfce028e696fcb8156ac3571acf8eafca650768453bdcd6295fda910ae61d6d9"
        ),
        Asset(
            path: "tokenizer.json",
            sha256: "d7af4599a1914d04aaf44839418757f40ccca4c78033edeba369484978378335"
        ),
        Asset(
            path: "tokenizer_config.json",
            sha256: "fb20bc34a5b9e424c29d0aa8cf4754a7f0e42cf8ff5ba304dec219c54854dba9"
        ),
        Asset(
            path: "special_tokens_map.json",
            sha256: "5c87151ef0f72a99d1f766a4c418bd2a1f90aaa30a8e22fe5eca9641daebb64f"
        ),
    ]
}

actor GrammarCorrector {
    private struct LoadedModel {
        let tokenizer: any Tokenizer
        let environment: ORTEnv
        let encoder: ORTSession
        let decoder: ORTSession
    }

    private var loadedModel: LoadedModel?

    func isCached() -> Bool {
        let root = modelDirectory
        return GrammarModelAssets.assets.allSatisfy { asset in
            FileManager.default.fileExists(atPath: root.appendingPathComponent(asset.path).path)
        }
    }

    func preload() async -> Bool {
        do {
            _ = try await loadModel()
            return true
        } catch {
            return false
        }
    }

    func correct(_ text: String) async -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.split(whereSeparator: \.isWhitespace).count > 1 else { return text }

        do {
            let model = try await loadModel()
            return try correct(trimmed, with: model)
        } catch {
            // Grammar cleanup is deliberately optional: ASR text remains usable if the model is unavailable.
            return text
        }
    }

    private func loadModel() async throws -> LoadedModel {
        if let loadedModel { return loadedModel }

        let directory = try await downloadModel()
        let tokenizer = try await AutoTokenizer.from(modelFolder: directory)
        let environment = try ORTEnv(loggingLevel: .warning)
        let options = try ORTSessionOptions()
        try options.setIntraOpNumThreads(2)
        try options.setGraphOptimizationLevel(.all)
        let encoder = try ORTSession(
            env: environment,
            modelPath: directory.appendingPathComponent("onnx/encoder_model_quantized.onnx").path,
            sessionOptions: options
        )
        let decoder = try ORTSession(
            env: environment,
            modelPath: directory.appendingPathComponent("onnx/decoder_model_merged_quantized.onnx").path,
            sessionOptions: options
        )
        let model = LoadedModel(tokenizer: tokenizer, environment: environment, encoder: encoder, decoder: decoder)
        loadedModel = model
        return model
    }

    private func downloadModel() async throws -> URL {
        let root = modelDirectory
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)

        for asset in GrammarModelAssets.assets {
            let destination = root.appendingPathComponent(asset.path)
            try FileManager.default.createDirectory(
                at: destination.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            if isValidAsset(at: destination, sha256: asset.sha256) { continue }

            guard let url = URL(string: GrammarModelAssets.baseURL + asset.path + "?download=true") else {
                throw GrammarCorrectionError.invalidModelAsset(asset.path)
            }
            let (temporaryURL, response) = try await URLSession.shared.download(from: url)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                throw GrammarCorrectionError.invalidModelAsset(asset.path)
            }
            guard isValidAsset(at: temporaryURL, sha256: asset.sha256) else {
                throw GrammarCorrectionError.invalidModelAsset(asset.path)
            }
            try? FileManager.default.removeItem(at: destination)
            try FileManager.default.moveItem(at: temporaryURL, to: destination)
        }
        return root
    }

    private var modelDirectory: URL {
        (FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory)
            .appendingPathComponent("BetterVoice", isDirectory: true)
            .appendingPathComponent(GrammarModelAssets.modelDirectoryName, isDirectory: true)
    }

    private func isValidAsset(at url: URL, sha256 expected: String) -> Bool {
        guard let data = try? Data(contentsOf: url) else { return false }
        let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        return digest == expected
    }

    private func correct(_ text: String, with model: LoadedModel) throws -> String {
        let inputIDs = model.tokenizer.encode(text: text)
        guard inputIDs.count <= 128 else { return text }
        let attentionMask = Array(repeating: Int64(1), count: inputIDs.count)
        let encoderInputs: [String: ORTValue] = [
            "input_ids": try tensor(inputIDs.map(Int64.init), type: .int64, shape: [1, inputIDs.count]),
            "attention_mask": try tensor(attentionMask, type: .int64, shape: [1, attentionMask.count]),
        ]
        let encoderOutputs = try run(
            model.encoder,
            inputs: encoderInputs,
            outputNames: ["last_hidden_state"]
        )
        guard let hiddenStates = encoderOutputs["last_hidden_state"] else {
            throw GrammarCorrectionError.missingOutput("encoder hidden states")
        }
        guard let attention = encoderInputs["attention_mask"] else {
            throw GrammarCorrectionError.invalidTensor("attention mask")
        }

        var decoderInputs: [String: ORTValue] = [
            "encoder_hidden_states": hiddenStates,
            "encoder_attention_mask": attention,
        ]
        for layer in 0..<4 {
            decoderInputs["past_key_values.\(layer).decoder.key"] = try tensor(
                [],
                type: .float,
                shape: [1, 4, 0, 64]
            )
            decoderInputs["past_key_values.\(layer).decoder.value"] = try tensor(
                [],
                type: .float,
                shape: [1, 4, 0, 64]
            )
        }

        let decoderOutputs = Set(
            ["logits"] + (0..<4).flatMap { layer in
                [
                    "present.\(layer).decoder.key",
                    "present.\(layer).decoder.value",
                ]
            }
        )
        var decoderToken = 0
        var outputIDs: [Int] = []
        var reachedEnd = false
        for _ in 0..<96 {
            decoderInputs["input_ids"] = try tensor(
                [Int64(decoderToken)],
                type: .int64,
                shape: [1, 1]
            )
            let outputs = try run(model.decoder, inputs: decoderInputs, outputNames: decoderOutputs)
            guard let logits = outputs["logits"] else {
                throw GrammarCorrectionError.missingOutput("decoder logits")
            }
            let nextToken = try argmaxLastVocabularyValue(logits)
            if nextToken == 1 {
                reachedEnd = true
                break
            }
            outputIDs.append(nextToken)
            decoderToken = nextToken
            for layer in 0..<4 {
                guard
                    let key = outputs["present.\(layer).decoder.key"],
                    let value = outputs["present.\(layer).decoder.value"]
                else {
                    throw GrammarCorrectionError.missingOutput("decoder cache")
                }
                decoderInputs["past_key_values.\(layer).decoder.key"] = key
                decoderInputs["past_key_values.\(layer).decoder.value"] = value
            }
        }

        guard reachedEnd else { return text }
        let corrected = model.tokenizer.decode(tokens: outputIDs, skipSpecialTokens: true)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !corrected.isEmpty else { return text }
        let minimumLength = max(8, Int(Double(text.count) * 0.55))
        return corrected.count >= minimumLength ? corrected : text
    }

    private func tensor<T>(
        _ values: [T],
        type: ORTTensorElementDataType,
        shape: [Int]
    ) throws -> ORTValue {
        let data: NSMutableData
        if values.isEmpty {
            data = NSMutableData(data: Data())
        } else {
            data = values.withUnsafeBufferPointer { buffer in
                NSMutableData(bytes: buffer.baseAddress, length: buffer.count * MemoryLayout<T>.stride)
            }
        }
        return try ORTValue(
            tensorData: data,
            elementType: type,
            shape: shape.map(NSNumber.init(value:))
        )
    }

    private func run(
        _ session: ORTSession,
        inputs: [String: ORTValue],
        outputNames: Set<String>
    ) throws -> [String: ORTValue] {
        try session.run(withInputs: inputs, outputNames: outputNames, runOptions: nil)
    }

    private func argmaxLastVocabularyValue(_ value: ORTValue) throws -> Int {
        let data = try value.tensorData()
        let count = data.length / MemoryLayout<Float>.stride
        guard count > 0 else {
            throw GrammarCorrectionError.invalidTensor("decoder logits")
        }
        let bytes = data.bytes
        let logits = Array(
            UnsafeBufferPointer(
                start: bytes.assumingMemoryBound(to: Float.self),
                count: count
            )
        )
        let vocabularySize = 32_128
        guard logits.count >= vocabularySize else {
            throw GrammarCorrectionError.invalidTensor("decoder logits")
        }
        let offset = logits.count - vocabularySize
        var bestIndex = 0
        var bestValue = -Float.infinity
        for index in 0..<vocabularySize {
            let candidate = logits[offset + index]
            if candidate > bestValue {
                bestValue = candidate
                bestIndex = index
            }
        }
        return bestIndex
    }
}
