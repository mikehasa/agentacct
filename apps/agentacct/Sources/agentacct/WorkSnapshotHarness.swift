import SwiftUI

enum WorkSnapshotState: String {
    case table
    case receipt
    case attentionReceipt = "attention-receipt"
    case listLoading = "list-loading"
    case empty
    case listError = "list-error"
    case receiptLoading = "receipt-loading"
    case receiptError = "receipt-error"
    case receiptStale = "receipt-stale"

    var storeState: SnapshotWorkStoreState {
        switch self {
        case .table, .receipt: return .populated
        case .attentionReceipt: return .attentionReceipt
        case .listLoading: return .listLoading
        case .empty: return .empty
        case .listError: return .listError
        case .receiptLoading: return .receiptLoading
        case .receiptError: return .receiptError
        case .receiptStale: return .receiptStale
        }
    }

    var selectsReceipt: Bool {
        switch self {
        case .receipt, .attentionReceipt, .receiptLoading, .receiptError, .receiptStale: return true
        case .table, .listLoading, .empty, .listError: return false
        }
    }
}

struct WorkSnapshotConfiguration {
    let state: WorkSnapshotState
    let viewport: String
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "work-\(state.rawValue)-\(viewport)-\(appearance).png"
    }

    var dynamicTypeSize: DynamicTypeSize {
        switch viewport {
        case "accessibility": return .accessibility1
        case "accessibility-maximum": return .accessibility5
        default: return .medium
        }
    }

    static let reviewConfigurations: [Self] = {
        let core = [WorkSnapshotState.table, .receipt].flatMap { state in
            [
                Self(state: state, viewport: "minimum", width: 960, height: 560, colorScheme: .light),
                Self(state: state, viewport: "minimum", width: 960, height: 560, colorScheme: .dark),
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .light),
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .dark),
            ]
        }
        let transient = [
            WorkSnapshotState.listLoading,
            WorkSnapshotState.empty,
            .listError,
            .receiptLoading,
            .receiptError,
            .receiptStale,
            .attentionReceipt,
        ].flatMap { state in
            [
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .light),
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .dark),
            ]
        }
        let accessibility = [WorkSnapshotState.table, .receipt].flatMap { state in
            [
                Self(state: state, viewport: "accessibility", width: 1120, height: 800, colorScheme: .light),
                Self(state: state, viewport: "accessibility", width: 1120, height: 800, colorScheme: .dark),
            ]
        }
        let accessibilityMaximum = [WorkSnapshotState.table, .receipt].flatMap { state in
            [
                Self(state: state, viewport: "accessibility-maximum", width: 1120, height: 1000, colorScheme: .light),
                Self(state: state, viewport: "accessibility-maximum", width: 1120, height: 1000, colorScheme: .dark),
            ]
        }
        return core + transient + accessibility + accessibilityMaximum
    }()
}

enum WorkSnapshotRenderer {
    private static let snapshotLocale = Locale(identifier: "en_US_POSIX")
    private static let snapshotTimeZone = TimeZone(secondsFromGMT: 0)!

