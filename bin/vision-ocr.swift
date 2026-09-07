// vision-ocr.swift — строковый OCR (VNRecognizeTextRequest). Резерв к vision-doc.
//
// 05.09.2026: прежняя версия глотала любой отказ (`try?`) и всегда выходила с
// кодом 0. Отказ движка выглядел ровно как пустая страница, и роутер клал пустой
// page_NNN.txt как готовую работу. Теперь ошибка Vision — код 4, пустой результат
// без ошибки — код 0 (страница действительно без текста; движок проверяется
// отдельно: bin/vision-doc --selftest).

import Foundation
import Vision
import AppKit

func err(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

let args = CommandLine.arguments
guard args.count > 1, let img = NSImage(contentsOfFile: args[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    err("load fail"); exit(1)
}
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = ["ru-RU", "en-US"]
req.usesLanguageCorrection = true
do {
    try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
} catch {
    err("⛔ OCR НЕ ВЫПОЛНЕН (движок, не страница): \(error) — проверь bin/vision-doc --selftest")
    exit(4)
}
for o in req.results ?? [] {
    if let t = o.topCandidates(1).first { print(t.string) }
}
