import AppKit
import XCTest
@testable import agentacct

final class WorkSnapshotHarnessTests: XCTestCase {
    private struct ExpectedArtifact {
        let filename: String
        let pixelsWide: Int
        let pixelsHigh: Int
    }

    // Keep this contract independent of the production configuration. A
    // removed Work state, viewport, or appearance must fail review loudly.
    private let expectedArtifacts = [
        ExpectedArtifact(filename: "work-table-minimum-light.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "work-table-minimum-dark.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "work-table-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-table-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-minimum-light.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "work-receipt-minimum-dark.png", pixelsWide: 1920, pixelsHigh: 1120),
        ExpectedArtifact(filename: "work-receipt-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-list-loading-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-list-loading-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-empty-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-empty-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-list-error-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-list-error-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-retained-list-error-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-retained-list-error-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-attention-overflow-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-attention-overflow-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-loading-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-loading-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-error-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-error-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-stale-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-stale-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-attention-receipt-reference-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-attention-receipt-reference-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-table-accessibility-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-table-accessibility-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-accessibility-light.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-receipt-accessibility-dark.png", pixelsWide: 2240, pixelsHigh: 1600),
        ExpectedArtifact(filename: "work-table-accessibility-maximum-light.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-table-accessibility-maximum-dark.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-receipt-accessibility-maximum-light.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-receipt-accessibility-maximum-dark.png", pixelsWide: 2240, pixelsHigh: 2000),
        ExpectedArtifact(filename: "work-session-steps-hierarchy-light.png", pixelsWide: 1520, pixelsHigh: 2100),
        ExpectedArtifact(filename: "work-session-steps-hierarchy-dark.png", pixelsWide: 1520, pixelsHigh: 2100),
        ExpectedArtifact(filename: "work-session-steps-dense-checks-light.png", pixelsWide: 1520, pixelsHigh: 2400),
        ExpectedArtifact(filename: "work-session-steps-dense-checks-dark.png", pixelsWide: 1520, pixelsHigh: 2400),
        ExpectedArtifact(filename: "work-session-steps-expanded-current-light.png", pixelsWide: 1520, pixelsHigh: 5000),
        ExpectedArtifact(filename: "work-session-steps-expanded-current-dark.png", pixelsWide: 1520, pixelsHigh: 5000),
        ExpectedArtifact(filename: "work-session-steps-expanded-history-light.png", pixelsWide: 1520, pixelsHigh: 2900),
        ExpectedArtifact(filename: "work-session-steps-expanded-history-dark.png", pixelsWide: 1520, pixelsHigh: 2900),
        ExpectedArtifact(filename: "work-session-steps-load-failure-light.png", pixelsWide: 1520, pixelsHigh: 480),
        ExpectedArtifact(filename: "work-session-steps-load-failure-dark.png", pixelsWide: 1520, pixelsHigh: 480),
        ExpectedArtifact(filename: "work-session-steps-retrying-light.png", pixelsWide: 1520, pixelsHigh: 480),
        ExpectedArtifact(filename: "work-session-steps-retrying-dark.png", pixelsWide: 1520, pixelsHigh: 480),
        ExpectedArtifact(filename: "work-session-steps-compact-checks-light.png", pixelsWide: 720, pixelsHigh: 3200),
        ExpectedArtifact(filename: "work-session-steps-compact-checks-dark.png", pixelsWide: 720, pixelsHigh: 3200),
        ExpectedArtifact(filename: "work-session-steps-rtl-stress-light.png", pixelsWide: 1520, pixelsHigh: 2500),
        ExpectedArtifact(filename: "work-session-steps-rtl-stress-dark.png", pixelsWide: 1520, pixelsHigh: 2500),
        ExpectedArtifact(filename: "work-session-steps-compact-accessibility-light.png", pixelsWide: 720, pixelsHigh: 8000),
        ExpectedArtifact(filename: "work-session-steps-compact-accessibility-dark.png", pixelsWide: 720, pixelsHigh: 8000),
        ExpectedArtifact(filename: "work-session-steps-rtl-accessibility-light.png", pixelsWide: 720, pixelsHigh: 8000),
        ExpectedArtifact(filename: "work-session-steps-rtl-accessibility-dark.png", pixelsWide: 720, pixelsHigh: 8000),
        ExpectedArtifact(filename: "work-actions-exact-regular-light.png", pixelsWide: 1520, pixelsHigh: 1080),
        ExpectedArtifact(filename: "work-actions-exact-regular-dark.png", pixelsWide: 1520, pixelsHigh: 1080),
        ExpectedArtifact(filename: "work-actions-exact-compact-light.png", pixelsWide: 720, pixelsHigh: 1280),
        ExpectedArtifact(filename: "work-actions-exact-compact-dark.png", pixelsWide: 720, pixelsHigh: 1280),
        ExpectedArtifact(filename: "work-actions-semantic-gallery-light.png", pixelsWide: 1520, pixelsHigh: 3200),
        ExpectedArtifact(filename: "work-actions-semantic-gallery-dark.png", pixelsWide: 1520, pixelsHigh: 3200),
        ExpectedArtifact(filename: "work-actions-semantic-edge-cases-light.png", pixelsWide: 1520, pixelsHigh: 4120),
        ExpectedArtifact(filename: "work-actions-semantic-edge-cases-dark.png", pixelsWide: 1520, pixelsHigh: 4120),
        ExpectedArtifact(filename: "work-actions-layout-stress-light.png", pixelsWide: 1840, pixelsHigh: 1520),
        ExpectedArtifact(filename: "work-actions-layout-stress-dark.png", pixelsWide: 1840, pixelsHigh: 1520),
        ExpectedArtifact(filename: "work-actions-dynamic-type-stress-light.png", pixelsWide: 1840, pixelsHigh: 2700),
        ExpectedArtifact(filename: "work-actions-dynamic-type-stress-dark.png", pixelsWide: 1840, pixelsHigh: 2700),
        ExpectedArtifact(filename: "work-checks-overview-light.png", pixelsWide: 1520, pixelsHigh: 1360),
        ExpectedArtifact(filename: "work-checks-overview-dark.png", pixelsWide: 1520, pixelsHigh: 1360),
        ExpectedArtifact(filename: "work-checks-all-passed-light.png", pixelsWide: 1520, pixelsHigh: 920),
        ExpectedArtifact(filename: "work-checks-all-passed-dark.png", pixelsWide: 1520, pixelsHigh: 920),
        ExpectedArtifact(filename: "work-checks-expanded-light.png", pixelsWide: 1520, pixelsHigh: 2280),
        ExpectedArtifact(filename: "work-checks-expanded-dark.png", pixelsWide: 1520, pixelsHigh: 2280),
        ExpectedArtifact(filename: "work-checks-compact-light.png", pixelsWide: 720, pixelsHigh: 1440),
        ExpectedArtifact(filename: "work-checks-compact-dark.png", pixelsWide: 720, pixelsHigh: 1440),
        ExpectedArtifact(filename: "work-checks-compact-accessibility-light.png", pixelsWide: 720, pixelsHigh: 640),
        ExpectedArtifact(filename: "work-checks-compact-accessibility-dark.png", pixelsWide: 720, pixelsHigh: 640),
        ExpectedArtifact(filename: "work-checks-accessibility-rtl-light.png", pixelsWide: 1520, pixelsHigh: 1400),
        ExpectedArtifact(filename: "work-checks-accessibility-rtl-dark.png", pixelsWide: 1520, pixelsHigh: 1400),
    ]

    @MainActor
    func testFixturePreloadsRepresentativeWorkReceipt() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let work = try XCTUnwrap(fixture.work)
        let store = DashboardStore(preloaded: fixture)

        XCTAssertEqual(store.receipt?.taskId, work.receipt.taskId)
        XCTAssertEqual(work.attentionReceipt?.axes.decisionStatus.key, "blocked")
        XCTAssertNotNil(work.attentionReceipt?.axes.decisionStatus.blocker?.text)
        XCTAssertEqual(store.preloadedSessions.count, work.sessions.count)
        XCTAssertTrue(store.receiptTasks.contains { $0.decisionStatus.key == "blocked" })
        XCTAssertTrue(store.receiptTasks.contains { $0.decisionStatus.key == "finding" })
        XCTAssertGreaterThanOrEqual(work.receipt.dimensions.actions.touchedFileCount ?? 0, 12)
        XCTAssertEqual(work.receipt.dimensions.actions.toolCategoryTotal, 80)
        XCTAssertEqual(work.receipt.dimensions.actions.toolCategoryCounts?.values.reduce(0, +), 80)
        XCTAssertTrue(work.receipt.dimensions.actions.gaps?.isEmpty ?? true)
        XCTAssertEqual(work.receipt.dimensions.gaps.count, 2)
        XCTAssertGreaterThanOrEqual(work.receipt.dimensions.evidence.checks?.count ?? 0, 6)
        XCTAssertFalse(work.receipt.dimensions.provenance.sourcesPresent?.contains("ci") ?? false)
        XCTAssertGreaterThanOrEqual(work.receipt.sessions?.count ?? 0, 2)
    }

