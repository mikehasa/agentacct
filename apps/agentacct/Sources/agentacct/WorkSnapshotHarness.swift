import SwiftUI

enum WorkSnapshotState: String {
    case table
    case receipt
    case empty
    case listError = "list-error"
    case receiptLoading = "receipt-loading"
    case receiptError = "receipt-error"

    var storeState: SnapshotWorkStoreState {
        switch self {
        case .table, .receipt: return .populated
        case .empty: return .empty
        case .listError: return .listError
        case .receiptLoading: return .receiptLoading
        case .receiptError: return .receiptError
        }
    }

    var selectsReceipt: Bool {
        switch self {
        case .receipt, .receiptLoading, .receiptError: return true
        case .table, .empty, .listError: return false
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
            WorkSnapshotState.empty,
            .listError,
            .receiptLoading,
            .receiptError,
        ].flatMap { state in
            [
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .light),
                Self(state: state, viewport: "reference", width: 1120, height: 800, colorScheme: .dark),
            ]
        }
        return core + transient
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
            selection.taskId = configuration.state.selectsReceipt ? work.receipt.taskId : nil

            let view = MainWindow(canSetUpOverride: true)
                .environmentObject(glance)
                .environmentObject(dashboard)
                .environmentObject(selection)
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
                .environment(\.dynamicTypeSize, .medium)
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

enum ReceiptActionSnapshotKind: String, CaseIterable {
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
    private static let snapshotLocale = Locale(identifier: "en_US_POSIX")

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
            reportedTotal: 80
        )
    }

    private var exactCard: some View {
        Card(padding: Space.xl) {
            ReceiptActionsDigest(
                synopsis: exactSynopsis,
                recordedPathCount: 18,
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
                        recordedPathCount: 1_234,
                        provenance: ["lokales_client_protokoll_mit_langer_bezeichnung"],
                        gaps: ["Die Abdeckung einzelner Aktionen ist unvollständig und kann nicht rekonstruiert werden."]
                    )
                    .frame(width: 320)
                }
                stressCard(title: "560 pt · RTL") {
                    ReceiptActionsDigest(
                        synopsis: exactSynopsis,
                        recordedPathCount: 18,
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
                    recordedPathCount: 18,
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
                        reportedTotal: 101
                    ),
                    recordedPathCount: 18,
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
                synopsis: receiptActionSynopsis(counts: nil, reportedTotal: nil),
                pathCount: nil, provenance: nil, gaps: []
            ),
            .init(
                id: "zero", title: "Zero records · capture unknown",
                synopsis: receiptActionSynopsis(counts: [:], reportedTotal: 0),
                pathCount: nil, provenance: nil,
                gaps: ["Tool categories were not instrumented for this session."]
            ),
            .init(
                id: "total-only", title: "Total only",
                synopsis: receiptActionSynopsis(counts: nil, reportedTotal: 80),
                pathCount: 18, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "exact", title: "Exact reconciliation",
                synopsis: exactSynopsis,
                pathCount: 18, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "partial", title: "Conflicting total · reported higher",
                synopsis: receiptActionSynopsis(counts: ["read": 7], reportedTotal: 10),
                pathCount: 2, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "mismatch", title: "Conflicting total · categorized higher",
                synopsis: receiptActionSynopsis(counts: ["read": 8, "edit": 4], reportedTotal: 10),
                pathCount: 4, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "invalid", title: "Invalid category",
                synopsis: receiptActionSynopsis(counts: ["read": 8, "edit": -2], reportedTotal: 8),
                pathCount: 1, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "legacy", title: "Recorded total unavailable",
                synopsis: receiptActionSynopsis(counts: ["read": 3, "execute": 2], reportedTotal: nil),
                pathCount: 2, provenance: ["client_log"], gaps: []
            ),
            .init(
                id: "high-cardinality", title: "High-cardinality future taxonomy",
                synopsis: receiptActionSynopsis(counts: highCardinality, reportedTotal: 65),
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
            headline: "66 aufgezeichnete Aktionen",
            integrityDetail: nil,
            reportedTotal: 66,
            categorizedTotal: 66,
            shareDenominator: 66,
            captureBoundary: "Kein geordnetes Aktionsprotokoll; erfasste Signale lassen sich nicht mit Ergebnissen oder Zeitangaben verknüpfen."
        )
    }

    private func actionCard(_ example: ReceiptActionSnapshotExample) -> some View {
        Card(padding: Space.l) {
            VStack(alignment: .leading, spacing: Space.s) {
                CapsLabel(text: example.title)
                Rectangle().fill(Theme.hairline).frame(height: 1)
                ReceiptActionsDigest(
                    synopsis: example.synopsis,
                    recordedPathCount: example.pathCount,
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