    private static var snapshotCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = snapshotLocale
        calendar.timeZone = snapshotTimeZone
        return calendar
    }

    @MainActor
    static func render(
        fixture: DashboardSnapshotFixture,
        outputDirectory: URL,
        configurations: [WorkSnapshotConfiguration] = WorkSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        guard let generatedAt = fixture.glance.generatedAt else {
            throw SnapshotError.missingFixtureDate
        }
        guard let work = fixture.work else {
            throw SnapshotError.missingWorkFixture
        }
        try FileManager.default.createDirectory(
            at: outputDirectory,
            withIntermediateDirectories: true
        )

        SnapshotMode.enabled = true
        SnapshotMode.boundsScrollContentToViewport = true
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: generatedAt))
        defer {
            SnapshotMode.enabled = false
            SnapshotMode.boundsScrollContentToViewport = false
            SnapshotMode.setFixtureDate(nil)
            SnapshotScheme.override = nil
        }

        let glance = GlanceState(preloaded: fixture.glanceSnapshot)
        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            let dashboard = DashboardStore(
                preloaded: fixture,
                workState: configuration.state.storeState
            )
            let selection = AppSelection()
            selection.pane = .work
            if configuration.state == .attentionReceipt {
                selection.taskId = work.attentionReceipt?.taskId
            } else {
                selection.taskId = configuration.state.selectsReceipt ? work.receipt.taskId : nil
            }

            let view = MainWindow(canSetUpOverride: true)
                .environment(glance)
                .environment(dashboard)
                .environment(selection)
                .frame(
                    width: configuration.width,
                    height: configuration.height,
                    alignment: .top
                )
                .clipped()
                .environment(\.colorScheme, configuration.colorScheme)
                .environment(\.locale, snapshotLocale)
                .environment(\.calendar, snapshotCalendar)
                .environment(\.timeZone, snapshotTimeZone)
                .environment(\.displayScale, 2)
                .environment(\.layoutDirection, .leftToRight)
                .environment(\.dynamicTypeSize, configuration.dynamicTypeSize)
                .environment(\.controlSize, .regular)
                .environment(\.legibilityWeight, nil)
                .environment(\.appearsActive, true)
                .transaction { transaction in
                    transaction.disablesAnimations = true
                }
            let outputURL = outputDirectory.appendingPathComponent(configuration.filename)
            try SnapshotImageWriter.render(
                view,
                to: outputURL,
                size: CGSize(width: configuration.width, height: configuration.height)
            )
            return outputURL
        }
    }
}

enum ReceiptActionSnapshotKind: String {
    case exactRegular = "exact-regular"
    case exactCompact = "exact-compact"
    case semanticGallery = "semantic-gallery"
    case semanticEdgeCases = "semantic-edge-cases"
    case layoutStress = "layout-stress"
    case dynamicTypeStress = "dynamic-type-stress"
}

struct ReceiptActionSnapshotConfiguration {
    let kind: ReceiptActionSnapshotKind
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "work-actions-\(kind.rawValue)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        (.exactRegular, 760, 540),
        (.exactCompact, 360, 640),
        (.semanticGallery, 760, 1600),
        (.semanticEdgeCases, 760, 2060),
        (.layoutStress, 920, 760),
        (.dynamicTypeStress, 920, 1350),
    ].flatMap { kind, width, height in
        [
            Self(kind: kind, width: width, height: height, colorScheme: .light),
            Self(kind: kind, width: width, height: height, colorScheme: .dark),
        ]
    }
}

enum ReceiptActionSnapshotRenderer {
    @MainActor
    static func render(
        outputDirectory: URL,
        configurations: [ReceiptActionSnapshotConfiguration] = ReceiptActionSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        try FileManager.default.createDirectory(
            at: outputDirectory,
            withIntermediateDirectories: true
        )

        SnapshotMode.enabled = true
        defer {
            SnapshotMode.enabled = false
            SnapshotScheme.override = nil
        }

        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            let unconstrainedScene = ReceiptActionSnapshotScene(kind: configuration.kind)
                .frame(width: configuration.width, alignment: .topLeading)
                .fixedSize(horizontal: false, vertical: true)
                .modifier(ReceiptActionSnapshotEnvironment(configuration: configuration))
            let requiredSize = try SnapshotImageWriter.renderedSize(
                unconstrainedScene,
                proposedSize: ProposedViewSize(width: configuration.width, height: nil)
            )
            guard requiredSize.height <= configuration.height else {
                throw SnapshotError.snapshotContentExceedsCanvas(
                    filename: configuration.filename,
                    requiredHeight: Int(ceil(requiredSize.height)),
                    availableHeight: Int(configuration.height)
                )
            }

            let scene = ReceiptActionSnapshotScene(kind: configuration.kind)
                .frame(
                    width: configuration.width,
                    height: configuration.height,
                    alignment: .topLeading
                )
                .clipped()
                .modifier(ReceiptActionSnapshotEnvironment(configuration: configuration))
            let outputURL = outputDirectory.appendingPathComponent(configuration.filename)
            try SnapshotImageWriter.render(
                scene,
                to: outputURL,
                size: CGSize(width: configuration.width, height: configuration.height)
            )
            return outputURL
        }
    }
}

private struct ReceiptActionSnapshotEnvironment: ViewModifier {
    let configuration: ReceiptActionSnapshotConfiguration

