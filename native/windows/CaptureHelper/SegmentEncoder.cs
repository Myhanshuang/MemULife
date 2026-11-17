using System.Collections.Concurrent;
using System.Threading.Channels;
using MemULife.Capture;
using Windows.Media.Core;
using Windows.Media.Editing;
using Windows.Media.MediaProperties;
using Windows.Media.Transcoding;
using Windows.Storage;

namespace MemULife.Capture.Windows;

internal sealed class SegmentEncoder(CaptureOptions options, int sourceWidth, int sourceHeight, bool useHardware = true)
{
    public OwnedFrame? Carry { get; private set; }
    public int FrameCount { get; private set; }
    public long LastSequence { get; private set; }
    public ulong EndNs { get; private set; }

    public async Task EncodeAsync(string part, OwnedFrame first, ChannelReader<OwnedFrame> frames,
        Action<ulong>? checkpoint = null)
    {
        var size = options.EncodedSize(sourceWidth, sourceHeight);
        var profile = MediaEncodingProfile.CreateMp4(VideoEncodingQuality.HD1080p);
        profile.Audio = null;
        // MFTranscodeContainerType_FMPEG4, defined by the Windows SDK mfidl.h.
        profile.Container.Subtype = "9ba876f1-419f-4b77-a1e0-35959d9d4004";
        profile.Video.Width = size.Width;
        profile.Video.Height = size.Height;
        profile.Video.Bitrate = options.Bitrate;
        profile.Video.FrameRate.Numerator = (uint)options.Fps;
        profile.Video.FrameRate.Denominator = 1;
        var properties = VideoEncodingProperties.CreateUncompressed(MediaEncodingSubtypes.Bgra8, (uint)sourceWidth, (uint)sourceHeight);
        properties.FrameRate.Numerator = (uint)options.Fps;
        properties.FrameRate.Denominator = 1;
        var source = new MediaStreamSource(new VideoStreamDescriptor(properties))
        {
            BufferTime = TimeSpan.Zero,
            CanSeek = false
        };
        source.Starting += (_, args) => args.Request.SetActualStartPosition(TimeSpan.Zero);
        var inFlight = new ConcurrentDictionary<long, OwnedFrame>();
        var sampleGate = new SemaphoreSlim(1, 1);
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(options.SegmentSeconds + 5));
        OwnedFrame? nextFirst = first;
        Exception? sampleFailure = null;
        ulong lastTimestamp = 0;
        ulong endBoundary = first.TimestampNs + (ulong)options.SegmentSeconds * 1_000_000_000;

        async void SupplySample(MediaStreamSource sender, MediaStreamSourceSampleRequestedEventArgs args)
        {
            var deferral = args.Request.GetDeferral();
            await sampleGate.WaitAsync();
            OwnedFrame? frame = null;
            try
            {
                if (sampleFailure is not null || Carry is not null) { args.Request.Sample = null; return; }
                frame = Interlocked.Exchange(ref nextFirst, null);
                if (frame is null)
                {
                    try { frame = await frames.ReadAsync(deadline.Token); }
                    catch (ChannelClosedException) { args.Request.Sample = null; return; }
                    catch (OperationCanceledException) { args.Request.Sample = null; return; }
                }
                if (frame.TimestampNs >= endBoundary) { Carry = frame; frame = null; args.Request.Sample = null; return; }
                if (FrameCount > 0 && frame.TimestampNs <= lastTimestamp) throw new CaptureFailure("frame_timestamp_regressed");
                lastTimestamp = frame.TimestampNs;
                var sample = MediaStreamSample.CreateFromDirect3D11Surface(frame.Surface,
                    TimeSpan.FromTicks(checked((long)((frame.TimestampNs - first.TimestampNs) / 100))));
                sample.Duration = TimeSpan.FromTicks(10_000_000 / options.Fps);
                var owned = frame;
                inFlight[owned.Sequence] = owned;
                sample.Processed += (_, _) =>
                {
                    if (inFlight.TryRemove(owned.Sequence, out var finished)) finished.Dispose();
                };
                FrameCount++;
                LastSequence = frame.Sequence;
                EndNs = frame.TimestampNs + (ulong)(1_000_000_000 / options.Fps);
                if (FrameCount == 1 || FrameCount % options.Fps == 0) checkpoint?.Invoke(EndNs);
                args.Request.Sample = sample;
                frame = null;
            }
            catch (Exception error) { sampleFailure = error; args.Request.Sample = null; }
            finally { frame?.Dispose(); sampleGate.Release(); deferral.Complete(); }
        }
        source.SampleRequested += SupplySample;

        try
        {
            using (var file = new FileStream(part, FileMode.CreateNew, FileAccess.Write, FileShare.None)) file.Flush(true);
            var destination = await StorageFile.GetFileFromPathAsync(part);
            using (var output = await destination.OpenAsync(FileAccessMode.ReadWrite))
            {
                var transcoder = new MediaTranscoder { HardwareAccelerationEnabled = useHardware };
                var prepared = await transcoder.PrepareMediaStreamSourceTranscodeAsync(source, output, profile);
                if (!prepared.CanTranscode) throw new CaptureFailure("h264_encoder_unavailable");
                using var hardDeadline = new CancellationTokenSource(TimeSpan.FromSeconds(options.SegmentSeconds + 35));
                await prepared.TranscodeAsync().AsTask(hardDeadline.Token);
                await output.FlushAsync();
            }
            if (sampleFailure is not null) throw sampleFailure;
            if (FrameCount < 1) throw new CaptureFailure("segment_empty");
            SpoolWriter.ValidateFragmentedMp4(part);
            // Resolve the encoded file through the Windows media decoder and
            // decode an actual thumbnail; a successful encoder call alone is
            // insufficient proof that a replayable video was produced.
            var clip = await MediaClip.CreateFromFileAsync(destination);
            if (clip.OriginalDuration <= TimeSpan.Zero) throw new CaptureFailure("segment_decode_failed");
            var composition = new MediaComposition();
            composition.Clips.Add(clip);
            using var thumbnail = await composition.GetThumbnailAsync(TimeSpan.Zero, 32, 32, VideoFramePrecision.NearestFrame);
            if (thumbnail.Size == 0) throw new CaptureFailure("segment_decode_failed");
        }
        finally
        {
            deadline.Cancel();
            source.SampleRequested -= SupplySample;
            await sampleGate.WaitAsync();
            nextFirst?.Dispose();
            foreach (var frame in inFlight.Values) frame.Dispose();
            sampleGate.Release();
        }
    }
}
