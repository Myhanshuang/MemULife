using System.Text.Json;
using MemULife.Capture;
using Microsoft.Win32;
using Windows.Graphics.Capture;

namespace MemULife.Capture.Windows;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            if (args.SequenceEqual(["probe"]))
            {
                bool supported = OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22000) && GraphicsCaptureSession.IsSupported();
                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    backend = "windows_wgc",
                    profile_id = "windows-wgc-v1",
                    platform = Environment.OSVersion.Version.ToString(),
                    capture_supported = supported,
                    permission = "needs_source_selection",
                    recording = false,
                    encoding = "windows_media_h264_fmp4_runtime_probe_required",
                    power_loss_qualified = false
                }));
                return supported ? 0 : 2;
            }
            if (args.Length < 1 || args[0] is not ("record" or "fixture")) throw new CaptureFailure("usage_record_probe_or_fixture_required");
            var values = new Dictionary<string, string>();
            for (int i = 1; i < args.Length; i += 2)
            {
                if (i + 1 == args.Length || !new[] { "--spool", "--device-id", "--stream-id", "--fps", "--segment-seconds", "--long-edge", "--bitrate", "--disk-limit-bytes" }.Contains(args[i]) || !values.TryAdd(args[i], args[i + 1]))
                    throw new CaptureFailure("configuration_invalid");
            }
            var options = new CaptureOptions(values["--spool"], Guid.Parse(values["--device-id"]), Guid.Parse(values["--stream-id"]),
                Fps: int.Parse(values.GetValueOrDefault("--fps", "10")),
                SegmentSeconds: int.Parse(values.GetValueOrDefault("--segment-seconds", "30")),
                LongEdge: int.Parse(values.GetValueOrDefault("--long-edge", "1920")),
                Bitrate: uint.Parse(values.GetValueOrDefault("--bitrate", "2000000")),
                DiskLimitBytes: long.Parse(values.GetValueOrDefault("--disk-limit-bytes", "8589934592")));
            options.Validate();
            if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22000)) throw new CaptureFailure("platform_unsupported");
            ApplicationConfiguration.Initialize();
            using var spool = new SpoolWriter(options);
            spool.RecoverInterrupted();
            if (args[0] == "fixture")
            {
                Task.Run(() => EncoderFixture.RunAsync(options, spool)).GetAwaiter().GetResult();
                return 0;
            }
            using var form = new CaptureForm(options, spool);
            Application.Run(form);
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { status = "failed", error_code = ErrorCode(error) }));
            return 1;
        }
    }

    public static string ErrorCode(Exception error) => error is CaptureFailure known ? known.Code : "native_capture_failed";
}

internal sealed class CaptureForm : Form
{
    private readonly CaptureOptions options;
    private readonly SpoolWriter spool;
    private readonly Button select = new() { Text = "选择屏幕或窗口并开始", AutoSize = true };
    private readonly Button stop = new() { Text = "停止录制", AutoSize = true, Enabled = false };
    private readonly Label status = new() { Text = "尚未开始。选择范围后才会录制；系统会显示采集边框。", AutoSize = true, MaximumSize = new Size(500, 0) };
    private CaptureEngine? engine;
    private bool finishing;
    private bool closeReady;

    public CaptureForm(CaptureOptions options, SpoolWriter spool)
    {
        this.options = options;
        this.spool = spool;
        Text = "MemULife 屏幕记录";
        ClientSize = new Size(560, 180);
        var layout = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(20), FlowDirection = FlowDirection.TopDown };
        layout.Controls.AddRange([status, select, stop]);
        Controls.Add(layout);
        select.Click += async (_, _) => await SelectAsync();
        stop.Click += async (_, _) => await StopAsync("user_stopped");
        FormClosing += ClosingAsync;
        SystemEvents.SessionSwitch += SessionChanged;
        SystemEvents.PowerModeChanged += PowerChanged;
        SystemEvents.DisplaySettingsChanged += DisplayChanged;
        Console.CancelKeyPress += ConsoleCancelled;
        FormClosed += (_, _) =>
        {
            SystemEvents.SessionSwitch -= SessionChanged;
            SystemEvents.PowerModeChanged -= PowerChanged;
            SystemEvents.DisplaySettingsChanged -= DisplayChanged;
            Console.CancelKeyPress -= ConsoleCancelled;
        };
    }

    private async Task SelectAsync()
    {
        if (engine is not null || finishing) return;
        select.Enabled = false;
        try
        {
            if (!GraphicsCaptureSession.IsSupported()) throw new CaptureFailure("capture_unsupported");
            var picker = new GraphicsCapturePicker();
            WinRT.Interop.InitializeWithWindow.Initialize(picker, Handle);
            var item = await picker.PickSingleItemAsync();
            if (item is null) { status.Text = "未选择来源，录制保持关闭。"; return; }
            engine = new CaptureEngine(options, spool, item);
            engine.StopRequested += RequestStop;
            engine.Start();
            stop.Enabled = true;
            status.Text = "正在记录选定范围。停止或关闭此窗口将封口当前片段。";
        }
        catch (Exception error)
        {
            status.Text = "无法启动：" + Program.ErrorCode(error);
            if (engine is not null) { await engine.DisposeAsync(); engine = null; }
        }
        finally { select.Enabled = engine is null; }
    }

    private void RequestStop(string reason)
    {
        if (!IsDisposed && IsHandleCreated) BeginInvoke(new Action(async () => await StopAsync(reason)));
    }

    private async Task StopAsync(string reason)
    {
        if (finishing || engine is null) return;
        finishing = true;
        stop.Enabled = false;
        status.Text = "正在完成当前片段…";
        try
        {
            await engine.StopAsync(reason);
            status.Text = reason == "user_stopped" ? "已停止。再次选择范围即可开启新会话。" : "采集已中断，请重新选择范围。原因：" + reason;
        }
        catch (Exception error) { status.Text = "采集已停止：" + Program.ErrorCode(error); }
        finally
        {
            try { await engine.DisposeAsync(); }
            catch (Exception error) { status.Text = "采集已停止：" + Program.ErrorCode(error); }
            engine = null; finishing = false; select.Enabled = true;
        }
    }

    private async void ClosingAsync(object? sender, FormClosingEventArgs args)
    {
        if (closeReady) return;
        args.Cancel = true;
        if (finishing) return;
        await StopAsync("user_stopped");
        closeReady = true;
        Close();
    }

    private void SessionChanged(object sender, SessionSwitchEventArgs args)
    {
        if (args.Reason is SessionSwitchReason.SessionLock or SessionSwitchReason.SessionLogoff
            or SessionSwitchReason.RemoteDisconnect or SessionSwitchReason.ConsoleDisconnect)
            RequestStop("session_interrupted");
    }
    private void PowerChanged(object sender, PowerModeChangedEventArgs args)
    {
        if (args.Mode == PowerModes.Suspend) RequestStop("system_suspended");
    }
    private void DisplayChanged(object? sender, EventArgs args) => RequestStop("display_config_changed");
    private void ConsoleCancelled(object? sender, ConsoleCancelEventArgs args) { args.Cancel = true; RequestStop("user_stopped"); }
}