    func body(content: Content) -> some View {
        content
            .background(Theme.canvas)
            .environment(\.colorScheme, configuration.colorScheme)
            .environment(\.locale, Locale(identifier: "en_US_POSIX"))
            .environment(\.displayScale, 2)
            .environment(\.layoutDirection, .leftToRight)
            .environment(\.dynamicTypeSize, .medium)
            .environment(\.controlSize, .regular)
            .environment(\.legibilityWeight, nil)
            .environment(\.appearsActive, true)
            .transaction { transaction in
                transaction.disablesAnimations = true
            }
    }
}

private struct ReceiptActionSnapshotScene: View {
    let kind: ReceiptActionSnapshotKind

    var body: some View {
        Group {
            switch kind {
            case .exactRegular, .exactCompact:
                exactCard
            case .semanticGallery:
                semanticGallery
            case .semanticEdgeCases:
                semanticEdgeCaseGallery
            case .layoutStress:
                layoutStressGallery
            case .dynamicTypeStress:
                dynamicTypeStressGallery
            }
        }
        .padding(Space.l)
    }

    private var exactSynopsis: ReceiptActionSynopsis {
        receiptActionSynopsis(
            counts: ["edit": 7, "execute": 24, "read": 38, "search": 11],
            storedTotal: 80
        )
    }

    private var exactCard: some View {
        Card(padding: Space.xl) {
            ReceiptActionsDigest(
                synopsis: exactSynopsis,
                relatedPathCount: 18,
                provenance: ["client_log"],
                gaps: []
            )
        }
    }

