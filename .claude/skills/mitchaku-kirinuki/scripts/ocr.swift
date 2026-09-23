import Foundation
import Vision
import AppKit

let args = CommandLine.arguments.dropFirst()
for path in args {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        print("\(path)\t<LOADFAIL>"); continue
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLanguages = ["ja-JP","en-US"]
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform([req])
        let obs = req.results ?? []
        var lines: [(CGFloat,String)] = []
        for o in obs {
            if let c = o.topCandidates(1).first {
                lines.append((o.boundingBox.origin.y, c.string))
            }
        }
        lines.sort { $0.0 > $1.0 }
        print("\(path)\t" + lines.map{$0.1}.joined(separator: "／"))
    } catch {
        print("\(path)\t<ERR>")
    }
}
