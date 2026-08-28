import Foundation

struct TemporalTextContext {
    let now: Date
    let locale: Locale
    let calendar: Calendar
    let timeZone: TimeZone

    static var current: Self {
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = .autoupdatingCurrent
        return Self(
            now: SnapshotMode.currentDate,
            locale: .autoupdatingCurrent,
            calendar: calendar,
            timeZone: .autoupdatingCurrent
        )
    }
}

enum ProviderResetPresentation: Equatable {
    case missing
    case future(countdown: String)
    case passed(dateTime: String)
    case invalid

    var text: String {
        switch self {
        case .missing: return "Reset time not reported"
        case .future(let countdown): return "Resets in \(countdown)"
        case .passed(let dateTime): return "Reported reset time passed \(dateTime)"
        case .invalid: return "Reset time is invalid"
        }
    }
}

enum TemporalText {
    // Wide enough for legitimate audit history and provider schedules, while
    // keeping Double-to-Int conversion and absurd calendar values bounded.
    private static let maximumSupportedSeconds = 100.0 * 366.0 * 86_400.0

    static func age(
        epoch: Double?,
        context: TemporalTextContext = .current
    ) -> String? {
        guard let epoch, validEpoch(epoch) else { return nil }
        let delta = context.now.timeIntervalSince1970 - epoch
        guard delta.isFinite, abs(delta) <= maximumSupportedSeconds else { return nil }
        if delta < -60 { return "time appears incorrect" }
        if delta < 60 { return "just now" }
        if delta >= 7 * 86_400 { return date(epoch: epoch, context: context) }

        let total = Int(delta.rounded(.down))
        if total < 3_600 { return unit(total / 60, singular: "min", plural: "min") + " ago" }
        if total < 86_400 { return unit(total / 3_600, singular: "hr", plural: "hr") + " ago" }
        return unit(total / 86_400, singular: "day", plural: "days") + " ago"
    }

    static func recordedSpan(seconds: Double) -> String? {
        guard validDuration(seconds) else { return nil }
        if seconds == 0 { return "0 min" }
        if seconds < 60 { return "<1 min" }
        let total = Int(seconds.rounded(.down))
        let hours = total / 3_600
        let minutes = (total % 3_600) / 60
        if hours > 0 {
            return minutes > 0 ? "\(hours) hr \(minutes) min" : "\(hours) hr"
        }
        return "\(minutes) min"
    }

    static func interval(seconds: Double?) -> String? {
        guard let seconds, validDuration(seconds) else { return nil }
        if seconds == 0 { return "0 sec" }
        if seconds < 1 { return "<1 sec" }
        let total = Int(seconds.rounded())
        if total < 60 { return unit(total, singular: "sec", plural: "sec") }
        if total < 3_600 { return unit(total / 60, singular: "min", plural: "min") }
        if total < 86_400 { return unit(total / 3_600, singular: "hr", plural: "hr") }
        return unit(total / 86_400, singular: "day", plural: "days")
    }

    static func providerReset(
        epoch: Double?,
        context: TemporalTextContext = .current
    ) -> ProviderResetPresentation {
        guard let epoch else { return .missing }
        guard validEpoch(epoch) else { return .invalid }
        let delta = epoch - context.now.timeIntervalSince1970
        guard delta.isFinite, abs(delta) <= maximumSupportedSeconds else { return .invalid }
        if delta <= 0 {
            guard let dateTime = exactDateTime(epoch: epoch, context: context) else { return .invalid }
            return .passed(dateTime: dateTime)
        }
        guard let countdown = countdown(seconds: delta) else { return .invalid }
        return .future(countdown: countdown)
    }

    static func shouldShowSecondaryRefresh(
        _ secondary: Date?,
        primary: Date?,
        minimumDifference: TimeInterval = 60
    ) -> Bool {
        guard let secondary else { return false }
        guard let primary else { return true }
        return abs(secondary.timeIntervalSince(primary)) >= minimumDifference
    }

    static func date(
        epoch: Double,
        context: TemporalTextContext = .current
    ) -> String? {
        guard validEpoch(epoch) else { return nil }
        guard abs(context.now.timeIntervalSince1970 - epoch) <= maximumSupportedSeconds else {
            return nil
        }
        let value = Date(timeIntervalSince1970: epoch)
        let formatter = DateFormatter()
        formatter.locale = context.locale
        formatter.calendar = context.calendar
        formatter.timeZone = context.timeZone
        formatter.dateStyle = .medium
        formatter.timeStyle = .none
        return formatter.string(from: value)
    }

    static func exactDateTime(
        epoch: Double,
        context: TemporalTextContext = .current
    ) -> String? {
        guard validEpoch(epoch) else { return nil }
        guard abs(context.now.timeIntervalSince1970 - epoch) <= maximumSupportedSeconds else {
            return nil
        }
        let value = Date(timeIntervalSince1970: epoch)
        let formatter = DateFormatter()
        formatter.locale = context.locale
        formatter.calendar = context.calendar
        formatter.timeZone = context.timeZone
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter.string(from: value)
    }

    private static func countdown(seconds: Double) -> String? {
        guard validDuration(seconds), seconds > 0 else { return nil }
        if seconds < 60 { return "<1 min" }
        let total = Int(seconds.rounded(.down))
        let days = total / 86_400
        let hours = (total % 86_400) / 3_600
        let minutes = (total % 3_600) / 60
        if days > 0 {
            let dayText = unit(days, singular: "day", plural: "days")
            return hours > 0 ? "\(dayText) \(hours) hr" : dayText
        }
        if hours > 0 { return minutes > 0 ? "\(hours) hr \(minutes) min" : "\(hours) hr" }
        return "\(minutes) min"
    }

    private static func unit(_ value: Int, singular: String, plural: String) -> String {
        "\(value) \(value == 1 ? singular : plural)"
    }

    private static func validEpoch(_ value: Double) -> Bool {
        value.isFinite && value > 0
    }

    private static func validDuration(_ value: Double) -> Bool {
        value.isFinite && value >= 0 && value <= maximumSupportedSeconds
    }
}
