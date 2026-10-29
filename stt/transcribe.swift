// Transcribes WAV files with Apple's on-device speech recognition (SpeechAnalyzer, macOS 26+).
// usage: apple-stt <locale> <words to recognise, comma separated> <file.wav>...
// Prints one line of text per file. The first run downloads the language model (once).
import AVFoundation
import Foundation
import Speech

@main struct Main {
    static func main() async throws {
        let args = CommandLine.arguments
        guard args.count >= 4,
              let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: args[1])) else {
            FileHandle.standardError.write("unsupported locale or bad arguments\n".data(using: .utf8)!)
            exit(2)
        }
        let words = args[2].split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let probe = SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [], attributeOptions: [])
        if let req = try await AssetInventory.assetInstallationRequest(supporting: [probe]) {
            try await req.downloadAndInstall()
        }
        for path in args.dropFirst(3) {
            let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [], attributeOptions: [])
            let analyzer = SpeechAnalyzer(modules: [transcriber])
            if !words.isEmpty {
                let context = AnalysisContext()
                context.contextualStrings[.general] = words
                try await analyzer.setContext(context)
            }
            let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
            async let text = transcriber.results.reduce("") { $0 + String($1.text.characters) }
            if let last = try await analyzer.analyzeSequence(from: file) {
                try await analyzer.finalizeAndFinish(through: last)
            } else {
                await analyzer.cancelAndFinishNow()
            }
            print(try await text.trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }
}
