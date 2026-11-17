using System.Runtime.InteropServices;
using MemULife.Capture;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;
using Windows.Graphics.DirectX.Direct3D11;
using WinRT;

namespace MemULife.Capture.Windows;

internal sealed class D3DDevice : IDisposable
{
    private readonly ID3D11Device native;
    private readonly ID3D11DeviceContext context;
    public IDirect3DDevice WinRt { get; }

    public D3DDevice(bool software = false)
    {
        D3D11.D3D11CreateDevice(null, software ? DriverType.Warp : DriverType.Hardware, DeviceCreationFlags.BgraSupport,
            [FeatureLevel.Level_11_1, FeatureLevel.Level_11_0], out native, out context).CheckError();
        using var threading = context.QueryInterface<ID3D11Multithread>();
        threading.SetMultithreadProtected(true);
        using var dxgi = native.QueryInterface<IDXGIDevice>();
        Marshal.ThrowExceptionForHR(CreateDirect3D11DeviceFromDXGIDevice(dxgi.NativePointer, out nint pointer));
        try { WinRt = MarshalInterface<IDirect3DDevice>.FromAbi(pointer); }
        finally { Marshal.Release(pointer); }
    }

    public OwnedFrame SyntheticFrame(uint width, uint height, long sequence, ulong timestamp)
    {
        var texture = native.CreateTexture2D(new Texture2DDescription(Format.B8G8R8A8_UNorm,
            width, height, mipLevels: 1, bindFlags: BindFlags.ShaderResource | BindFlags.RenderTarget));
        try
        {
            using var view = native.CreateRenderTargetView(texture);
            context.ClearRenderTargetView(view, new Vortice.Mathematics.Color4((sequence % 10) / 10f, 0.25f, 0.5f, 1));
            using var dxgi = texture.QueryInterface<IDXGISurface>();
            Marshal.ThrowExceptionForHR(CreateDirect3D11SurfaceFromDXGISurface(dxgi.NativePointer, out nint pointer));
            IDirect3DSurface surface;
            try { surface = MarshalInterface<IDirect3DSurface>.FromAbi(pointer); }
            finally { Marshal.Release(pointer); }
            return new OwnedFrame(texture, surface, sequence, timestamp, () => { });
        }
        catch { texture.Dispose(); throw; }
    }

    public OwnedFrame Copy(IDirect3DSurface surface, long sequence, ulong timestamp, Action releaseBudget)
    {
        var access = surface.As<IDirect3DDxgiInterfaceAccess>();
        Guid iid = typeof(ID3D11Texture2D).GUID;
        Marshal.ThrowExceptionForHR(access.GetInterface(ref iid, out nint sourcePointer));
        using var source = new ID3D11Texture2D(sourcePointer);
        Texture2DDescription description = source.Description;
        description.BindFlags = BindFlags.ShaderResource | BindFlags.RenderTarget;
        description.CPUAccessFlags = CpuAccessFlags.None;
        description.Usage = ResourceUsage.Default;
        description.MiscFlags = ResourceOptionFlags.None;
        ID3D11Texture2D copy = native.CreateTexture2D(description);
        try
        {
            context.CopyResource(copy, source);
            using var dxgi = copy.QueryInterface<IDXGISurface>();
            Marshal.ThrowExceptionForHR(CreateDirect3D11SurfaceFromDXGISurface(dxgi.NativePointer, out nint pointer));
            IDirect3DSurface ownedSurface;
            try { ownedSurface = MarshalInterface<IDirect3DSurface>.FromAbi(pointer); }
            finally { Marshal.Release(pointer); }
            return new OwnedFrame(copy, ownedSurface, sequence, timestamp, releaseBudget);
        }
        catch { copy.Dispose(); throw; }
    }

    public static void RequireSdrOutputs()
    {
        // Picker items do not expose a stable monitor handle. Reject an HDR
        // desktop conservatively instead of labeling clipped HDR as sRGB.
        using var factory = DXGI.CreateDXGIFactory1<IDXGIFactory1>();
        for (uint adapterIndex = 0; factory.EnumAdapters1(adapterIndex, out var adapter).Success; adapterIndex++)
            using (adapter)
            {
                for (uint outputIndex = 0; adapter.EnumOutputs(outputIndex, out var output).Success; outputIndex++)
                    using (output)
                    using (var output6 = output.QueryInterface<IDXGIOutput6>())
                    {
                        if (output6.Description1.AttachedToDesktop && output6.Description1.ColorSpace != ColorSpaceType.RgbFullG22NoneP709)
                            throw new CaptureFailure("hdr_or_color_space_unsupported");
                    }
            }
    }

    public void Dispose() { WinRt.Dispose(); context.Dispose(); native.Dispose(); }

    [DllImport("d3d11.dll", ExactSpelling = true)]
    private static extern int CreateDirect3D11DeviceFromDXGIDevice(nint device, out nint graphicsDevice);
    [DllImport("d3d11.dll", ExactSpelling = true)]
    private static extern int CreateDirect3D11SurfaceFromDXGISurface(nint surface, out nint graphicsSurface);
}

[ComImport, Guid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IDirect3DDxgiInterfaceAccess
{
    [PreserveSig] int GetInterface(ref Guid iid, out nint value);
}

internal sealed class OwnedFrame(ID3D11Texture2D texture, IDirect3DSurface surface,
    long sequence, ulong timestamp, Action releaseBudget) : IDisposable
{
    private int disposed;
    public IDirect3DSurface Surface { get; } = surface;
    public long Sequence { get; } = sequence;
    public ulong TimestampNs { get; } = timestamp;
    public void Dispose()
    {
        if (Interlocked.Exchange(ref disposed, 1) != 0) return;
        Surface.Dispose();
        texture.Dispose();
        releaseBudget();
    }
}
