// Local Apple Vision OCR. Coordinates returned in image top-left orientation.
import Foundation
import Vision
import ImageIO

struct Block: Codable {
    var text: String
    var confidence: Float
    var bbox: [Double]
    var angle: Double?
}
struct Recognition: Codable {
    var blocks: [Block]
    var error: String?
}
var results: [Recognition] = []
for path in CommandLine.arguments.dropFirst() {
    do {
        guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            results.append(Recognition(blocks: [], error: "Cannot decode OCR image")); continue
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        let languages = try request.supportedRecognitionLanguages()
        request.recognitionLanguages = ["zh-Hans", "en-US"].filter { languages.contains($0) }
        request.usesLanguageCorrection = false
        request.minimumTextHeight = 0.004
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        let blocks = (request.results ?? []).prefix(3000).compactMap { observation -> Block? in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            let box = observation.boundingBox
            var angle: Double? = nil
            if let rectangle = try? candidate.boundingBox(for: candidate.string.startIndex..<candidate.string.endIndex) {
                let dx = Double(rectangle.topRight.x - rectangle.topLeft.x) * Double(image.width)
                let dy = -Double(rectangle.topRight.y - rectangle.topLeft.y) * Double(image.height)
                angle = atan2(dy, dx) * 180 / .pi
            }
            return Block(text: candidate.string, confidence: candidate.confidence,
                         bbox: [Double(box.minX), Double(1 - box.maxY), Double(box.maxX), Double(1 - box.minY)], angle: angle)
        }
        results.append(Recognition(blocks: blocks, error: nil))
    } catch {
        results.append(Recognition(blocks: [], error: String(describing: error)))
    }
}
let data = try JSONEncoder().encode(results)
FileHandle.standardOutput.write(data)
