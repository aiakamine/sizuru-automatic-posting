import Foundation
import Vision
import AppKit

// 使い方: ocr <画像パス...>  → 1行1件で "パス\tテキスト" を出力
let args = Array(CommandLine.arguments.dropFirst())
for path in args {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        print("\(path)\t")
        continue
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["ja-JP", "en-US"]
    req.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    var out = ""
    do {
        try handler.perform([req])
        if let obs = req.results {
            let lines = obs.compactMap { $0.topCandidates(1).first?.string }
            out = lines.joined(separator: " ")
        }
    } catch { out = "" }
    print("\(path)\t\(out.replacingOccurrences(of: "\n", with: " "))")
}