    private var semanticGallery: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            snapshotHeading("Action Digest · core semantic states")
            ForEach(Array(semanticExamples.prefix(5))) { example in
                actionCard(example)
            }
        }
    }

    private var semanticEdgeCaseGallery: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            snapshotHeading("Action Digest · edge semantic states")
            ForEach(Array(semanticExamples.dropFirst(5))) { example in
                actionCard(example)
            }
        }
    }

    private var layoutStressGallery: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            snapshotHeading("Action Digest · layout stress")
            HStack(alignment: .top, spacing: Space.l) {
                stressCard(title: "320 pt · long translated content") {
                    ReceiptActionsDigest(
                        synopsis: germanSynopsis,
                        relatedPathCount: 1_234,
                        provenance: ["lokales_client_protokoll_mit_langer_bezeichnung"],
                        gaps: ["Die Abdeckung einzelner Aktionen ist unvollständig und kann nicht rekonstruiert werden."]
                    )
                    .frame(width: 320)
                }
                stressCard(title: "560 pt · RTL") {
                    ReceiptActionsDigest(
                        synopsis: exactSynopsis,
                        relatedPathCount: 18,
                        provenance: ["سجل_العميل"],
                        gaps: ["تفاصيل كل إجراء غير مسجلة"]
                    )
                    .frame(width: 500)
                    .environment(\.layoutDirection, .rightToLeft)
                }
            }
        }
    }

    private var dynamicTypeStressGallery: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            snapshotHeading("Action Digest · accessibility type and taxonomy stress")
            stressCard(title: "360 pt · accessibility 3 · RTL · exact totals") {
                ReceiptActionsDigest(
                    synopsis: exactSynopsis,
                    relatedPathCount: 18,
                    provenance: ["transcript_scan", "mcp"],
                    gaps: []
                )
                .frame(width: 360)
                .environment(\.layoutDirection, .rightToLeft)
                .environment(\.dynamicTypeSize, .accessibility3)
            }
            stressCard(title: "840 pt · accessibility 3 · maximum taxonomy") {
                ReceiptActionsDigest(
                    synopsis: receiptActionSynopsis(
                        counts: [
                            "read": 38,
                            "edit": 7,
                            "execute": 24,
                            "search": 11,
                            "network": 5,
                            "agent": 4,
                            "plan": 3,
                            "mcp": 2,
                            "other": 1,
                            "future_category": 6,
                        ],
                        storedTotal: 101
                    ),
                    relatedPathCount: 18,
                    provenance: ["client_log"],
                    gaps: []
                )
                .frame(width: 840)
                .environment(\.dynamicTypeSize, .accessibility3)
            }
        }
    }

    private var semanticExamples: [ReceiptActionSnapshotExample] {
        var highCardinality: [String: Int] = ["read": 20, "edit": 5]
        for index in 1...40 { highCardinality["plugin_type_\(index)"] = 1 }
        return [
            .init(
                id: "absent", title: "Absent instrumentation",
                synopsis: receiptActionSynopsis(counts: nil, storedTotal: nil),
                pathCount: nil, provenance: nil, gaps: []
            ),
            .init(
                id: "zero", title: "Zero records · capture unknown",
                synopsis: receiptActionSynopsis(counts: [:], storedTotal: 0),
                pathCount: nil, provenance: nil,
                gaps: ["Tool categories were not instrumented for this session."]
            ),
            .init(
                id: "total-only", title: "Total only",
                synopsis: receiptActionSynopsis(counts: nil, storedTotal: 80),
                pathCount: 18, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "exact", title: "Exact reconciliation",
                synopsis: exactSynopsis,
                pathCount: 18, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "partial", title: "Conflicting total · stored total higher",
                synopsis: receiptActionSynopsis(counts: ["read": 7], storedTotal: 10),
                pathCount: 2, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "mismatch", title: "Conflicting total · categorized higher",
                synopsis: receiptActionSynopsis(counts: ["read": 8, "edit": 4], storedTotal: 10),
                pathCount: 4, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "invalid", title: "Invalid category",
                synopsis: receiptActionSynopsis(counts: ["read": 8, "edit": -2], storedTotal: 8),
                pathCount: 1, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "legacy", title: "Stored total unavailable",
                synopsis: receiptActionSynopsis(counts: ["read": 3, "execute": 2], storedTotal: nil),
                pathCount: 2, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "high-cardinality", title: "High-cardinality future taxonomy",
                synopsis: receiptActionSynopsis(counts: highCardinality, storedTotal: 65),
                pathCount: 7, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "redacted", title: "Privacy-redacted aggregate",
                synopsis: exactSynopsis,
                pathCount: 18, provenance: ["client_log"],
                gaps: ["Sensitive action details redacted"]
            ),
        ]
    }

    private var germanSynopsis: ReceiptActionSynopsis {
        ReceiptActionSynopsis(
            integrity: .exact,
            metrics: [
                ReceiptActionMetric(
                    key: "read", label: "Lesen",
                    detail: "Werkzeugaufrufe zum Lesen von Dateien oder zusätzlichem Kontext",
                    count: 38
                ),
                ReceiptActionMetric(
                    key: "execute", label: "Ausführen",
                    detail: "Werkzeugaufrufe zum Starten von Befehlen oder Prozessen",
                    count: 24
                ),
                ReceiptActionMetric(
                    key: "other", label: "Sonstige",
                    detail: "Werkzeugaufrufe außerhalb der benannten Kategorien",
                    count: 4
                ),
            ],
            headline: "66 erfasste Werkzeugaufrufe",
            integrityDetail: nil,
            storedTotal: 66,
            categorizedTotal: 66,
            shareDenominator: 66,
            captureBoundary: "Kein geordnetes Aktionsprotokoll; die erfassten Werkzeugaufrufzahlen lassen sich nicht mit Ergebnissen oder Zeitangaben verknüpfen."
        )
    }

    private func actionCard(_ example: ReceiptActionSnapshotExample) -> some View {
        Card(padding: Space.l) {
            VStack(alignment: .leading, spacing: Space.s) {
                CapsLabel(text: example.title)
                Rectangle().fill(Theme.hairline).frame(height: 1)
                ReceiptActionsDigest(
                    synopsis: example.synopsis,
                    relatedPathCount: example.pathCount,
                    provenance: example.provenance,
                    gaps: example.gaps
                )
            }
        }
    }

    private func stressCard<Content: View>(
        title: String,
        @ViewBuilder content: @escaping () -> Content
    ) -> some View {
        Card(padding: Space.l) {
            VStack(alignment: .leading, spacing: Space.s) {
                CapsLabel(text: title)
                Rectangle().fill(Theme.hairline).frame(height: 1)
                content()
            }
        }
    }

    private func snapshotHeading(_ title: String) -> some View {
        Text(title)
            .font(Type.titleCard)
            .foregroundStyle(Theme.ink)
    }
}