    func testSessionStepsFixtureUsesARealisticSupersessionAndSeparateResolution() throws {
        let referenceTimestamp: TimeInterval = 1_787_618_800
        let superseded = SessionStepsSnapshotScene.denseCheck(
            20,
            kind: .denseChecks,
            referenceTimestamp: referenceTimestamp
        )
        let supersedingPass = SessionStepsSnapshotScene.denseCheck(
            19,
            kind: .denseChecks,
            referenceTimestamp: referenceTimestamp
        )
        let blockerResolution = SessionStepsSnapshotScene.denseCheck(
            18,
            kind: .denseChecks,
            referenceTimestamp: referenceTimestamp
        )

        XCTAssertEqual(superseded.result, "failed")
        XCTAssertEqual(superseded.supersessionState, "superseded")
        XCTAssertEqual(superseded.supersededByEventId, supersedingPass.eventId)
        XCTAssertEqual(supersedingPass.result, "passed")
        XCTAssertGreaterThan(
            try XCTUnwrap(supersedingPass.createdAt),
            try XCTUnwrap(superseded.createdAt)
        )
        XCTAssertEqual(supersedingPass.evidenceType, superseded.evidenceType)
        XCTAssertEqual(supersedingPass.checkIdentity, superseded.checkIdentity)
        XCTAssertEqual(
            superseded.summary,
            "An earlier artifact verification failed before a later matching rerun passed."
        )
        XCTAssertEqual(
            supersedingPass.summary,
            "A later matching artifact verification passed and superseded the earlier failure."
        )
        XCTAssertNil(superseded.resolutionScope)
        XCTAssertNil(supersedingPass.resolutionScope)

        XCTAssertEqual(blockerResolution.result, "passed")
        XCTAssertEqual(blockerResolution.resolutionScope, "partial")
        XCTAssertNotNil(blockerResolution.resolvesBlockedEventId)
        XCTAssertNotEqual(blockerResolution.checkIdentity, superseded.checkIdentity)
    }

