import XCTest
@testable import agentacct

final class SetupModelTests: XCTestCase {
    func testProcessRunnerStreamsOutputThenThrowsWhenProcessExitsNonzero() async {
        let stream = ProcessRunner.run(
            executable: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", "printf 'configuration failed\\n'; exit 7"]
        )
        var lines: [String] = []
        var receivedError: Error?

        do {
            for try await line in stream {
                lines.append(line)
            }
        } catch {
            receivedError = error
        }

        XCTAssertEqual(lines, ["configuration failed"])
        XCTAssertEqual(receivedError as? ProcessRunnerError, .nonzeroExit(7))
    }

    func testProcessRunnerThrowsWhenLaunchFailsWithoutInventingLogOutput() async {
        let stream = ProcessRunner.run(
            executable: URL(fileURLWithPath: "/path/that/does/not/exist/agentacct"),
            arguments: []
        )
        var lines: [String] = []
        var receivedError: Error?

        do {
            for try await line in stream {
                lines.append(line)
            }
        } catch {
            receivedError = error
        }

        XCTAssertTrue(lines.isEmpty)
        XCTAssertNotNil(receivedError)
    }

    func testProcessRunnerStreamsOutputAndCompletesForZeroExit() async throws {
        let stream = ProcessRunner.run(
            executable: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", "printf 'configured'"]
        )
        var lines: [String] = []

        for try await line in stream {
            lines.append(line)
        }

        XCTAssertEqual(lines, ["configured"])
    }

    @MainActor
    func testSetupModelFailsAfterStreamingNonzeroExit() async {
        let model = makeModel(lines: ["provider configured"], failure: ProcessRunnerError.nonzeroExit(9))

        await model.setUp()

        XCTAssertEqual(model.phase, .failed("Recorder setup exited with status 9."))
        XCTAssertEqual(
            model.log,
            [
                "Running: agentacct onboard --agent auto --yes",
                "provider configured",
                "error: Recorder setup exited with status 9.",
            ]
        )
    }

    @MainActor
    func testSetupModelFailsWhenLaunchThrows() async {
        let model = makeModel(failure: StubError.launchFailed)

        await model.setUp()

        XCTAssertEqual(model.phase, .failed("The recorder could not be launched."))
        XCTAssertEqual(
            model.log,
            [
                "Running: agentacct onboard --agent auto --yes",
                "error: The recorder could not be launched.",
            ]
        )
    }

    @MainActor
    func testSetupModelCompletesOnlyAfterSuccessfulStream() async {
        let model = makeModel(lines: ["provider configured"])

        await model.setUp()

        XCTAssertEqual(model.phase, .done)
        XCTAssertEqual(
            model.log,
            ["Running: agentacct onboard --agent auto --yes", "provider configured"]
        )
    }

    @MainActor
    func testSetupModelReturnsToIdleWhenProcessStreamIsCancelled() async {
        let model = makeModel(failure: CancellationError())

        await model.setUp()

        XCTAssertEqual(model.phase, .idle)
        XCTAssertEqual(model.log, ["Running: agentacct onboard --agent auto --yes"])
    }

    @MainActor
    private func makeModel(
        lines: [String] = [],
        failure: Error? = nil
    ) -> SetupModel {
        SetupModel(
            installer: { URL(fileURLWithPath: "/tmp/test-agentacct") },
            processRunner: { _, _ in
                AsyncThrowingStream { continuation in
                    for line in lines {
                        continuation.yield(line)
                    }
                    if let failure {
                        continuation.finish(throwing: failure)
                    } else {
                        continuation.finish()
                    }
                }
            }
        )
    }

    private enum StubError: LocalizedError {
        case launchFailed

        var errorDescription: String? {
            "The recorder could not be launched."
        }
    }
}