private struct ReceiptActionSnapshotExample: Identifiable {
    let id: String
    let title: String
    let synopsis: ReceiptActionSynopsis
    let pathCount: Int?
    let provenance: [String]?
    let gaps: [String]?
}

enum ReceiptCheckSnapshotKind: String {
    case overview
    case allPassed = "all-passed"
    case expanded
    case compact
    case compactAccessibility = "compact-accessibility"
    case accessibilityRTL = "accessibility-rtl"
}

struct ReceiptCheckSnapshotConfiguration {
    let kind: ReceiptCheckSnapshotKind
    let width: CGFloat
    let height: CGFloat
    let colorScheme: ColorScheme
    let dynamicTypeSize: DynamicTypeSize
    let layoutDirection: LayoutDirection

    var filename: String {
        let appearance = colorScheme == .dark ? "dark" : "light"
        return "work-checks-\(kind.rawValue)-\(appearance).png"
    }

    static let reviewConfigurations: [Self] = [
        (.overview, 760, 680, DynamicTypeSize.medium, LayoutDirection.leftToRight),
        (.allPassed, 760, 460, .medium, .leftToRight),
        (.expanded, 760, 1_140, .medium, .leftToRight),
        (.compact, 360, 720, .medium, .leftToRight),
        (.compactAccessibility, 360, 320, .accessibility5, .leftToRight),
        (.accessibilityRTL, 760, 700, .accessibility5, .rightToLeft),
    ].flatMap { kind, width, height, dynamicTypeSize, layoutDirection in
        [
            Self(
                kind: kind, width: width, height: height, colorScheme: .light,
                dynamicTypeSize: dynamicTypeSize, layoutDirection: layoutDirection
            ),
            Self(
                kind: kind, width: width, height: height, colorScheme: .dark,
                dynamicTypeSize: dynamicTypeSize, layoutDirection: layoutDirection
            ),
        ]
    }
}

enum ReceiptCheckSnapshotRenderer {
    @MainActor
    static func render(
        outputDirectory: URL,
        configurations: [ReceiptCheckSnapshotConfiguration] = ReceiptCheckSnapshotConfiguration.reviewConfigurations
    ) throws -> [URL] {
        try FileManager.default.createDirectory(
            at: outputDirectory,
            withIntermediateDirectories: true
        )

        SnapshotMode.enabled = true
        SnapshotMode.setFixtureDate(Date(timeIntervalSince1970: 1_787_590_000))
        defer {
            SnapshotMode.enabled = false
            SnapshotMode.setFixtureDate(nil)
            SnapshotScheme.override = nil
        }

        return try configurations.map { configuration in
            SnapshotScheme.override = configuration.colorScheme
            let unconstrainedScene = ReceiptCheckSnapshotScene(kind: configuration.kind)
                .frame(width: configuration.width, alignment: .topLeading)
                .fixedSize(horizontal: false, vertical: true)
                .modifier(ReceiptCheckSnapshotEnvironment(configuration: configuration))
            let requiredSize = try SnapshotImageWriter.renderedSize(
                unconstrainedScene,
                proposedSize: ProposedViewSize(width: configuration.width, height: nil)
            )
            guard requiredSize.height <= configuration.height else {
                throw SnapshotError.snapshotContentExceedsCanvas(
                    filename: configuration.filename,
                    requiredHeight: Int(ceil(requiredSize.height)),
                    availableHeight: Int(configuration.height)
                )
            }

            let scene = ReceiptCheckSnapshotScene(kind: configuration.kind)
                .frame(
                    width: configuration.width,
                    height: configuration.height,
                    alignment: .topLeading
                )
                .clipped()
                .modifier(ReceiptCheckSnapshotEnvironment(configuration: configuration))
            let outputURL = outputDirectory.appendingPathComponent(configuration.filename)
            try SnapshotImageWriter.render(
                scene,
                to: outputURL,
                size: CGSize(width: configuration.width, height: configuration.height)
            )
            return outputURL
        }
    }
}

private struct ReceiptCheckSnapshotEnvironment: ViewModifier {
    let configuration: ReceiptCheckSnapshotConfiguration

