using System.Threading.Channels;
using MemULife.Capture;

namespace MemULife.Capture.Windows;

// Explicit CI mode: generates color textures with WARP; never requests screen
// access or constructs GraphicsCaptureSession / GraphicsCapturePicker.
internal static class EncoderFixture
{
    public static async Task RunAsync(CaptureOptions options, SpoolWriter spool)
    {
        using var device = new D3DDevice(software: true);
        var channel = Channel.CreateBounded<OwnedFrame>(12);
        var clock = new ClockMapping();
        Guid session = Guid.NewGuid(), segment = Guid.NewGuid();
        ulong start = clock.AnchorNs;
        for (int i = 0; i < 12; i++)
            channel.Writer.TryWrite(device.SyntheticFrame(320, 240, i, start + (ulong)i * 100_000_000));
        channel.Writer.Complete();
        OwnedFrame first = await channel.Reader.ReadAsync();
        var encoder = new SegmentEncoder(options with { Fps = 10 }, 320, 240, useHardware: false);
        string part = spool.PartPath(session, segment);
        try
        {
            await encoder.EncodeAsync(part, first, channel.Reader);
            spool.Seal(session, options.StreamId, segment, part, clock, start, encoder.EndNs,
                320, 240, 320, 240, 0, encoder.LastSequence, encoder.FrameCount, [], ["synthetic_encoder_fixture_not_capture"]);
        }
        finally
        {
            first.Dispose();
            encoder.Carry?.Dispose();
            while (channel.Reader.TryRead(out var item)) item.Dispose();
        }
    }
}
