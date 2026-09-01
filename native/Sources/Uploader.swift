import Foundation

/// Posts dropped PDFs to the server's upload endpoint.
///
/// This is the same request the web page makes when you drop a file onto it,
/// so a PDF dropped on the Dock icon and a PDF dropped on the window take the
/// identical path through the server.
enum Uploader {
    enum UploadError: LocalizedError {
        case unreadable(String)
        case rejected(Int, String)

        var errorDescription: String? {
            switch self {
            case .unreadable(let name): return "Could not read \(name)."
            case .rejected(let status, let body):
                return "The server refused the upload (HTTP \(status)). \(body)"
            }
        }
    }

    static func isPDF(_ url: URL) -> Bool {
        url.pathExtension.lowercased() == "pdf"
    }

    static func upload(
        _ urls: [URL],
        to baseURL: URL,
        extractNow: Bool,
        completion: @escaping (Result<Int, Error>) -> Void
    ) {
        guard !urls.isEmpty else { return completion(.success(0)) }
        var components = URLComponents(url: baseURL.appendingPathComponent("api/upload"),
                                       resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "extract_now", value: extractNow ? "true" : "false")]
        guard let endpoint = components?.url else {
            return completion(.failure(UploadError.unreadable(baseURL.absoluteString)))
        }
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        let boundary = "doxograph-\(UUID().uuidString)"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        let body: URL
        do {
            body = try multipartFile(for: urls, boundary: boundary)
        } catch {
            return completion(.failure(error))
        }

        URLSession.shared.uploadTask(with: request, fromFile: body) { data, response, error in
            try? FileManager.default.removeItem(at: body)
            DispatchQueue.main.async {
                if let error { return completion(.failure(error)) }
                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                guard (200..<300).contains(status) else {
                    let detail = data.map { String(decoding: $0.prefix(500), as: UTF8.self) } ?? ""
                    return completion(.failure(UploadError.rejected(status, detail)))
                }
                let queued = (try? JSONSerialization.jsonObject(with: data ?? Data()))
                    .flatMap { ($0 as? [String: Any])?["queued"] as? Int }
                completion(.success(queued ?? urls.count))
            }
        }.resume()
    }

    /// Builds the request body on disk rather than in memory, so dropping a
    /// stack of large papers at once costs a temp file instead of holding every
    /// one of them resident while the upload runs.
    private static func multipartFile(for urls: [URL], boundary: String) throws -> URL {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("doxograph-upload-\(UUID().uuidString)")
        FileManager.default.createFile(atPath: path.path, contents: nil)
        guard let handle = FileHandle(forWritingAtPath: path.path) else {
            throw UploadError.unreadable(path.lastPathComponent)
        }
        defer { try? handle.close() }

        for url in urls {
            guard let source = FileHandle(forReadingAtPath: url.path) else {
                throw UploadError.unreadable(url.lastPathComponent)
            }
            defer { try? source.close() }
            let header = """
            --\(boundary)\r
            Content-Disposition: form-data; name="files"; filename="\(escape(url.lastPathComponent))"\r
            Content-Type: application/pdf\r
            \r

            """
            try handle.write(contentsOf: Data(header.utf8))
            while let chunk = try source.read(upToCount: 1 << 20), !chunk.isEmpty {
                try handle.write(contentsOf: chunk)
            }
            try handle.write(contentsOf: Data("\r\n".utf8))
        }
        try handle.write(contentsOf: Data("--\(boundary)--\r\n".utf8))
        return path
    }

    /// A quote or a newline in a filename would otherwise break the part header.
    private static func escape(_ filename: String) -> String {
        filename
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
    }
}
