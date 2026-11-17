using System.Diagnostics;
using System.Globalization;

namespace MemULife.Capture;

public sealed class ClockMapping
{
    public Guid Id { get; } = Guid.NewGuid();
    public ulong AnchorNs { get; } = NowNs();
    public DateTimeOffset AnchorUtc { get; } = DateTimeOffset.UtcNow;

    public static ulong NowNs() => checked((ulong)((Int128)Stopwatch.GetTimestamp() * 1_000_000_000 / Stopwatch.Frequency));
    public static ulong FromWgcTicks(long ticks) => checked((ulong)ticks * 100);
    public static string Wire(ulong value) => value.ToString(CultureInfo.InvariantCulture);
    public string Utc(ulong value) => AnchorUtc.AddTicks(checked((long)(((Int128)value - AnchorNs) / 100)))
        .ToUniversalTime().ToString("O", CultureInfo.InvariantCulture);
}

public sealed record DroppedRange(long FirstSeq, long LastSeq, string Reason);

public sealed class DropLedger
{
    private readonly object gate = new();
    private readonly List<DroppedRange> ranges = [];
    public bool DetailsIncomplete { get; private set; }

    public void Add(long sequence, string reason)
    {
        lock (gate)
        {
            if (ranges.Count > 0 && ranges[^1].LastSeq + 1 == sequence && ranges[^1].Reason == reason)
                ranges[^1] = ranges[^1] with { LastSeq = sequence };
            else if (ranges.Count < 1024) ranges.Add(new(sequence, sequence, reason));
            else DetailsIncomplete = true;
        }
    }

    public IReadOnlyList<DroppedRange> Through(long sequence)
    {
        lock (gate)
        {
            var result = ranges.Where(x => x.FirstSeq <= sequence)
                .Select(x => x with { LastSeq = Math.Min(x.LastSeq, sequence) }).ToArray();
            for (int i = ranges.Count - 1; i >= 0; --i)
                if (ranges[i].LastSeq <= sequence) ranges.RemoveAt(i);
                else if (ranges[i].FirstSeq <= sequence) ranges[i] = ranges[i] with { FirstSeq = sequence + 1 };
            return result;
        }
    }
}
