using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Threading;
using Windows.Foundation;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;
using Windows.Graphics.Imaging;
using Windows.Storage.Streams;

namespace ComputerUse.Windows {
    internal static class CaptureWorker {
        [StructLayout(LayoutKind.Sequential)] struct Rect { internal int Left,Top,Right,Bottom; internal Rectangle Value {get{return Rectangle.FromLTRB(Left,Top,Right,Bottom);}} }
        [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h,out Rect rect);
        [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
        [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h,out uint pid);
        [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h,IntPtr dc,uint flags);
        [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
        [DllImport("user32.dll")] static extern IntPtr GetWindowDpiAwarenessContext(IntPtr h);
        [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
        [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr h,int attr,out Rect rect,int size);
        [DllImport("d3d11.dll")] static extern int D3D11CreateDevice(IntPtr adapter,int driver,IntPtr software,uint flags,IntPtr levels,uint count,uint version,out IntPtr device,out int level,out IntPtr context);
        [DllImport("d3d11.dll")] static extern int CreateDirect3D11DeviceFromDXGIDevice(IntPtr dxgi,out IntPtr device);
        [ComImport,Guid("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface CaptureInterop {
            IntPtr CreateForWindow(IntPtr window,ref Guid iid);
            IntPtr CreateForMonitor(IntPtr monitor,ref Guid iid);
        }
        static IDirect3DDevice Device() {
            IntPtr native=IntPtr.Zero,context=IntPtr.Zero,dxgi=IntPtr.Zero,inspectable=IntPtr.Zero; int level;
            try {
                int hr=D3D11CreateDevice(IntPtr.Zero,1,IntPtr.Zero,0x20,IntPtr.Zero,0,7,out native,out level,out context);
                if(hr<0) { if(native!=IntPtr.Zero){Marshal.Release(native);native=IntPtr.Zero;} if(context!=IntPtr.Zero){Marshal.Release(context);context=IntPtr.Zero;}
                    hr=D3D11CreateDevice(IntPtr.Zero,5,IntPtr.Zero,0x20,IntPtr.Zero,0,7,out native,out level,out context); }
                Marshal.ThrowExceptionForHR(hr);
                var iid=new Guid("54ec77fa-1377-44e6-8c32-88fd5f44c84c");
                Marshal.ThrowExceptionForHR(Marshal.QueryInterface(native,ref iid,out dxgi));
                Marshal.ThrowExceptionForHR(CreateDirect3D11DeviceFromDXGIDevice(dxgi,out inspectable));
                return (IDirect3DDevice)Marshal.GetObjectForIUnknown(inspectable);
            } finally {foreach(var p in new[]{inspectable,dxgi,context,native}) if(p!=IntPtr.Zero)Marshal.Release(p);}
        }
        static Rectangle Bounds(IntPtr h) {Rect r;if(!GetWindowRect(h,out r))throw new Exception("window bounds unavailable");return r.Value;}
        static Bitmap Compositor(IntPtr h,out Rectangle source) {
            var iid=new Guid("79C3F95B-31F7-4EC2-A464-632EF5D30760");
            var factory=(CaptureInterop)WindowsRuntimeMarshal.GetActivationFactory(typeof(GraphicsCaptureItem));
            IntPtr pointer=factory.CreateForWindow(h,ref iid); GraphicsCaptureItem item;
            try {item=(GraphicsCaptureItem)Marshal.GetObjectForIUnknown(pointer);}finally{Marshal.Release(pointer);}
            var size=item.Size; new NativeCaptureGeometry(0,0,size.Width,size.Height,1);
            using(var device=Device())
            using(var pool=Direct3D11CaptureFramePool.CreateFreeThreaded(device,DirectXPixelFormat.B8G8R8A8UIntNormalized,2,size))
            using(var session=pool.CreateCaptureSession(item)) {
                // Keep the OS capture indicator. Reading never requests
                // borderless capture privileges or changes foreground.
                if(global::Windows.Foundation.Metadata.ApiInformation.IsPropertyPresent("Windows.Graphics.Capture.GraphicsCaptureSession","IsCursorCaptureEnabled"))session.IsCursorCaptureEnabled=false;
                session.StartCapture();
                var watch=Stopwatch.StartNew(); Direct3D11CaptureFrame frame=null;
                while(frame==null && watch.ElapsedMilliseconds<1500){frame=pool.TryGetNextFrame();if(frame==null)Thread.Sleep(15);}
                if(frame==null)throw new Exception("WGC did not publish a frame within 1500 ms");
                using(frame) {
                    if(frame.ContentSize.Width!=size.Width || frame.ContentSize.Height!=size.Height)throw new Exception("window resized during compositor capture");
                    var nativeBounds=Bounds(h);Rect extended;
                    var frameBounds=DwmGetWindowAttribute(h,9,out extended,Marshal.SizeOf(typeof(Rect)))==0?extended.Value:Rectangle.Empty;
                    source=frameBounds.Size==new System.Drawing.Size(size.Width,size.Height)?frameBounds:nativeBounds;
                    if(source.Width!=size.Width || source.Height!=size.Height)throw new Exception("compositor pixel extent has no verified screen coordinate mapping");
                    var copy=SoftwareBitmap.CreateCopyFromSurfaceAsync(frame.Surface,BitmapAlphaMode.Premultiplied);
                    try {
                        while(copy.Status==AsyncStatus.Started && watch.ElapsedMilliseconds<2000)Thread.Sleep(10);
                        if(copy.Status!=AsyncStatus.Completed){copy.Cancel();throw new Exception("compositor readback unavailable or timed out");}
                        using(var software=copy.GetResults()) {
                            if(software.PixelWidth!=size.Width || software.PixelHeight!=size.Height)throw new Exception("readback size mismatch");
                            var buffer=new global::Windows.Storage.Streams.Buffer(checked((uint)(size.Width*size.Height*4)));
                            software.CopyToBuffer(buffer);var bytes=new byte[buffer.Length];
                            using(var reader=DataReader.FromBuffer(buffer))reader.ReadBytes(bytes);
                            var bitmap=new Bitmap(size.Width,size.Height,PixelFormat.Format32bppPArgb);
                            var data=bitmap.LockBits(new Rectangle(0,0,size.Width,size.Height),ImageLockMode.WriteOnly,PixelFormat.Format32bppPArgb);
                            try {for(int row=0;row<size.Height;row++)Marshal.Copy(bytes,row*size.Width*4,IntPtr.Add(data.Scan0,row*data.Stride),size.Width*4);}
                            finally{bitmap.UnlockBits(data);}return bitmap;
                        }
                    } finally {copy.Close();}
                }
            }
        }
        static Bitmap Painted(IntPtr h,Rectangle physical) {
            IntPtr previous=SetThreadDpiAwarenessContext(GetWindowDpiAwarenessContext(h));
            if(previous==IntPtr.Zero)throw new Exception("window DPI context unavailable");
            try {var rect=Bounds(h);new NativeCaptureGeometry(rect.X,rect.Y,rect.Width,rect.Height,1);
                using(var bitmap=new Bitmap(rect.Width,rect.Height,PixelFormat.Format32bppRgb)) {
                    using(var graphics=Graphics.FromImage(bitmap)){
                        graphics.Clear(Color.Black);IntPtr dc=graphics.GetHdc();bool ok;
                        try{ok=PrintWindow(h,dc,2);}finally{graphics.ReleaseHdc(dc);}
                        if(!ok)throw new Exception("PrintWindow did not supply pixels");
                    }return new Bitmap(bitmap,physical.Size);
                }
            }finally{SetThreadDpiAwarenessContext(previous);}
        }
        [MTAThread] static int Main() {
            Console.OutputEncoding=new System.Text.UTF8Encoding(false);
            try {
                string line=Console.ReadLine();if(line==null)return 0;
                SetProcessDpiAwarenessContext(new IntPtr(-4));
                var p=Wire.Object(Wire.Serializer().DeserializeObject(line));var h=new IntPtr(Convert.ToInt64(p["handle"]));
                uint pid;GetWindowThreadProcessId(h,out pid);
                if(!IsWindow(h)||pid!=Convert.ToUInt32(p["pid"])||IsIconic(h))throw new Exception("window is unavailable or minimized; no activation performed");
                using(var process=Process.GetProcessById((int)pid))if(process.StartTime.ToUniversalTime().Ticks!=Int64.Parse((string)p["started"]))throw new Exception("window process changed");
                var before=Bounds(h);Rectangle source=before;
                using(var bitmap=Equals(p["route"],"wgc")?Compositor(h,out source):Painted(h,before)) {
                    if(Bounds(h)!=before || IsIconic(h))throw new Exception("window geometry changed during capture");
                    var shape=new NativeCaptureGeometry(source.X,source.Y,source.Width,source.Height,Convert.ToInt32(p["maxDimension"]));
                    using(var scaled=new Bitmap(bitmap,shape.Width,shape.Height))using(var stream=new MemoryStream()) {
                        scaled.Save(stream,ImageFormat.Png);if(stream.Length>32*1024*1024)throw new Exception("capture exceeds image budget");
                        Console.WriteLine(Wire.Json(Wire.Obj("png",Convert.ToBase64String(stream.ToArray()),"width",shape.Width,"height",shape.Height,
                            "rect",Wire.Obj("x",source.X,"y",source.Y,"width",source.Width,"height",source.Height))));
                    }
                }
                return 0;
            }catch(Exception e){Console.WriteLine(Wire.Json(Wire.Obj("error",e.GetType().Name+": "+e.Message)));return 1;}
        }
    }
}
