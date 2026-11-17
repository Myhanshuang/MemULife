using System.Threading.Channels;

namespace MemULife.Capture;

// Durable manifests are authoritative. A slow or disconnected stdout consumer
// must not retain GPU frames or prevent the user's Stop action from completing.
internal static class JsonLineSignals
{
    private static readonly Channel<string> Pending = Channel.CreateBounded<string>(
        new BoundedChannelOptions(64) { FullMode = BoundedChannelFullMode.Wait, SingleReader = true });

    static JsonLineSignals()
    {
        _ = Task.Run(async () =>
        {
            try
            {
                await foreach (string line in Pending.Reader.ReadAllAsync()) await Console.Out.WriteLineAsync(line);
            }
            catch (IOException) { Pending.Writer.TryComplete(); }
        });
    }

    public static void TryPublish(string line) => Pending.Writer.TryWrite(line);
}
