namespace MemULife.Capture;

public sealed record CaptureOptions(string Spool, Guid DeviceId, Guid StreamId, int Fps = 10,
    int SegmentSeconds = 30, int LongEdge = 1920, uint Bitrate = 2_000_000,
    long FrameBudgetBytes = 64 * 1024 * 1024, long DiskLimitBytes = 8L * 1024 * 1024 * 1024)
{
    public void Validate()
    {
        if (string.IsNullOrWhiteSpace(Spool) || DeviceId == Guid.Empty || StreamId == Guid.Empty || Fps is < 1 or > 60
            || SegmentSeconds is < 1 or > 120 || LongEdge is < 320 or > 7680
            || Bitrate is < 100_000 or > 100_000_000
            || (long)Bitrate * SegmentSeconds / 8 > 24 * 1024 * 1024
            || FrameBudgetBytes is < 16 * 1024 * 1024 or > 512 * 1024 * 1024
            || DiskLimitBytes is < 64 * 1024 * 1024 or > 1024L * 1024 * 1024 * 1024)
            throw new CaptureFailure("configuration_invalid");
    }

    public (uint Width, uint Height) EncodedSize(int width, int height)
    {
        if (width < 2 || height < 2) throw new CaptureFailure("source_dimensions_invalid");
        double ratio = Math.Min(1.0, (double)LongEdge / Math.Max(width, height));
        return ((uint)Math.Max(2, ((int)(width * ratio) / 2) * 2),
                (uint)Math.Max(2, ((int)(height * ratio) / 2) * 2));
    }

    public int OwnedFrameLimit(int width, int height)
    {
        // One additional texture belongs to WGC's one-frame pool.
        long bytes = checked((long)width * height * 4);
        int count = (int)Math.Min(8, FrameBudgetBytes / bytes - 1);
        if (count < 1) throw new CaptureFailure("frame_budget_exceeded");
        return count;
    }
}

public sealed class CaptureFailure(string code) : Exception(code)
{
    public string Code { get; } = code;
}
