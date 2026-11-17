using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Security.AccessControl;
using System.Security.Principal;
using MemULife.Capture;

string root = Path.Combine(Path.GetTempPath(), "memulife-native-fixture-" + Guid.NewGuid().ToString("N"));
int passed = 0;
void Check(bool value, string name) { if (!value) throw new Exception(name); passed++; }
void Fails(Action operation, string code)
{
    try { operation(); throw new Exception("expected " + code); }
    catch (CaptureFailure error) { Check(error.Code == code, code); }
}
byte[] Box(string type, byte[]? content = null)
{
    content ??= [];
    byte[] bytes = new byte[8 + content.Length];
    BinaryPrimitives.WriteUInt32BigEndian(bytes, (uint)bytes.Length);
    Encoding.ASCII.GetBytes(type).CopyTo(bytes, 4);
    content.CopyTo(bytes, 8);
    return bytes;
}
try
{
    Guid device = Guid.NewGuid(), stream = Guid.NewGuid(), session = Guid.NewGuid(), segment = Guid.NewGuid();
    var options = new CaptureOptions(root, device, stream);
    options.Validate();
    Check(options.EncodedSize(3840, 2160) == (1920u, 1080u), "scaled dimensions");
    Check(options.EncodedSize(101, 99) == (100u, 98u), "even dimensions");
    Check(options.OwnedFrameLimit(3840, 2160) == 1, "4k bounded texture ownership");
    Fails(() => options.OwnedFrameLimit(7680, 4320), "frame_budget_exceeded");
    Fails(() => (options with { Fps = 0 }).Validate(), "configuration_invalid");
    Fails(() => (options with { Bitrate = 100_000_000, SegmentSeconds = 120 }).Validate(), "configuration_invalid");
    Fails(() => (options with { DeviceId = Guid.Empty }).Validate(), "configuration_invalid");
    Fails(() => (options with { StreamId = Guid.Empty }).Validate(), "configuration_invalid");
    Check(ClockMapping.Wire(ulong.MaxValue) == "18446744073709551615", "u64 wire precision");
    Check(ClockMapping.FromWgcTicks(90_071_992_547_410) == 9_007_199_254_741_000, "qpc conversion beyond js precision");
    var clock = new ClockMapping();
    Check(DateTimeOffset.Parse(clock.Utc(clock.AnchorNs)) == clock.AnchorUtc, "utc clock mapping");
    var drops = new DropLedger();
    foreach (long number in new long[] { 1, 2, 3 }) drops.Add(number, "encoder_backpressure");
    drops.Add(5, "configured_frame_rate");
    Check(drops.Through(2).Single() == new DroppedRange(1, 2, "encoder_backpressure"), "drop prefix splitting");
    Check(drops.Through(10).SequenceEqual([new(3, 3, "encoder_backpressure"), new(5, 5, "configured_frame_rate")]), "drop remainder preservation");
    Check(drops.Through(10).Count == 0, "drop ledger consumed");
    var boundedDrops = new DropLedger();
    for (long number = 0; number < 2000; number++) boundedDrops.Add(number * 2, "encoder_backpressure");
    Check(boundedDrops.DetailsIncomplete && boundedDrops.Through(4000).Count == 1024, "bounded precise drops with explicit incomplete evidence");

    var emitted = new List<string>();
    using var spool = new SpoolWriter(options, emitted.Add);
    Fails(() => spool.Resolve("../outside"), "spool_path_invalid");
    Fails(() => spool.Resolve("segments/../../outside"), "spool_path_invalid");
    Fails(() => spool.Resolve("C:/outside"), "spool_path_invalid");
    Fails(() => spool.Resolve("segments\\outside"), "spool_path_invalid");
    try { using var other = new SpoolWriter(options); throw new Exception("lease conflict not rejected"); }
    catch (IOException) { passed++; }
    string part = spool.PartPath(session, segment);
    byte[] container = Box("ftyp").Concat(Box("moov")).Concat(Box("moof")).Concat(Box("mdat", [1, 2, 3, 4])).ToArray();
    File.WriteAllBytes(part, container);
    SpoolWriter.ValidateFragmentedMp4(part);
    spool.Progress(session, stream, segment, clock, clock.AnchorNs, clock.AnchorNs + 100_000_000);
    spool.Seal(session, stream, segment, part, clock, clock.AnchorNs, clock.AnchorNs + 100_000_000,
        1920, 1080, 1920, 1080, 1, 1, 1, [], ["synthetic_container_fixture_not_video"]);
    Check(emitted.Count == 1 && !File.Exists(part), "seal publication only after rename");
    using (var document = JsonDocument.Parse(emitted[0]))
    {
        var value = document.RootElement;
        Check(value.GetProperty("backend").GetString() == "windows_wgc", "backend pin");
        Check(value.GetProperty("stream_id").GetGuid() == stream, "stable stream identity");
        Check(value.GetProperty("sha256").GetString() == Convert.ToHexStringLower(SHA256.HashData(container)), "content hash receipt");
        Check(value.GetProperty("size_bytes").GetInt64() == container.Length, "size receipt");
        Check(value.GetProperty("coverage").GetProperty("status").GetString() == "partial", "no invented full coverage");
        Check(value.GetProperty("start_monotonic_ns").ValueKind == JsonValueKind.String, "nanosecond string wire");
    }
    Check(spool.RecoverInterrupted() == 0, "sealed journal not recovered as gap");
    Guid lost = Guid.NewGuid();
    spool.Progress(session, stream, lost, clock, clock.AnchorNs, clock.AnchorNs);
    spool.Progress(session, stream, lost, clock, clock.AnchorNs, clock.AnchorNs + 200_000_000);
    Check(spool.RecoverInterrupted() == 1, "crash checkpoint recovery emits gap");
    Check(spool.RecoverInterrupted() == 0, "crash recovery idempotent");
    using (var recovered = JsonDocument.Parse(emitted[^1]))
    {
        Check(recovered.RootElement.GetProperty("coverage").GetProperty("reason").GetString() == "unsealed_after_crash", "explicit crash gap");
        Check(recovered.RootElement.GetProperty("end_monotonic_ns").GetString() == ClockMapping.Wire(clock.AnchorNs + 200_000_000), "durable observed gap boundary");
    }
    string malformed = spool.Resolve("malformed.mp4", true);
    File.WriteAllBytes(malformed, Box("ftyp"));
    Fails(() => SpoolWriter.ValidateFragmentedMp4(malformed), "fragmented_mp4_unavailable");
    byte[] overrun = Box("mdat");
    BinaryPrimitives.WriteUInt32BigEndian(overrun, 999);
    File.WriteAllBytes(malformed, overrun);
    Fails(() => SpoolWriter.ValidateFragmentedMp4(malformed), "segment_container_invalid");
    Guid oversized = Guid.NewGuid();
    string oversizedPart = spool.PartPath(session, oversized);
    File.WriteAllBytes(oversizedPart, container);
    Fails(() => spool.Seal(session, stream, oversized, oversizedPart, clock, clock.AnchorNs, clock.AnchorNs + 100_000_000,
        1920, 1080, 1920, 1080, 1, 1, 1, [], [new string('a', 256 * 1024)]), "manifest_budget_exceeded");
    Check(File.Exists(oversizedPart) && !File.Exists(oversizedPart[..^5]), "over-budget manifest cannot promote media");
    string fixtureOutput = Environment.GetEnvironmentVariable("MEMULIFE_FIXTURE_MANIFEST") ?? "";
    if (fixtureOutput.Length > 0)
    {
        File.WriteAllText(fixtureOutput, emitted[0] + "\n");
        File.WriteAllBytes(fixtureOutput + ".media", container);
    }
    if (OperatingSystem.IsWindows())
    {
        using var identity = WindowsIdentity.GetCurrent();
        var acl = new DirectoryInfo(root).GetAccessControl();
        Check(acl.AreAccessRulesProtected && identity.User!.Equals(acl.GetOwner(typeof(SecurityIdentifier))), "private root owner and protected DACL");
        bool privateAccess = true;
        foreach (FileSystemAccessRule rule in acl.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            if (rule.AccessControlType == AccessControlType.Allow && !identity.User!.Equals(rule.IdentityReference)) privateAccess = false;
        Check(privateAccess, "no broad root access");
        string broad = Path.Combine(root, "broad-access");
        Directory.CreateDirectory(broad);
        var broadAcl = new DirectoryInfo(broad).GetAccessControl();
        broadAcl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.WorldSid, null), FileSystemRights.Read, AccessControlType.Allow));
        new DirectoryInfo(broad).SetAccessControl(broadAcl);
        Fails(() => { using var rejected = new SpoolWriter(options with { Spool = broad }); }, "spool_permissions_not_private");
        Fails(() => spool.Resolve("broad-access/media.mp4", true), "spool_permissions_not_private");
    }
    Console.WriteLine($"{passed} portable native core assertions passed; Windows capture/codec not exercised");
}
finally { if (Directory.Exists(root)) Directory.Delete(root, recursive: true); }