    @MainActor
    func testInitialWorkStatesDoNotInventReceiptFreshness() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())

        XCTAssertNil(
            DashboardStore(preloaded: fixture, workState: .listLoading).receiptListLastUpdated
        )
        XCTAssertNil(
            DashboardStore(preloaded: fixture, workState: .listError).receiptListLastUpdated
        )
        XCTAssertNotNil(
            DashboardStore(preloaded: fixture, workState: .populated).receiptListLastUpdated
        )
        XCTAssertNotNil(
            DashboardStore(preloaded: fixture, workState: .empty).receiptListLastUpdated
        )
    }

    @MainActor
    func testTransientTableStatesKeepAttentionEvidenceConsistent() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let loading = DashboardStore(preloaded: fixture, workState: .listLoading)
        let empty = DashboardStore(preloaded: fixture, workState: .empty)
        let listError = DashboardStore(preloaded: fixture, workState: .listError)

        XCTAssertFalse(loading.hasLoadedReceiptTasks)
        XCTAssertTrue(loading.receiptTasks.isEmpty)
        XCTAssertNil(loading.receiptListError)

        XCTAssertEqual(empty.totalReceiptTasks, 0)
        XCTAssertEqual(empty.attention?.total, 0)
        XCTAssertEqual(empty.attention?.items.count, 0)

        XCTAssertNotNil(listError.receiptListError)
        XCTAssertEqual(listError.attention?.total, fixture.attention.total)
        XCTAssertEqual(listError.attention?.items.map(\.taskId), fixture.attention.items.map(\.taskId))
    }

    @MainActor
    func testRendersEveryWorkReviewConfigurationDeterministically() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let firstDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-work-snapshots-\(UUID().uuidString)")
        let secondDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-work-snapshots-\(UUID().uuidString)")
        defer {
            try? FileManager.default.removeItem(at: firstDirectory)
            try? FileManager.default.removeItem(at: secondDirectory)
        }

        let workRendered: [URL]
        let sessionStepsRendered: [URL]
        let actionRendered: [URL]
        let checkRendered: [URL]
        do {
            workRendered = try WorkSnapshotRenderer.render(
                fixture: fixture,
                outputDirectory: firstDirectory
            )
        } catch {
            XCTFail("First full-page Work render failed: \(error)")
            throw error
        }
        do {
            sessionStepsRendered = try SessionStepsSnapshotRenderer.render(
                fixture: fixture,
                outputDirectory: firstDirectory
            )
        } catch {
            XCTFail("First focused Session and steps render failed: \(error)")
            throw error
        }
        do {
            actionRendered = try ReceiptActionSnapshotRenderer.render(
                outputDirectory: firstDirectory
            )
        } catch {
            XCTFail("First focused Action render failed: \(error)")
            throw error
        }
        do {
            checkRendered = try ReceiptCheckSnapshotRenderer.render(
                outputDirectory: firstDirectory
            )
        } catch {
            XCTFail("First focused Checks render failed: \(error)")
            throw error
        }
        do {
            _ = try WorkSnapshotRenderer.render(
                fixture: fixture,
                outputDirectory: secondDirectory
            )
        } catch {
            XCTFail("Second full-page Work render failed: \(error)")
            throw error
        }
        do {
            _ = try SessionStepsSnapshotRenderer.render(
                fixture: fixture,
                outputDirectory: secondDirectory
            )
        } catch {
            XCTFail("Second focused Session and steps render failed: \(error)")
            throw error
        }
        do {
            _ = try ReceiptActionSnapshotRenderer.render(
                outputDirectory: secondDirectory
            )
        } catch {
            XCTFail("Second focused Action render failed: \(error)")
            throw error
        }
        do {
            _ = try ReceiptCheckSnapshotRenderer.render(
                outputDirectory: secondDirectory
            )
        } catch {
            XCTFail("Second focused Checks render failed: \(error)")
            throw error
        }
        let rendered = workRendered + sessionStepsRendered + actionRendered + checkRendered

        XCTAssertEqual(rendered.map(\.lastPathComponent), expectedArtifacts.map(\.filename))
        for artifact in expectedArtifacts {
            let imageURL = firstDirectory.appendingPathComponent(artifact.filename)
            let image = try XCTUnwrap(NSImage(contentsOf: imageURL), artifact.filename)
            let representation = try XCTUnwrap(image.representations.first, artifact.filename)
            XCTAssertEqual(representation.pixelsWide, artifact.pixelsWide)
            XCTAssertEqual(representation.pixelsHigh, artifact.pixelsHigh)

            let difference = try VisualSnapshotHarness.compare(
                expectedURL: imageURL,
                actualURL: secondDirectory.appendingPathComponent(artifact.filename)
            )
            XCTAssertLessThanOrEqual(
                difference.maximumChannelDelta,
                VisualSnapshotTolerance.renderingNoise.maximumChannelDelta
            )
            XCTAssertLessThanOrEqual(
                difference.changedChannelFraction,
                VisualSnapshotTolerance.renderingNoise.maximumChangedChannelFraction
            )
        }

        let appearanceDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("work-receipt-reference-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-receipt-reference-dark.png")
        )
        XCTAssertGreaterThan(appearanceDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(appearanceDifference.changedPixelFraction, 0.05)

        let stateDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("work-table-reference-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-receipt-reference-light.png")
        )
        XCTAssertGreaterThan(stateDifference.maximumChannelDelta, 32)
        XCTAssertGreaterThan(stateDifference.changedPixelFraction, 0.05)

        let transientDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("work-receipt-loading-reference-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-receipt-error-reference-light.png")
        )
        XCTAssertGreaterThan(
            transientDifference.maximumChannelDelta,
            32,
            "Loading and error states must never collapse to the same renderer placeholder"
        )
        XCTAssertGreaterThan(
            transientDifference.changedPixelFraction,
            0.001,
            "Loading and error states must remain visually distinguishable"
        )
        let initialStateDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("work-list-loading-reference-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-empty-reference-light.png")
        )
        XCTAssertGreaterThan(initialStateDifference.changedPixelFraction, 0.0005)

        let decisionDifference = try VisualSnapshotHarness.compare(
            expectedURL: firstDirectory.appendingPathComponent("work-receipt-reference-light.png"),
            actualURL: firstDirectory.appendingPathComponent("work-attention-receipt-reference-light.png")
        )
        XCTAssertGreaterThan(decisionDifference.changedPixelFraction, 0.01)
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertFalse(SnapshotMode.boundsScrollContentToViewport)
        XCTAssertNil(SnapshotScheme.override)
    }

    @MainActor
    func testFocusedActionRendererRejectsAClippedCanvas() throws {
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-clipped-action-snapshot-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: outputDirectory) }
        let configuration = ReceiptActionSnapshotConfiguration(
            kind: .exactCompact,
            width: 360,
            height: 100,
            colorScheme: .light
        )

        XCTAssertThrowsError(
            try ReceiptActionSnapshotRenderer.render(
                outputDirectory: outputDirectory,
                configurations: [configuration]
            )
        ) { error in
            guard case SnapshotError.snapshotContentExceedsCanvas(
                let filename,
                let requiredHeight,
                let availableHeight
            ) = error else {
                return XCTFail("Expected clipped-canvas failure; got \(error)")
            }
            XCTAssertEqual(filename, configuration.filename)
            XCTAssertGreaterThan(requiredHeight, availableHeight)
            XCTAssertEqual(availableHeight, 100)
        }
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertNil(SnapshotScheme.override)
    }

    @MainActor
    func testFocusedSessionStepsRendererRejectsAClippedCanvas() throws {
        let fixture = try DashboardSnapshotFixture.load(from: fixtureURL())
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-clipped-session-steps-snapshot-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: outputDirectory) }
        let configuration = SessionStepsSnapshotConfiguration(
            kind: .denseChecks,
            width: 760,
            height: 100,
            colorScheme: .light
        )

        XCTAssertThrowsError(
            try SessionStepsSnapshotRenderer.render(
                fixture: fixture,
                outputDirectory: outputDirectory,
                configurations: [configuration]
            )
        ) { error in
            guard case SnapshotError.snapshotContentExceedsCanvas(
                let filename,
                let requiredHeight,
                let availableHeight
            ) = error else {
                return XCTFail("Expected clipped-canvas failure; got \(error)")
            }
            XCTAssertEqual(filename, configuration.filename)
            XCTAssertGreaterThan(requiredHeight, availableHeight)
            XCTAssertEqual(availableHeight, 100)
        }
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertNil(SnapshotScheme.override)
    }

    @MainActor
    func testFocusedActionReviewConfigurationsFitTheirCanvases() throws {
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-action-snapshot-matrix-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let rendered = try ReceiptActionSnapshotRenderer.render(outputDirectory: outputDirectory)
        let expected = expectedArtifacts.filter { $0.filename.hasPrefix("work-actions-") }
        XCTAssertEqual(rendered.map(\.lastPathComponent), expected.map(\.filename))
        for artifact in expected {
            let image = try XCTUnwrap(
                NSImage(contentsOf: outputDirectory.appendingPathComponent(artifact.filename)),
                artifact.filename
            )
            let representation = try XCTUnwrap(image.representations.first, artifact.filename)
            XCTAssertEqual(representation.pixelsWide, artifact.pixelsWide)
            XCTAssertEqual(representation.pixelsHigh, artifact.pixelsHigh)
        }
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertNil(SnapshotScheme.override)
    }

    @MainActor
    func testFocusedChecksRendererRejectsAClippedCanvas() throws {
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-clipped-checks-snapshot-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: outputDirectory) }
        let configuration = ReceiptCheckSnapshotConfiguration(
            kind: .compact,
            width: 360,
            height: 100,
            colorScheme: .light,
            dynamicTypeSize: .medium,
            layoutDirection: .leftToRight
        )

        XCTAssertThrowsError(
            try ReceiptCheckSnapshotRenderer.render(
                outputDirectory: outputDirectory,
                configurations: [configuration]
            )
        ) { error in
            guard case SnapshotError.snapshotContentExceedsCanvas(
                let filename,
                let requiredHeight,
                let availableHeight
            ) = error else {
                return XCTFail("Expected clipped-canvas failure; got \(error)")
            }
            XCTAssertEqual(filename, configuration.filename)
            XCTAssertGreaterThan(requiredHeight, availableHeight)
            XCTAssertEqual(availableHeight, 100)
        }
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertNil(SnapshotScheme.override)
    }

    @MainActor
    func testFocusedChecksReviewConfigurationsFitTheirCanvases() throws {
        let outputDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("agentacct-checks-snapshot-matrix-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: outputDirectory) }

        let rendered = try ReceiptCheckSnapshotRenderer.render(outputDirectory: outputDirectory)
        let expected = expectedArtifacts.filter { $0.filename.hasPrefix("work-checks-") }
        XCTAssertEqual(rendered.map(\.lastPathComponent), expected.map(\.filename))
        for artifact in expected {
            let image = try XCTUnwrap(
                NSImage(contentsOf: outputDirectory.appendingPathComponent(artifact.filename)),
                artifact.filename
            )
            let representation = try XCTUnwrap(image.representations.first, artifact.filename)
            XCTAssertEqual(representation.pixelsWide, artifact.pixelsWide)
            XCTAssertEqual(representation.pixelsHigh, artifact.pixelsHigh)
        }
        XCTAssertFalse(SnapshotMode.enabled)
        XCTAssertNil(SnapshotScheme.override)
    }

    func testRejectsUnsupportedWorkPayloadSchemas() throws {
        let validFixture = try String(contentsOf: fixtureURL(), encoding: .utf8)
        let invalidReceipt = validFixture.replacingOccurrences(
            of: "\"schema_version\": \"agentacct.receipt.v1\"",
            with: "\"schema_version\": \"agentacct.receipt.v999\""
        )
        XCTAssertThrowsError(try DashboardSnapshotFixture.decode(Data(invalidReceipt.utf8))) { error in
            guard case SnapshotError.unsupportedSchema(let payload, let actual, let expected) = error else {
                return XCTFail("Expected unsupported Work receipt schema; got \(error)")
            }
            XCTAssertEqual(payload, "work receipt")
            XCTAssertEqual(actual, "agentacct.receipt.v999")
            XCTAssertEqual(expected, DashboardSnapshotFixture.supportedTasksSchema)
        }

        let invalidSession = validFixture.replacingOccurrences(
            of: "\"schema\": \"agentacct.v1-session-detail.v1\"",
            with: "\"schema\": \"agentacct.v1-session-detail.v999\""
        )
        XCTAssertThrowsError(try DashboardSnapshotFixture.decode(Data(invalidSession.utf8))) { error in
            guard case SnapshotError.unsupportedSchema(let payload, let actual, let expected) = error else {
                return XCTFail("Expected unsupported Work session schema; got \(error)")
            }
            XCTAssertEqual(payload, "work session")
            XCTAssertEqual(actual, "agentacct.v1-session-detail.v999")
            XCTAssertEqual(expected, WorkSnapshotFixture.supportedSessionSchema)
        }
    }

    @MainActor
    func testRendererRejectsFixtureWithoutWorkPayload() throws {
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL())) as? [String: Any]
        )
        var withoutWork = object
        withoutWork.removeValue(forKey: "work")
        let fixture = try DashboardSnapshotFixture.decode(
            JSONSerialization.data(withJSONObject: withoutWork)
        )

        XCTAssertThrowsError(
            try WorkSnapshotRenderer.render(
                fixture: fixture,
                outputDirectory: FileManager.default.temporaryDirectory
            )
        ) { error in
            guard case SnapshotError.missingWorkFixture = error else {
                return XCTFail("Expected missing Work fixture error; got \(error)")
            }
        }
    }

    private func fixtureURL() throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: "dashboard", withExtension: "json"))
    }
}