    func body(content: Content) -> some View {
        content
            .background(Theme.canvas)
            .environment(\.colorScheme, configuration.colorScheme)
            .environment(\.locale, Locale(identifier: "en_US_POSIX"))
            .environment(\.calendar, Calendar(identifier: .gregorian))
            .environment(\.timeZone, TimeZone(secondsFromGMT: 0)!)
            .environment(\.displayScale, 2)
            .environment(\.layoutDirection, configuration.layoutDirection)
            .environment(\.dynamicTypeSize, configuration.dynamicTypeSize)
            .environment(\.controlSize, .regular)
            .environment(\.legibilityWeight, nil)
            .environment(\.appearsActive, true)
            .transaction { transaction in
                transaction.disablesAnimations = true
            }
    }
}

private struct ReceiptCheckSnapshotScene: View {
    let kind: ReceiptCheckSnapshotKind

    var body: some View {
        let evidence = evidence(for: kind)
        let collection = ReceiptCheckCollectionPresentation(evidence: evidence)
        RecordChecksCard(
            evidence: evidence,
            taskId: "checks-snapshot",
            initiallyShowsRoutineGroups: kind == .overview ? false : true,
            initiallyExpandedCheckIDs: kind == .expanded
                ? Set(collection.rows(in: .attention).prefix(1).map(\.id))
                : []
        )
        .padding(Space.l)
        .environment(DashboardStore())
    }

    private func evidence(for kind: ReceiptCheckSnapshotKind) -> ReceiptEvidenceDim {
        switch kind {
        case .overview: return overviewEvidence
        case .allPassed: return allPassedEvidence
        case .expanded: return expandedEvidence
        case .compact: return compactEvidence
        case .compactAccessibility: return compactAccessibilityEvidence
        case .accessibilityRTL: return accessibilityRTLEvidence
        }
    }

    private var overviewEvidence: ReceiptEvidenceDim {
        let sharedScope = "project:agentacct-gui-dashboard-command-center:756ce2c1ac691443"
        var checks = [
            check(
                name: "2640 tests passed and 9 failures will be rerun separately",
                result: "failed", exitCode: 1, scope: sharedScope, source: "mcp"
            ),
            check(
                name: "Ruff executable is unavailable in this shell",
                result: "error", exitCode: 127, scope: sharedScope, source: "mcp"
            ),
            check(
                name: "Hosted visual comparison did not start",
                result: "failed", exitCode: 2, scope: sharedScope, source: "ci"
            ),
            check(
                name: "Network-dependent artifact upload timed out",
                result: "error", exitCode: 124, scope: sharedScope, source: "hook"
            ),
            check(
                name: "Optional Intel compatibility pass",
                result: "skipped", scope: sharedScope, source: "ci"
            ),
            check(
                name: "Future provider verification state",
                result: nil, scope: sharedScope, source: "future_provider"
            ),
        ]
        checks += (1...68).map { index in
            check(
                name: "Passing verification run \(index)",
                result: "passed", exitCode: 0, scope: sharedScope, source: "mcp"
            )
        }
        return evidence(checks: checks, total: 74, passed: 68, failed: 4)
    }

    private var allPassedEvidence: ReceiptEvidenceDim {
        let sharedScope = "project:agentacct-gui:release-readiness"
        return evidence(
            checks: (1...4).map { index in
                check(
                    name: "Passing release verification \(index)",
                    result: "passed", exitCode: 0,
                    scope: sharedScope, source: index == 1 ? "ci" : "hook"
                )
            },
            total: 4,
            passed: 4,
            failed: 0
        )
    }

    private var expandedEvidence: ReceiptEvidenceDim {
        let openFinding = ReceiptCheckFinding(
            targetDigest: "checks-snapshot-finding",
            state: "open",
            revision: 2,
            attentionOpen: true,
            note: nil
        )
        return evidence(
            checks: [
                check(
                    name: "The complete failing check name remains readable after disclosure",
                    result: "failed", exitCode: 127,
                    scope: "project:agentacct-gui:focused-checks",
                    source: "hook",
                    summary: "Static analysis could not run because the executable was unavailable in this shell. The rest of the verification matrix completed independently.",
                    files: [
                        "apps/agentacct/Sources/agentacct/ReceiptsPane.swift",
                        "apps/agentacct/Tests/agentacctTests/ReceiptCheckPresentationTests.swift",
                    ],
                    commandRedacted: true,
                    artifactRef: "artifacts/checks/failure-report-with-a-long-reference.json",
                    finding: openFinding
                ),
                check(
                    name: "Swift semantic suite",
                    result: "passed", exitCode: 0,
                    scope: "app", source: "ci",
                    summary: "All focused semantic tests passed."
                ),
                check(
                    name: "Optional compatibility lane",
                    result: "skipped", scope: "legacy runner", source: "ci"
                ),
                check(
                    name: "Earlier failing run retained for audit",
                    result: "failed", exitCode: 1,
                    scope: "app", source: "hook", superseded: true
                ),
            ],
            total: 4,
            passed: 1,
            failed: 2
        )
    }

