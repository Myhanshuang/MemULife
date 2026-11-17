using System.Buffers.Binary;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace MemULife.Capture;

public sealed class SpoolWriter : IDisposable
{
    private readonly string root;
    private readonly Guid deviceId;
    private readonly Guid streamId;
    private readonly FileStream lease;
    private readonly object gate = new();
    private readonly long diskLimit;
    private readonly Action<string> publish;
    public string Root => root;

    public SpoolWriter(CaptureOptions options, Action<string>? publish = null)
    {
        options.Validate();
        root = Path.GetFullPath(options.Spool);
        deviceId = options.DeviceId;
        streamId = options.StreamId;
        diskLimit = options.DiskLimitBytes;
        this.publish = publish ?? JsonLineSignals.TryPublish;
        CheckParents(root);
        SpoolPrivacy.CreateRoot(root);
        lease = new FileStream(Resolve(".capture-owner"), FileMode.OpenOrCreate,
            FileAccess.ReadWrite, FileShare.None);
    }

    public void Dispose() => lease.Dispose();

    public string PartPath(Guid session, Guid segment)
    {
        lock (gate)
        {
            long size = 0;
            foreach (string path in Directory.EnumerateFiles(root, "*", new EnumerationOptions { RecurseSubdirectories = true, AttributesToSkip = FileAttributes.ReparsePoint }))
            {
                CheckParents(path);
                SpoolPrivacy.Verify(path);
                size = checked(size + new FileInfo(path).Length);
                if (size >= diskLimit) throw new CaptureFailure("storage_pressure");
            }
            return Resolve($"segments/{session:D}/{segment:D}.mp4.part", createParent: true);
        }
    }

