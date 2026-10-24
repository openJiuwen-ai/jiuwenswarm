// Portable math probe. Does not call GDI, PrintWindow, Win32, UIA or a desktop.
using System;
using System.Text.Json;

namespace System.Web.Script.Serialization {
    public sealed class JavaScriptSerializer {
        public int MaxJsonLength { get; set; }
        public int RecursionLimit { get; set; }
        public string Serialize(object value) { throw new NotSupportedException(); }
    }
}
namespace ComputerUse.Windows {
    internal static class NativeCaptureGeometryTests {
        static int count;
        static void Assert(bool condition) { count++; if (!condition) throw new Exception("geometry assertion " + count); }
        static void Reject(string code, Action action) {
            try { action(); } catch (Failure failure) { Assert(failure.Code == code); return; }
            throw new Exception("expected " + code);
        }
        static int Main() {
            var original = new NativeCaptureGeometry(-1920, -240, 3840, 2160, 2048);
            Assert(original.Width == 2048 && original.Height == 1152);
            var transform = (double[])original.Coordinates()["toNative"];
            Assert(transform[0] == 1.875 && transform[3] == 1.875 && transform[4] == -1920 && transform[5] == -240);
            Assert(transform[0]*original.Width + transform[4] == 1920);
            Assert(transform[3]*original.Height + transform[5] == 1920);
            var clipped = original.Clip(-2000, -400, 1000, 1000);
            Assert((double)clipped["x"] == 0 && (double)clipped["y"] == 0);
            Assert((double)clipped["width"] == 920/1.875 && (double)clipped["height"] == 840/1.875);
            Assert(original.Clip(1920, 0, 100, 100) == null);
            Assert(original.Clip(Int32.MaxValue-5, 0, 100, 100) == null);
            Assert(new NativeCaptureGeometry(0, 0, 32768, 1, 1).Height == 1);
            var odd = new NativeCaptureGeometry(31, 47, 1001, 777, 500);
            var oddTransform = (double[])odd.Coordinates()["toNative"];
            Assert(odd.Width == 500 && odd.Height == 388);
            Assert(Math.Abs(oddTransform[3]*odd.Height - 777) < 1e-9);
            Assert(new NativeCaptureGeometry(0, 0, 33, 51, 1024).Width == 33);
            Reject("observation_unavailable", () => new NativeCaptureGeometry(0, 0, 0, 100, 100));
            Reject("observation_unavailable", () => new NativeCaptureGeometry(0, 0, 65536, 1, 100));
            Reject("observation_unavailable", () => new NativeCaptureGeometry(0, 0, 8000, 8001, 100));
            Reject("invalid_request", () => new NativeCaptureGeometry(0, 0, 10, 10, 0));
            Reject("invalid_request", () => new NativeCaptureGeometry(0, 0, 10, 10, 4097));
            Console.WriteLine(JsonSerializer.Serialize(new { assertions = count, coordinates = original.Coordinates(), clipped = clipped,
                scope = "Production capture geometry; no Windows pixel capture or native GUI execution" }));
            return 0;
        }
    }
}
