// The IMPURE half of the accessibility smoke test: it activates an ALREADY
// RUNNING agentacct process, drives it with key events, dumps the accessibility
// tree after each step, and hands the recording to `AccessibilitySmokeJudge`
// (compiled alongside this file — see Scripts/a11y-smoke.sh).
//
// It never launches, installs, quits or configures anything; it needs a pid.
// Run it through `a11y-smoke.sh`, which documents usage and permissions.
//
//   a11y-smoke-driver --pid <pid> [--dump <file.json>] [--record-only]
//   a11y-smoke-driver --judge <file.json>      # no GUI: re-judge a dump
//
// Requires Accessibility permission for the terminal that runs it (System
// Settings → Privacy & Security → Accessibility); without it AXUIElement reads
// return nothing and the run fails with "no accessibility tree", which is the
// correct answer for "can a screen reader use this app?" from where it sits.

import ApplicationServices
import Foundation

// MARK: - arguments

struct Options {
    var pid: pid_t?
    var dumpPath: String?
    var judgePath: String?
    var recordOnly = false
}

struct OptionError: Error { let message: String }

func parseOptions(_ arguments: [String]) -> Result<Options, OptionError> {
    var options = Options()
    var index = 0
    while index < arguments.count {
        let argument = arguments[index]
        func next() -> String? {
            index += 1
            return index < arguments.count ? arguments[index] : nil
        }
        switch argument {
        case "--pid":
            guard let raw = next(), let value = Int32(raw) else { return .failure(.init(message: "--pid needs a process id")) }
            options.pid = value
        case "--dump":
            guard let path = next() else { return .failure(.init(message: "--dump needs a file path")) }
            options.dumpPath = path
        case "--judge":
            guard let path = next() else { return .failure(.init(message: "--judge needs a file path")) }
            options.judgePath = path
        case "--record-only":
            options.recordOnly = true
        default:
            return .failure(.init(message: "unknown argument \(argument)"))
        }
        index += 1
    }
    if options.judgePath == nil && options.pid == nil {
        return .failure(.init(message: "pass --pid <pid> of a running agentacct, or --judge <file.json>"))
    }
    return .success(options)
}

// MARK: - accessibility reads

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func string(_ element: AXUIElement, _ name: String) -> String? {
    guard let value = attribute(element, name) else { return nil }
    if let text = value as? String { return text.isEmpty ? nil : text }
    if let number = value as? NSNumber { return number.stringValue }
    return nil
}

func actionNames(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success,
          let list = names as? [String] else { return [] }
    return list
}

func childElements(_ element: AXUIElement) -> [AXUIElement] {
    guard let value = attribute(element, kAXChildrenAttribute as String),
          let children = value as? [AXUIElement] else { return [] }
    return children
}

/// Read one element (and, up to `depth`, its children) into a plain node.
func readNode(_ element: AXUIElement, depth: Int) -> AccessibilityNode {
    var node = AccessibilityNode(
        role: string(element, kAXRoleAttribute as String),
        subrole: string(element, kAXSubroleAttribute as String),
        identifier: string(element, "AXIdentifier"),
        title: string(element, kAXTitleAttribute as String),
        label: string(element, kAXDescriptionAttribute as String),
        value: string(element, kAXValueAttribute as String),
        help: string(element, kAXHelpAttribute as String),
        actions: actionNames(element),
        children: nil
    )
    if depth > 0 {
        let children = childElements(element).map { readNode($0, depth: depth - 1) }
        node.children = children.isEmpty ? nil : children
    }
    return node
}

func focusedNode(_ application: AXUIElement) -> AccessibilityNode? {
    guard let value = attribute(application, kAXFocusedUIElementAttribute as String) else { return nil }
    // CFTypeRef → AXUIElement without an unsafe bit-cast dance.
    guard CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    let element = value as! AXUIElement
    return readNode(element, depth: 1)
}

func windowTree(_ application: AXUIElement) -> AccessibilityNode? {
    if let value = attribute(application, kAXFocusedWindowAttribute as String),
       CFGetTypeID(value) == AXUIElementGetTypeID() {
        return readNode(value as! AXUIElement, depth: 24)
    }
    if let value = attribute(application, kAXWindowsAttribute as String),
       let windows = value as? [AXUIElement], let first = windows.first {
        return readNode(first, depth: 24)
    }
    return nil
}