    private var compactEvidence: ReceiptEvidenceDim {
        evidence(
            checks: [
                check(
                    name: "A paragraph-length verification title stays recognizable in a narrow review column without surrendering its result",
                    result: "failed", exitCode: 127,
                    scope: "project:agentacct-gui-dashboard-command-center:756ce2c1ac691443",
                    source: "mcp"
                ),
                check(
                    name: "Long unbroken identifier ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    result: "passed", exitCode: 0,
                    scope: "workspace:another-very-long-scope:abcdef0123456789",
                    source: "client_hook"
                ),
                check(
                    name: "Unknown future result remains neutral",
                    result: "timed_out", exitCode: 75,
                    source: "future_source"
                ),
            ],
            total: 3,
            passed: 1,
            failed: 1
        )
    }

    private var compactAccessibilityEvidence: ReceiptEvidenceDim {
        let reviewedFinding = ReceiptCheckFinding(
            targetDigest: "checks-compact-accessibility-finding",
            state: "reviewed",
            revision: 3,
            attentionOpen: false,
            note: "Reviewed by the operator"
        )
        return evidence(
            checks: [
                check(
                    name: "Every status and provenance detail reflows without leaving the card",
                    result: "failed", exitCode: 127,
                    scope: "project:agentacct-gui:a-very-long-accessibility-review-scope",
                    source: "future_provider_with_a_very_long_name",
                    superseded: true,
                    finding: reviewedFinding
                ),
            ],
            total: 1,
            passed: 0,
            failed: 1
        )
    }

    private var accessibilityRTLEvidence: ReceiptEvidenceDim {
        evidence(
            checks: [
                check(
                    name: "فشل التحقق ويظل العنوان الكامل قابلاً للقراءة مع النص الكبير",
                    result: "failed", exitCode: 1,
                    scope: "المشروع:agentacct-gui:مرجع-طويل-للتدقيق",
                    source: "hook"
                ),
                check(
                    name: "Die vollständige Prüfbezeichnung bleibt auch bei sehr großer Schrift lesbar",
                    result: "passed", exitCode: 0,
                    scope: "Projekt:agentacct-gui:Barrierefreiheitsprüfung",
                    source: "ci"
                ),
                check(
                    name: "اختبار اختياري مؤجل",
                    result: "skipped",
                    scope: "تشغيل-اختياري", source: "mcp"
                ),
            ],
            total: 3,
            passed: 1,
            failed: 1
        )
    }

    private func evidence(
        checks: [ReceiptCheck],
        total: Int,
        passed: Int,
        failed: Int
    ) -> ReceiptEvidenceDim {
        ReceiptEvidenceDim(
            checks: checks,
            checksTotal: total,
            checksPassed: passed,
            checksFailed: failed,
            provenance: ["client_hook"],
            gaps: nil
        )
    }

    private func check(
        name: String,
        result: String?,
        exitCode: Int? = nil,
        scope: String? = nil,
        source: String? = nil,
        superseded: Bool? = nil,
        summary: String? = nil,
        files: [String]? = nil,
        commandRedacted: Bool? = nil,
        artifactRef: String? = nil,
        finding: ReceiptCheckFinding? = nil
    ) -> ReceiptCheck {
        ReceiptCheck(
            kind: "test",
            name: name,
            result: result,
            exitCode: exitCode,
            scope: scope,
            source: source,
            superseded: superseded,
            at: 1_787_589_700,
            summary: summary,
            files: files,
            commandRedacted: commandRedacted,
            artifactRef: artifactRef,
            artifactUrl: nil,
            finding: finding
        )
    }
}
