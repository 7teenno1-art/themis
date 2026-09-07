// vision-doc.swift — структурный OCR через Apple Vision (macOS 26+).
//
// Зачем. Прежний bin/vision-ocr.swift работает на VNRecognizeTextRequest — это
// распознавание СТРОК. Таблица на выходе рассыпается: заголовки идут отдельными
// строками, потом номера колонок, связь ячейки со строкой теряется. На стр. 82
// одного экспертного заключения, где по таблице считались остатки счетов,
// выход выглядит как «Эмитент / Номер гос. рег- / ии, ISIN / Тип ЦБ / … / 10 11 12».
// Таблица там — доказательство, а не оформление.
//
// macOS 26 принес RecognizeDocumentsRequest: он отдает DocumentObservation со
// структурой — параграфы, списки и ТАБЛИЦЫ с сеткой ячеек (rows: [[Cell]],
// у ячейки rowRange/columnRange, то есть объединенные ячейки тоже видны).
//
// ТИХИЙ ПРОВАЛ И ПОЧЕМУ ЕГО БОЛЬШЕ НЕТ (авария 05.09.2026).
// RecognizeDocumentsRequest при недоступном вычислителе возвращает НЕ ошибку, а
// пустой документ: results.count == 1, paragraphs == [], tables == []. Прежняя
// версия печатала пустоту и выходила с кодом 0 — вышестоящий шаг считал работу
// сделанной и клал 41 пустой page_NNN.txt как «пустые сканы». Теперь пустой
// результат проходит лестницу диагноза:
//   1) строковый VNRecognizeTextRequest(.accurate) — он, в отличие от нового API,
//      отказ движка БРОСАЕТ (nilError). Дал текст — печатаем его, работа спасена;
//   2) контрольная картинка с заведомо известной строкой распознается тут же;
//      не распозналась — движка нет, страница ни при чем.
// Итог: «на странице пусто» и «движок мертв» больше не выглядят одинаково.
// Коды возврата: 0 ок · 1 не читается файл · 2 аргументы · 3 движок мертв
// (проверено контролем) · 4 ошибка Vision · 5 --selftest не прошел.
//
// Использование:
//   vision-doc IMAGE            → Markdown: параграфы + таблицы GFM
//   vision-doc IMAGE --json     → JSON: {tables:[{rows:[[cell]]}], paragraphs:[...]}
//   vision-doc IMAGE --text     → только текст (совместимо со старым vision-ocr)
//   vision-doc --selftest       → контрольная картинка: движок жив? (0/5)
//
// Языки: ru-RU, en-US. Коррекция включена.

import Foundation
import Vision
import AppKit
import Metal

let LANGS = ["ru-RU", "en-US"]
let CONTROL_TEXT = "HELLO WORLD 12345"