// MARK: - key events

enum Key: CGKeyCode {
    case tab = 0x30
    case ret = 0x24
    case escape = 0x35
}

func press(_ key: Key, shift: Bool = false, into pid: pid_t) {
    guard let source = CGEventSource(stateID: .hidSystemState) else { return }
    let flags: CGEventFlags = shift ? .maskShift : []
    for down in [true, false] {
        let event = CGEvent(keyboardEventSource: source, virtualKey: key.rawValue, keyDown: down)
        event?.flags = flags
        event?.postToPid(pid)
    }
    // The app is a SwiftUI process on its own run loop; give focus a beat to move.
    Thread.sleep(forTimeInterval: 0.25)
}

// MARK: - the run

func record(pid: pid_t) -> AccessibilitySmokeRun {
    let application = AXUIElementCreateApplication(pid)
    NSRunningApplicationActivate(pid)
    Thread.sleep(forTimeInterval: 0.8)

    var steps: [AccessibilityStep] = []

    func capture(_ name: String, input: String) {
        steps.append(AccessibilityStep(
            name: name,
            tree: windowTree(application),
            focus: focusedNode(application),
            input: input
        ))
    }

    // 1. Dashboard as it stands, with focus wherever the window put it.
    press(.tab, into: pid)
    capture("dashboard", input: "Tab")

    // 2. Tab forward until focus reaches a Work row (bounded — a Tab loop that
    //    never reaches a row is exactly the failure this tool reports).
    var landed = false
    for _ in 0..<40 {
        press(.tab, into: pid)
        if let focus = focusedNode(application), AccessibilitySmokeJudge.isRow(focus) {
            landed = true
            break
        }
    }
    capture("work", input: landed ? "Tab ×n → row" : "Tab ×40 (no row reached)")

    // 3. Open the focused row with the keyboard.
    press(.ret, into: pid)
    Thread.sleep(forTimeInterval: 0.6)
    capture("record", input: "Return")

    // 4. Back out again.
    press(.escape, into: pid)
    Thread.sleep(forTimeInterval: 0.5)
    capture("back", input: "Escape")

    return AccessibilitySmokeRun(app: "agentacct", pid: Int(pid), steps: steps)
}

/// `NSRunningApplication.activate` without importing AppKit's UI stack.
func NSRunningApplicationActivate(_ pid: pid_t) {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    process.arguments = ["-e", "tell application \"System Events\" to set frontmost of "
        + "(first process whose unix id is \(pid)) to true"]
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    try? process.run()
    process.waitUntilExit()
}

// MARK: - main

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("a11y-smoke: \(message)\n".utf8))
    exit(2)
}

@main
enum A11ySmokeDriver {
    static func main() {
        let options: Options
        switch parseOptions(Array(CommandLine.arguments.dropFirst())) {
        case .failure(let error): fail(error.message)
        case .success(let parsed): options = parsed
        }

        let run: AccessibilitySmokeRun
        if let judgePath = options.judgePath {
            guard let data = FileManager.default.contents(atPath: judgePath) else {
                fail("cannot read \(judgePath)")
            }
            do {
                run = try AccessibilitySmokeJudge.run(fromJSON: data)
            } catch {
                fail("\(judgePath) is not a run dump: \(error)")
            }
        } else {
            guard AXIsProcessTrusted() else {
                fail("""
                    this terminal has no Accessibility permission, so no AX tree can be read.
                    Grant it in System Settings → Privacy & Security → Accessibility, then rerun.
                    """)
            }
            run = record(pid: options.pid!)
        }

        if let dumpPath = options.dumpPath {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            if let data = try? encoder.encode(run) {
                try? data.write(to: URL(fileURLWithPath: dumpPath))
            }
        }
        if options.recordOnly { exit(0) }

        let report = AccessibilitySmokeJudge.report(run)
        print(report.text)
        exit(report.exitCode)
    }
}
