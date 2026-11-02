import Foundation

/// Check how an update's commit log is summarized for its alert.
private func expect(_ log: String, _ expected: String, _ label: String) {
    let actual = Updater.summarize(log)
    guard actual == expected else {
        FileHandle.standardError.write("\(label): expected\n\(expected)\ngot\n\(actual)\n".data(using: .utf8)!)
        exit(1)
    }
}

expect("", "", "empty range")
expect("abc1234 Add labels (#1)", "• Add labels (#1)", "one commit")
expect("abc1234", "• Commit abc1234", "empty subject")
expect("abc1234   ", "• Commit abc1234", "whitespace subject")
expect("abc1234\ndef5678 ", "• Commit abc1234\n• Commit def5678", "only empty subjects")
expect(
    (1...7).map { "c\($0)" + ($0 == 6 ? "" : " Change \($0)") }.joined(separator: "\n"),
    "• Change 1\n• Change 2\n• Change 3\n• Change 4\n• Change 5\n…and 2 more.",
    "long range with an empty subject")
print("updater probe passed")