func err(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

// MARK: - контрольная картинка

/// Рисует заведомо распознаваемую строку. Нужна, чтобы отличить пустую страницу
/// от мертвого движка: если ЭТО не читается, дело не в материале.
func controlImage() -> CGImage? {
    let w = 1200, h = 300
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
    (CONTROL_TEXT as NSString).draw(at: NSPoint(x: 40, y: 100), withAttributes: [
        .font: NSFont.systemFont(ofSize: 90), .foregroundColor: NSColor.black,
    ])
    NSGraphicsContext.restoreGraphicsState()
    guard let img = ctx.makeImage() else { return nil }
    // Проверяем, что чернила действительно легли: без этого сломанная отрисовка
    // выдала бы «движок мертв» на живом движке.
    guard let d = ctx.data else { return nil }
    let px = d.bindMemory(to: UInt8.self, capacity: ctx.bytesPerRow * h)
    var dark = 0
    for y in stride(from: 0, to: h, by: 3) {
        for x in stride(from: 0, to: w, by: 3) where px[y * ctx.bytesPerRow + x * 4] < 128 { dark += 1 }
    }
    return dark > 50 ? img : nil
}

// MARK: - строковый OCR (старый API: отказ движка бросает ошибку)

func legacyLines(_ cg: CGImage, langs: [String]) throws -> [String] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = langs
    req.usesLanguageCorrection = true
    try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    return (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
}

/// Жив ли движок. Проверяется контрольной картинкой, а не догадками.
/// Возврат nil — жив; строка — причина смерти, готовая для доклада.
func engineAutopsy() -> String? {
    guard let ctl = controlImage() else {
        return "не удалось построить контрольную картинку (нет отрисовки текста в этом процессе)"
    }
    var reason: String? = nil
    do {
        let got = try legacyLines(ctl, langs: ["en-US"]).joined(separator: " ")
        if got.replacingOccurrences(of: " ", with: "")
              .contains("HELLOWORLD") { return nil }        // движок жив
        reason = "контрольная строка «\(CONTROL_TEXT)» не распозналась (получено: «\(got)»)"
    } catch {
        reason = "контрольная картинка дала ошибку: \(error)"
    }
    let metal = MTLCreateSystemDefaultDevice()
    var d = [reason!]
    d.append("Metal-устройство: " + (metal.map { $0.name } ?? "НЕТ (MTLCreateSystemDefaultDevice = nil)"))
    if metal == nil {
        d.append("Vision считает нейросеть на Neural Engine или GPU. Оба идут через IOKit,")
        d.append("а в этой песочнице закрыт iokit-open-user-client и mach-lookup к")
        d.append("com.apple.aned / com.apple.ANECompilerService / com.apple.MTLCompilerService.")
        d.append("Без вычислителя сеть не поднимается — распознавать нечем.")
        d.append("Проверить самому: bin/vision-doc --selftest")
    }
    return d.joined(separator: "\n   ")
}

func die(engine reason: String, page: String) -> Never {
    err("""
    ⛔ ДВИЖОК РАСПОЗНАВАНИЯ НЕ РАБОТАЕТ — это НЕ пустая страница.
       файл: \(page)
       \(reason)
       Текст НЕ извлечен. Считать материал пустым запрещено.
    """)
    exit(3)
}

// MARK: - аргументы

let args = CommandLine.arguments
guard args.count > 1 else {
    err("usage: vision-doc IMAGE [--json|--text|--md] | vision-doc --selftest")
    exit(2)
}

if args[1] == "--selftest" {
    if let why = engineAutopsy() {
        err("SELFTEST: ПРОВАЛ\n   \(why)")
        exit(5)
    }
    print("SELFTEST: ОК — контрольная строка распознана, движок жив.")
    exit(0)
}

let path = args[1]
let mode = args.count > 2 ? args[2] : "--md"

guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    err("load fail: \(path)")
    exit(1)
}

// MARK: - разметка

/// Текст ячейки: у Cell есть .content (Container), берем его параграфы.
func cellText(_ cell: DocumentObservation.Container.Table.Cell) -> String {
    cell.content.paragraphs
        .map { $0.transcript.replacingOccurrences(of: "\n", with: " ") }
        .joined(separator: " ")
        .trimmingCharacters(in: .whitespacesAndNewlines)
}

/// GFM-таблица. Вертикальная черта внутри ячейки экранируется, иначе ломается разметка.
func tableToMarkdown(_ t: DocumentObservation.Container.Table) -> String {
    let rows = t.rows
    guard !rows.isEmpty else { return "" }
    let width = rows.map { $0.count }.max() ?? 0
    guard width > 0 else { return "" }

    func line(_ cells: [DocumentObservation.Container.Table.Cell]) -> String {
        var out = cells.map { cellText($0).replacingOccurrences(of: "|", with: "\\|") }
        while out.count < width { out.append("") }
        return "| " + out.joined(separator: " | ") + " |"
    }

    var md = [line(rows[0])]
    md.append("|" + String(repeating: " --- |", count: width))
    for r in rows.dropFirst() { md.append(line(r)) }
    return md.joined(separator: "\n")
}