    public string Resolve(string relative, bool createParent = false)
    {
        if (Path.IsPathRooted(relative) || relative.IndexOfAny(['\\', ':', '\0']) >= 0 || relative.Split('/').Any(x => x is "" or "." or ".."))
            throw new CaptureFailure("spool_path_invalid");
        string full = Path.GetFullPath(Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar)));
        if (!full.StartsWith(root.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new CaptureFailure("spool_path_invalid");
        CheckParents(full);
        CheckPrivateScope(full);
        if (createParent) Directory.CreateDirectory(Path.GetDirectoryName(full)!);
        CheckParents(full);
        CheckPrivateScope(full);
        return full;
    }

    private void CheckPrivateScope(string path)
    {
        for (string? current = path; current is not null; current = Path.GetDirectoryName(current))
        {
            SpoolPrivacy.Verify(current);
            if (string.Equals(current, root, StringComparison.OrdinalIgnoreCase)) break;
        }
    }

    private static void CheckParents(string path)
    {
        for (string? current = path; current is not null; current = Path.GetDirectoryName(current))
            if ((File.Exists(current) || Directory.Exists(current)) && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                throw new CaptureFailure("spool_path_invalid");
    }

    public static void ValidateFragmentedMp4(string path)
    {
        using FileStream stream = File.OpenRead(path);
        var types = new HashSet<string>();
        Span<byte> header = stackalloc byte[16];
        while (stream.Position < stream.Length)
        {
            long start = stream.Position;
            stream.ReadExactly(header[..8]);
            ulong size = BinaryPrimitives.ReadUInt32BigEndian(header);
            string type = Encoding.ASCII.GetString(header.Slice(4, 4));
            int headerSize = 8;
            if (size == 1) { stream.ReadExactly(header[8..]); size = BinaryPrimitives.ReadUInt64BigEndian(header[8..]); headerSize = 16; }
            if (size == 0) size = checked((ulong)(stream.Length - start));
            if (size < (ulong)headerSize || size > (ulong)(stream.Length - start))
                throw new CaptureFailure("segment_container_invalid");
            types.Add(type);
            stream.Position = checked(start + (long)size);
        }
        if (!new[] { "ftyp", "moov", "moof", "mdat" }.All(types.Contains))
            throw new CaptureFailure("fragmented_mp4_unavailable");
    }

    public void Seal(Guid session, Guid stream, Guid segment, string part, ClockMapping clock,
        ulong startNs, ulong endNs, int sourceWidth, int sourceHeight, uint width, uint height,
        long firstSequence, long lastSequence, int frameCount, IReadOnlyList<DroppedRange> drops,
        IReadOnlyList<string>? extraNotes = null)
    {
        lock (gate)
        {
            string relative = $"segments/{session:D}/{segment:D}.mp4";
            string target = Resolve(relative, createParent: true);
            if (!string.Equals(part, target + ".part", StringComparison.OrdinalIgnoreCase) || endNs < startNs || frameCount < 1)
                throw new CaptureFailure("segment_metadata_invalid");
            ValidateFragmentedMp4(part);
            if (new FileInfo(part).Length > 32 * 1024 * 1024)
                throw new CaptureFailure("segment_budget_exceeded");
            string hash;
            long length;
            using (var file = new FileStream(part, FileMode.Open, FileAccess.ReadWrite, FileShare.Read))
            {
                file.Flush(flushToDisk: true);
                hash = Convert.ToHexStringLower(SHA256.HashData(file));
                length = file.Length;
            }
            var record = Common(session, stream, segment, clock, startNs, endNs, "sealed_segment");
            record["relative_path"] = relative;
            record["sha256"] = hash;
            record["size_bytes"] = length;
            record["media_type"] = "video/mp4";
            record["container"] = "fragmented_mp4";
            record["codec"] = "h264";
            record["width"] = width;
            record["height"] = height;
            record["source_width"] = sourceWidth;
            record["source_height"] = sourceHeight;
            record["first_frame_seq"] = firstSequence;
            record["last_frame_seq"] = lastSequence;
            record["frame_count"] = frameCount;
            record["coverage"] = new
            {
                status = "partial",
                notes = new[] { "frame_index_unavailable", "wgc_picker_identity_session_scoped", "windows_power_loss_unqualified" }.Concat(extraNotes ?? []),
                dropped_frame_ranges = drops.Select(x => new { first_seq = x.FirstSeq, last_seq = x.LastSeq, reason = x.Reason })
            };
            ValidateManifest(record);
            PublishFile(part, target);
            Commit(record, session, segment);
        }
    }

    public void Progress(Guid session, Guid stream, Guid segment, ClockMapping clock, ulong startNs, ulong lastObservedNs)
    {
        lock (gate)
        {
            var record = Common(session, stream, segment, clock, startNs, lastObservedNs, "capture_gap");
            string target = Resolve($"pending/{session:D}/{segment:D}.json", createParent: true);
            WriteJson(target, JsonSerializer.Serialize(record), replace: true);
        }
    }

    public int RecoverInterrupted()
    {
        lock (gate)
        {
            string directory = Path.Combine(root, "pending");
            if (!Directory.Exists(directory)) return 0;
            int recovered = 0;
            foreach (string path in Directory.EnumerateFiles(directory, "*.json", new EnumerationOptions { RecurseSubdirectories = true, AttributesToSkip = FileAttributes.ReparsePoint }))
            {
                CheckParents(path);
                CheckPrivateScope(path);
                if (new FileInfo(path).Length > 64 * 1024) throw new CaptureFailure("recovery_journal_invalid");
                using var document = JsonDocument.Parse(File.ReadAllBytes(path));
                var record = document.RootElement;
                if (record.GetProperty("device_id").GetGuid() != deviceId || record.GetProperty("stream_id").GetGuid() != streamId || record.GetProperty("backend").GetString() != "windows_wgc")
                    throw new CaptureFailure("spool_device_mismatch");
                Guid session = record.GetProperty("session_id").GetGuid();
                Guid segment = record.GetProperty("segment_id").GetGuid();
                if (File.Exists(Resolve($"manifests/{session:D}/{segment:D}.json"))) continue;
                var gap = record.EnumerateObject().ToDictionary(x => x.Name, x => (object?)x.Value.Clone());
                gap["kind"] = "capture_gap";
                gap["coverage"] = new
                {
                    status = "gap",
                    reason = "unsealed_after_crash",
                    notes = new[] { "known_interval_from_durable_progress", "crash_tail_end_unknown", "unsealed_media_retained_for_manual_recovery" },
                    dropped_frame_ranges = Array.Empty<object>()
                };
                Commit(gap, session, segment);
                recovered++;
            }
            return recovered;
        }
    }

    public void Gap(Guid session, Guid stream, ClockMapping clock, ulong startNs, ulong endNs, string reason)
    {
        lock (gate)
        {
            if (endNs < startNs) throw new CaptureFailure("segment_metadata_invalid");
            Guid segment = Guid.NewGuid();
            var record = Common(session, stream, segment, clock, startNs, endNs, "capture_gap");
            record["coverage"] = new { status = "gap", reason, notes = Array.Empty<string>(), dropped_frame_ranges = Array.Empty<object>() };
            Commit(record, session, segment);
        }
    }

    private Dictionary<string, object?> Common(Guid session, Guid stream, Guid segment, ClockMapping clock,
        ulong startNs, ulong endNs, string kind) => new()
        {
            ["format"] = "memulife.screen-segment.v1",
            ["kind"] = kind,
            ["device_id"] = deviceId,
            ["session_id"] = session,
            ["stream_id"] = stream,
            ["segment_id"] = segment,
            ["clock_mapping_id"] = clock.Id,
            ["start_monotonic_ns"] = ClockMapping.Wire(startNs),
            ["end_monotonic_ns"] = ClockMapping.Wire(endNs),
            ["start_wallclock"] = clock.Utc(startNs),
            ["end_wallclock"] = clock.Utc(endNs),
            ["backend"] = "windows_wgc"
        };

    private void Commit(Dictionary<string, object?> record, Guid session, Guid segment)
    {
        string json = ValidateManifest(record);
        string path = Resolve($"manifests/{session:D}/{segment:D}.json", createParent: true);
        WriteJson(path, json);
        publish(json);
    }

    private static string ValidateManifest(Dictionary<string, object?> record)
    {
        string json = JsonSerializer.Serialize(record);
        if (Encoding.UTF8.GetByteCount(json) + 1 > 256 * 1024)
            throw new CaptureFailure("manifest_budget_exceeded");
        return json;
    }

    private static void WriteJson(string path, string json, bool replace = false)
    {
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".part";
        using (var output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096, FileOptions.WriteThrough))
        {
            output.Write(Encoding.UTF8.GetBytes(json + "\n"));
            output.Flush(flushToDisk: true);
        }
        PublishFile(temporary, path, replace);
    }

    private static void PublishFile(string temporary, string target, bool replace = false)
    {
        if (OperatingSystem.IsWindows())
        {
            if (!MoveFileEx(temporary, target, 0x8u | (replace ? 0x1u : 0))) throw new CaptureFailure("spool_publish_failed");
        }
        else File.Move(temporary, target, overwrite: replace); // portable fixtures only
    }

    [DllImport("kernel32.dll", EntryPoint = "MoveFileExW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool MoveFileEx(string existing, string target, uint flags);
}
