using System.Threading.Channels;
using MemULife.Capture;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;

namespace MemULife.Capture.Windows;

internal sealed class CaptureEngine : IAsyncDisposable
{
    private readonly CaptureOptions options;
    private readonly SpoolWriter spool;
    private readonly GraphicsCaptureItem item;
    private readonly D3DDevice device;
    private readonly Direct3D11CaptureFramePool pool;
    private readonly GraphicsCaptureSession capture;
    private readonly SemaphoreSlim budget;
    private readonly Channel<OwnedFrame> frames;
    private readonly object frameGate = new();
    private readonly DropLedger drops = new();
    private readonly ClockMapping clock = new();
    private readonly Guid sessionId = Guid.NewGuid();
    private readonly Guid streamId;
    private readonly ulong startedNs = ClockMapping.NowNs();
    private readonly int width;
    private readonly int height;
    private Task? worker;
    private long sequence;
    private ulong lastAccepted;
    private ulong lastObserved;
    private bool stopped;
    private bool resourcesDisposed;
    private string stopReason = "user_stopped";
    public event Action<string>? StopRequested;

    public CaptureEngine(CaptureOptions options, SpoolWriter spool, GraphicsCaptureItem item)
    {
        this.options = options;
        this.spool = spool;
        this.item = item;
        streamId = options.StreamId;
        width = item.Size.Width;
        height = item.Size.Height;
        int capacity = options.OwnedFrameLimit(width, height);
        D3DDevice.RequireSdrOutputs();
        device = new D3DDevice();
        budget = new SemaphoreSlim(capacity, capacity);
        frames = Channel.CreateBounded<OwnedFrame>(new BoundedChannelOptions(capacity)
        {
            FullMode = BoundedChannelFullMode.Wait,
            SingleWriter = true,
            SingleReader = true
        });
        pool = Direct3D11CaptureFramePool.CreateFreeThreaded(device.WinRt,
            DirectXPixelFormat.B8G8R8A8UIntNormalized, 1, item.Size);
        capture = pool.CreateCaptureSession(item);
        capture.IsCursorCaptureEnabled = true;
        pool.FrameArrived += OnFrame;
        item.Closed += OnClosed;
    }

    public void Start()
    {
        worker = Task.Run(EncodeLoopAsync);
        capture.StartCapture();
    }

    private void OnClosed(GraphicsCaptureItem sender, object args) => StopRequested?.Invoke("source_closed");

    private void OnFrame(Direct3D11CaptureFramePool sender, object args)
    {
        string? failure = null;
        lock (frameGate)
        {
            if (stopped) return;
            try
            {
                using var frame = sender.TryGetNextFrame();
                if (frame is null) return;
                long current = sequence++;
                ulong timestamp = ClockMapping.FromWgcTicks(frame.SystemRelativeTime.Ticks);
                lastObserved = timestamp;
                if (frame.ContentSize.Width != width || frame.ContentSize.Height != height)
                    failure = "display_config_changed";
                else if (timestamp <= lastAccepted || timestamp - lastAccepted < (ulong)(1_000_000_000 / options.Fps))
                    drops.Add(current, "configured_frame_rate");
                else if (!budget.Wait(0)) drops.Add(current, "encoder_backpressure");
                else
                {
                    OwnedFrame? owned = null;
                    try
                    {
                        owned = device.Copy(frame.Surface, current, timestamp, () => budget.Release());
                        if (frames.Writer.TryWrite(owned)) { owned = null; lastAccepted = timestamp; }
                        else drops.Add(current, "encoder_backpressure");
                    }
                    catch { if (owned is null) budget.Release(); throw; }
                    finally { owned?.Dispose(); }
                }
            }
            catch { failure = "capture_device_lost"; }
        }
        if (failure is not null) StopRequested?.Invoke(failure);
    }

    private async Task EncodeLoopAsync()
    {
        OwnedFrame? carry = null;
        ulong unsealedStart = startedNs;
        try
        {
            while (carry is not null || await frames.Reader.WaitToReadAsync())
            {
                OwnedFrame first = carry ?? await frames.Reader.ReadAsync();
                carry = null;
                unsealedStart = first.TimestampNs;
                Guid segment = Guid.NewGuid();
                string part;
                try { part = spool.PartPath(sessionId, segment); }
                catch { first.Dispose(); throw; }
                var encoder = new SegmentEncoder(options, width, height);
                try
                {
                    spool.Progress(sessionId, streamId, segment, clock, first.TimestampNs, first.TimestampNs);
                    await encoder.EncodeAsync(part, first, frames.Reader,
                        end => spool.Progress(sessionId, streamId, segment, clock, first.TimestampNs, end));
                }
                finally { first.Dispose(); carry = encoder.Carry; }
                var size = options.EncodedSize(width, height);
                spool.Seal(sessionId, streamId, segment, part, clock, first.TimestampNs, encoder.EndNs,
                    width, height, size.Width, size.Height, first.Sequence, encoder.LastSequence,
                    encoder.FrameCount, drops.Through(encoder.LastSequence),
                    drops.DetailsIncomplete ? ["dropped_frame_ranges_incomplete"] : []);
                unsealedStart = encoder.EndNs;
            }
        }
        catch (Exception error)
        {
            stopReason = error is CaptureFailure known ? known.Code : "encoding_failed";
            StopRequested?.Invoke(stopReason);
        }
        finally
        {
            StopCapture(stopReason);
            carry?.Dispose();
            while (frames.Reader.TryRead(out var remaining)) remaining.Dispose();
            ulong end = Math.Max(ClockMapping.NowNs(), lastObserved);
            spool.Gap(sessionId, streamId, clock, Math.Min(unsealedStart, end), end, stopReason);
        }
    }

    private void StopCapture(string reason)
    {
        lock (frameGate)
        {
            if (stopped) return;
            stopped = true;
            stopReason = reason;
            pool.FrameArrived -= OnFrame;
            item.Closed -= OnClosed;
            frames.Writer.TryComplete();
        }
        try { capture.Dispose(); }
        finally { pool.Dispose(); }
    }

    public async Task StopAsync(string reason)
    {
        StopCapture(reason);
        if (worker is not null) await worker;
    }

    public async ValueTask DisposeAsync()
    {
        if (resourcesDisposed) return;
        resourcesDisposed = true;
        try { await StopAsync("user_stopped"); }
        finally { budget.Dispose(); device.Dispose(); }
    }
}