func jsonEscape(_ s: String) -> String {
    var o = ""
    for c in s.unicodeScalars {
        switch c {
        case "\"": o += "\\\""
        case "\\": o += "\\\\"
        case "\n": o += "\\n"
        case "\t": o += "\\t"
        case "\r": o += "\\r"
        default:
            if c.value < 0x20 { o += String(format: "\\u%04x", c.value) } else { o.unicodeScalars.append(c) }
        }
    }
    return o
}

func emit(paragraphs: [String], tables: [DocumentObservation.Container.Table]) {
    switch mode {
    case "--text":
        // Совместимость со старым vision-ocr: плоский текст, но таблицы
        // выводятся построчно ячейками — уже связнее, чем строки экрана.
        for p in paragraphs { print(p) }
        for t in tables {
            for row in t.rows { print(row.map(cellText).joined(separator: "\t")) }
        }

    case "--json":
        var parts: [String] = []
        parts.append("\"paragraphs\":[" + paragraphs.map { "\"\(jsonEscape($0))\"" }.joined(separator: ",") + "]")
        let tj = tables.map { t -> String in
            let rows = t.rows.map { row in
                "[" + row.map { "\"\(jsonEscape(cellText($0)))\"" }.joined(separator: ",") + "]"
            }.joined(separator: ",")
            return "{\"rows\":[\(rows)]}"
        }.joined(separator: ",")
        parts.append("\"tables\":[\(tj)]")
        parts.append("\"table_count\":\(tables.count)")
        print("{" + parts.joined(separator: ",") + "}")

    default:
        for p in paragraphs where !p.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            print(p)
        }
        for (i, t) in tables.enumerated() {
            let md = tableToMarkdown(t)
            if !md.isEmpty {
                print("\n<!-- таблица \(i + 1): \(t.rows.count) строк -->")
                print(md)
            }
        }
    }
}

/// Плоский вывод строкового резерва — структуры таблиц в нем нет, и это сказано вслух.
func emitLines(_ lines: [String]) {
    switch mode {
    case "--json":
        print("{\"paragraphs\":[" + lines.map { "\"\(jsonEscape($0))\"" }.joined(separator: ",")
              + "],\"tables\":[],\"table_count\":0,\"degraded\":\"строковый резерв: структуры таблиц нет\"}")
    default:
        for l in lines { print(l) }
    }
    err("⚠ Структурный проход дал пусто, текст взят строковым резервом "
        + "(VNRecognizeTextRequest). Таблицы в этом выводе не размечены.")
}

// MARK: - основной проход

let sem = DispatchSemaphore(value: 0)
var exitCode: Int32 = 0

Task {
    defer { sem.signal() }
    var req = RecognizeDocumentsRequest()
    req.textRecognitionOptions.recognitionLanguages = LANGS.map { Locale.Language(identifier: $0) }
    req.textRecognitionOptions.useLanguageCorrection = true

    var paragraphs: [String] = []
    var tables: [DocumentObservation.Container.Table] = []
    var structuralError: String? = nil

    do {
        let results = try await req.perform(on: cg)
        if let doc = results.first?.document {
            paragraphs = doc.paragraphs.map { $0.transcript }
            tables = doc.tables
        } else {
            structuralError = "RecognizeDocumentsRequest не вернул DocumentObservation"
        }
    } catch {
        structuralError = "RecognizeDocumentsRequest: \(error)"
    }

    let gotStructure = !paragraphs.joined().trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        || !tables.isEmpty
    if gotStructure {
        emit(paragraphs: paragraphs, tables: tables)
        return
    }

    // Пусто. Дальше — лестница диагноза, а не молчаливый выход с кодом 0.
    if let e = structuralError { err("структурный проход: \(e)") }

    do {
        let lines = try legacyLines(cg, langs: LANGS)
        if !lines.joined().trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            emitLines(lines)
            return
        }
        // Ни структуры, ни строк и без ошибки — спрашиваем контроль.
        if let why = engineAutopsy() { die(engine: why, page: path) }
        err("ℹ Страница распознана, текста на ней нет (контроль движка пройден): \(path)")
    } catch {
        let why = engineAutopsy() ?? "строковый проход упал: \(error)"
        die(engine: why, page: path)
    }
}

sem.wait()
exit(exitCode)
